"""
Execution Layer for Polymarket Bot
Handles order placement and management using py-clob-client
"""

import os
import time

# Timeout richieste HTTP (proxy può essere lento); rispettato dove usiamo httpx con timeout=30
if "HTTPX_TIMEOUT" not in os.environ:
    os.environ["HTTPX_TIMEOUT"] = "30"
from urllib.parse import urlparse, quote_plus
from typing import Dict, Optional, List, Tuple
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.exceptions import PolyApiException
from py_clob_client.headers import headers as _poly_headers
from web3 import Web3
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception


# Paesi da provare per il proxy (come Replit): prima CH, poi gli altri
PROXY_COUNTRIES = ["ch", "no", "se", "nl", "dk"]
PROXY_COUNTRY_NAMES = {"ch": "Svizzera", "no": "Norvegia", "se": "Svezia", "nl": "Olanda", "dk": "Danimarca"}


def _print_creds_for_env(creds) -> None:
    """Stampa le credenziali derivate così l'utente può copiarle in .env (solo alla prima derivazione)."""
    try:
        api_key = getattr(creds, "api_key", None) or (creds.get("api_key") if isinstance(creds, dict) else None)
        secret = getattr(creds, "api_secret", None) or getattr(creds, "secret", None) or (creds.get("secret") if isinstance(creds, dict) else None)
        passphrase = getattr(creds, "api_passphrase", None) or getattr(creds, "passphrase", None) or (creds.get("passphrase") if isinstance(creds, dict) else None)
        if api_key and secret and passphrase:
            print("  → Copia queste in .env per le prossime run:")
            print(f"     POLYMARKET_API_KEY={api_key}")
            print(f"     POLYMARKET_API_SECRET={secret}")
            print(f"     POLYMARKET_API_PASSPHRASE={passphrase}")
    except Exception:
        pass


def _apply_poly_address_override():
    """
    Con signature_type=2 e POLY_SAFE_ADDRESS, la libreria invia POLY_ADDRESS=signer (EOA)
    ma l'API key è associata all'indirizzo Polymarket (funder). Il server restituisce 401.
    Patch: inviamo POLY_ADDRESS=funder nelle L2 headers così il server abbina la key corretta.
    """
    funder = (os.getenv("POLY_SAFE_ADDRESS") or os.getenv("SAFE_ADDRESS") or "").strip()
    if not funder:
        return
    _orig = _poly_headers.create_level_2_headers

    def _create_l2(signer, creds, request_args):
        h = _orig(signer, creds, request_args)
        h[_poly_headers.POLY_ADDRESS] = funder
        return h

    _poly_headers.create_level_2_headers = _create_l2


def _get_proxy_parts() -> Optional[Tuple[str, str, str, str]]:
    """Ritorna (host, port, user, password) da PROXY_URL o PROXY_HOST/PORT/USER/PASS."""
    url = os.getenv("PROXY_URL", "").strip()
    if url:
        p = urlparse(url)
        if p.hostname and p.port:
            user = (p.username or "").split("-session-")[0].split("_cr.")[0].split("__cr.")[0] or p.username
            return (p.hostname, str(p.port), user or "", p.password or "")
    host = os.getenv("PROXY_HOST", "").strip()
    port = os.getenv("PROXY_PORT", "").strip()
    user = os.getenv("PROXY_USER", "").strip()
    pwd = os.getenv("PROXY_PASS", "").strip() or os.getenv("PROXY_PASSWORD", "").strip()
    if host and port and user and pwd:
        return (host, port, user, pwd)
    return None


def _build_proxy_url(country: str, sticky_index: int = 0) -> Optional[str]:
    """Costruisce URL proxy DataImpulse: user_cr.COUNTRY su porta 823 (stessa del .env)."""
    parts = _get_proxy_parts()
    if not parts:
        return None
    host, port, user, pwd = parts
    use_port = int(port) if port.isdigit() else 823
    geo_user = f"{user}_cr.{country}"
    safe_user = quote_plus(geo_user)
    safe_pwd = quote_plus(pwd)
    return f"http://{safe_user}:{safe_pwd}@{host}:{use_port}"


def _patch_clob_client_proxy(proxy_url: str):
    """Imposta il client HTTP CLOB per usare il proxy indicato (per piazzare ordine)."""
    import httpx
    import py_clob_client.http_helpers.helpers as _h
    _h._http_client = httpx.Client(http2=True, proxy=proxy_url, timeout=30.0)


def _get_saved_clob_client():
    import py_clob_client.http_helpers.helpers as _h
    return getattr(_h, "_http_client", None)


def _restore_clob_client(saved_client) -> None:
    """Ripristina il client CLOB dopo i tentativi ordine con proxy per paese."""
    import py_clob_client.http_helpers.helpers as _h
    if saved_client is not None:
        _h._http_client = saved_client


def _is_request_exception(e: Exception) -> bool:
    """True se è errore di connessione/rete (Request exception, status_code=None)."""
    if isinstance(e, PolyApiException):
        if getattr(e, "status_code", None) is None:
            return True
        return False
    err = str(e).lower()
    return "request exception" in err or "status_code=none" in err or "connection" in err or "timeout" in err


def _log_request_exception(e: Exception, context: str = "place_order") -> None:
    """Log dettagliato per debug errore rete/proxy."""
    print(f"\n  [DEBUG {context}]")
    print(f"    Tipo: {type(e).__name__}")
    print(f"    Messaggio: {str(e)}")
    if isinstance(e, PolyApiException):
        print(f"    status_code: {getattr(e, 'status_code', 'N/A')}")
        print(f"    error_msg: {getattr(e, 'error_msg', 'N/A')}")
    cause = getattr(e, "__cause__", None)
    if cause:
        print(f"    Causa: {type(cause).__name__}: {cause}")
    if os.getenv("POLYBOT_DEBUG_PROXY"):
        import traceback
        traceback.print_exc()


def _clob_error_category(e: Exception) -> str:
    """
    Classifica l'errore CLOB per messaggi chiari e decisione retry.
    Ritorna: "proxy_ruleset" (0x02), "regional_403" (403), "retryable", "other".
    """
    msg = str(e).lower()
    cause = getattr(e, "__cause__", None)
    while cause:
        msg += " " + str(cause).lower()
        cause = getattr(cause, "__cause__", None)
    if "0x02" in msg or "ruleset" in msg:
        return "proxy_ruleset"
    if isinstance(e, PolyApiException) and getattr(e, "status_code", None) == 403:
        return "regional_403"
    if "403" in msg or "forbidden" in msg:
        return "regional_403"
    if "timeout" in msg or "connection" in msg or "temporarily" in msg:
        return "retryable"
    return "other"


def _is_retryable_clob_error(e: Exception) -> bool:
    """True se ha senso ritentare (no 403, no 0x02 proxy ruleset)."""
    cat = _clob_error_category(e)
    return cat in ("retryable", "other")


def _log_clob_error(context: str, token_id: str, e: Exception) -> None:
    """Stampa messaggio appropriato in base al tipo di errore."""
    cat = _clob_error_category(e)
    if cat == "proxy_ruleset":
        print(f"Error {context} {token_id}: proxy SOCKS5 (0x02) — connessione non consentita dalle regole del proxy. Verifica provider.")
    elif cat == "regional_403":
        print(f"Error {context} {token_id}: 403 Forbidden — restrizione geografica o autenticazione. Usa proxy/VPN consentito.")
    else:
        print(f"Error {context} {token_id}: {e}")


class OrderExecutor:
    """Handles order execution on Polymarket"""
    
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        private_key: str,
        signature_type: int = 0
    ):
        """
        Initialize order executor
        
        Args:
            api_key: Polymarket API key (for future use)
            api_secret: Polymarket API secret (for future use)
            api_passphrase: Polymarket API passphrase (for future use)
            private_key: Wallet private key (without 0x prefix)
            signature_type: 0=EOA, 1=Email/Magic, 2=Safe
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.private_key = private_key
        self.signature_type = signature_type

        # Con Safe (signature_type=2): L2 auth deve inviare POLY_ADDRESS=funder, altrimenti 401
        _apply_poly_address_override()

        # Initialize CLOB client with private key (come discountry/polymarket-trading-bot)
        # Per Safe (signature_type=2) serve l'indirizzo del wallet Polymarket (funder)
        funder = os.getenv("POLY_SAFE_ADDRESS", "").strip() or os.getenv("SAFE_ADDRESS", "").strip()
        self.client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,  # Polygon mainnet
            key=private_key,
            signature_type=signature_type,
            **({"funder": funder} if funder else {}),
        )
        
        # L2 auth: necessaria per post_order. Come discountry: deriva credenziali dalla chiave
        # se non sono già fornite (POLYMARKET_API_KEY/SECRET/PASSPHRASE).
        if api_key and api_secret and api_passphrase:
            try:
                from py_clob_client.clob_types import ApiCreds
                self.client.set_api_creds(ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase))
                print("CLOB: using API credentials from .env")
                if funder:
                    print(f"CLOB: API auth address = Polymarket (funder) {funder[:10]}...{funder[-6:]}")
            except Exception as e:
                print(f"Warning: Could not set API credentials from env: {e}")
                self._derive_and_set_api_creds()
        else:
            self._derive_and_set_api_creds()
        print("CLOB client initialized successfully")
        # Verifica allineamento con https://docs.polymarket.com/developers/CLOB/authentication
        if signature_type == 2 and funder:
            print("  (L2 + signature_type=2 GNOSIS_SAFE, funder=proxy wallet da polymarket.com/settings)")

    def _derive_and_set_api_creds(self) -> None:
        """
        Deriva credenziali L2 via API CLOB (L1 = firma con private key).
        Su Polymarket non c'è una 'Trading API Key' in Settings: si creano/derivano così.
        """
        try:
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            print("CLOB: L2 API credentials derived from private key (create_or_derive_api_creds)")
            # Salva in .env per le prossime run (opzionale)
            _print_creds_for_env(creds)
        except Exception as e:
            print(f"Warning: Could not derive API credentials: {e}")
            print("  Order placement will fail until POLYMARKET_API_KEY/SECRET/PASSPHRASE are set or derivation works.")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def get_orderbook(self, token_id: str) -> Optional[Dict]:
        """
        Fetch orderbook for a token
        
        Args:
            token_id: CLOB token ID
            
        Returns:
            Orderbook dictionary with bids and asks
        """
        try:
            orderbook = self.client.get_order_book(token_id)
            return orderbook
        except Exception as e:
            print(f"Error fetching orderbook for {token_id}: {e}")
            return None
    
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def get_midpoint_price(self, token_id: str) -> Optional[float]:
        """
        Get current midpoint price from CLOB API (quote reale Polymarket).
        Usa GET /midpoint invece dell'orderbook (che può essere vuoto → 0.50).
        """
        try:
            result = self.client.get_midpoint(token_id)
            if result is None:
                return None
            # può essere dict {"mid": "0.52"} o stringa
            if isinstance(result, dict):
                mid = result.get("mid") or result.get("price")
                return float(mid) if mid is not None else None
            if isinstance(result, str):
                return float(result)
            return float(result)
        except Exception as e:
            print(f"Error fetching midpoint for {token_id}: {e}")
            return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def get_price(self, token_id: str) -> Optional[float]:
        """
        Get current midpoint price for a token (alias: usa CLOB midpoint API)
        """
        return self.get_midpoint_price(token_id)
    
    def place_limit_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
        post_only: bool = True
    ) -> Optional[Dict]:
        """
        Place a limit order
        
        Args:
            token_id: CLOB token ID
            side: "BUY" or "SELL"
            size: Order size in USDC
            price: Limit price
            post_only: If True, order will be rejected if it would fill immediately
            
        Returns:
            Order response dictionary or None if failed
        """
        try:
            # Convert side string to constant
            order_side = BUY if side.upper() == "BUY" else SELL
            
            # CLOB accetta solo prezzi in [0.01, 0.99]; quote tipo 99.5% → 0.995 fuori range
            price = max(0.01, min(0.99, float(price)))

            # Create order using OrderArgs
            order_args = OrderArgs(
                price=price,
                size=size,
                side=order_side,
                token_id=token_id
            )
            
            # Retry con backoff esponenziale su "Request exception!" (rete/proxy)
            max_retries = 5
            backoff = [1, 2, 4, 8, 16]

            def _do_post_order():
                return self.client.create_and_post_order(order_args)

            def _try_order_with_retry(attempt_label: str = ""):
                last_e = None
                for attempt in range(max_retries):
                    try:
                        return _do_post_order()
                    except PolyApiException as e:
                        last_e = e
                        if _is_request_exception(e):
                            _log_request_exception(e, "POST order")
                            if attempt < max_retries - 1:
                                wait = backoff[attempt]
                                print(f"  Errore connessione (tentativo {attempt + 1}/{max_retries}), riprovo tra {wait}s...")
                                time.sleep(wait)
                                continue
                            raise
                        raise
                if last_e:
                    raise last_e
                return None

            # Debug: test GET verso CLOB con stesso proxy prima del POST (solo se POLYBOT_DEBUG_PROXY=1)
            if os.getenv("POLYBOT_DEBUG_PROXY"):
                _debug_proxy = os.getenv("ALL_PROXY") or os.getenv("HTTPS_PROXY") or ""
                if _debug_proxy:
                    try:
                        import requests as _req
                        r = _req.get("https://clob.polymarket.com/", proxies={"https": _debug_proxy}, timeout=10)
                        print(f"  [DEBUG] Pre-POST GET CLOB: HTTP {r.status_code}")
                    except Exception as _de:
                        print(f"  [DEBUG] Pre-POST GET CLOB: FAILED — {type(_de).__name__}: {_de}")

            # Primo tentativo: stesso client/proxy usato per GET — niente patch
            try:
                response = _try_order_with_retry()
                if response is not None:
                    print(f"Order placed: {side} {size} @ {price} for token {token_id}")
                    return response
            except PolyApiException as e:
                if getattr(e, "status_code", None) != 403:
                    raise
                err = (getattr(e, "error_msg", None) or "")
                if isinstance(err, dict):
                    err = err.get("error", str(err))
                if "regional" not in str(err).lower():
                    raise
            # 403 regional: prova con proxy per paese (user_cr.ch, user_cr.no, ...)
            proxy_parts = _get_proxy_parts()
            saved_client = _get_saved_clob_client()
            try:
                if proxy_parts:
                    for attempt, country in enumerate(PROXY_COUNTRIES):
                        proxy_url = _build_proxy_url(country, attempt)
                        if not proxy_url:
                            continue
                        cname = PROXY_COUNTRY_NAMES.get(country, country)
                        print(f"  Tentativo {attempt + 1}/{len(PROXY_COUNTRIES)}: ordine via proxy {cname}...")
                        _patch_clob_client_proxy(proxy_url)
                        try:
                            response = _try_order_with_retry()
                            if response is not None:
                                print(f"Order placed: {side} {size} @ {price} for token {token_id} (via {cname})")
                                return response
                        except PolyApiException as e2:
                            err2 = (getattr(e2, "error_msg", None) or "")
                            if isinstance(err2, dict):
                                err2 = err2.get("error", str(err2))
                            if getattr(e2, "status_code", None) == 403 and "regional" in str(err2).lower():
                                time.sleep(2)
                                continue
                            raise
                    print("  403 regional: tutti i paesi proxy provati.")
                    return None
            finally:
                _restore_clob_client(saved_client)
            
        except PolyApiException as e:
            if getattr(e, "status_code", None) == 401:
                print("Error placing order: 401 Unauthorized — API key non valida o scaduta.")
                print("  → Vai su polymarket.com → Settings → API Key e genera/usa la chiave per il TRADING (non la Builder Key).")
                print("  → Aggiorna POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE in .env")
            elif getattr(e, "status_code", None) == 403:
                err = (getattr(e, "error_msg", None) or "")
                if isinstance(err, dict):
                    err = err.get("error", str(err))
                print("Error placing order: 403 — Accesso bloccato per restrizioni geografiche.")
                print("  → Il proxy deve uscire da un paese consentito (es. Svizzera). Controlla PROXY_URL e riprova.")
            else:
                print(f"Error placing order: {e}")
            return None
        except Exception as e:
            print(f"Error placing order: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an open order
        
        Args:
            order_id: Order ID to cancel
            
        Returns:
            True if successful, False otherwise
        """
        try:
            self.client.cancel_order(order_id)
            print(f"Order cancelled: {order_id}")
            return True
        except Exception as e:
            print(f"Error cancelling order {order_id}: {e}")
            return False
    
    def get_open_orders(self) -> List[Dict]:
        """
        Get all open orders
        
        Returns:
            List of open order dictionaries
        """
        try:
            orders = self.client.get_orders()
            return orders if orders else []
        except Exception as e:
            print(f"Error fetching open orders: {e}")
            return []
    
    def get_balance(self) -> float:
        """
        Get USDC balance (CLOB: get_balance_allowance con asset_type=COLLATERAL).
        L'API restituisce il valore in unità con 6 decimali (micro-USDC); convertiamo in USDC.
        Returns:
            Balance in USDC
        """
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=-1,
            )
            balance_info = self.client.get_balance_allowance(params)
            raw = 0.0
            if isinstance(balance_info, dict):
                raw = float(balance_info.get("available", balance_info.get("balance", balance_info.get("usdc", balance_info.get("size", 0)))))
            elif isinstance(balance_info, (int, float)):
                raw = float(balance_info)
            elif isinstance(balance_info, list):
                for item in balance_info:
                    if isinstance(item, dict) and (item.get("currency") == "USDC" or "available" in item):
                        raw = float(item.get("available", item.get("balance", 0)))
                        break
            # API restituisce micro-USDC (6 decimali): 30041908 -> 30.041908 USDC
            return raw / 1e6
        except Exception as e:
            print(f"Error fetching balance: {e}")
            raise
    
    def execute_arbitrage(
        self,
        opportunity,
        yes_size: float,
        no_size: float
    ) -> bool:
        """
        Execute an arbitrage opportunity
        
        Args:
            opportunity: ArbitrageOpportunity object
            yes_size: Size to trade for YES token
            no_size: Size to trade for NO token
            
        Returns:
            True if orders placed successfully, False otherwise
        """
        if opportunity.action == "buy_both":
            # Buy both YES and NO tokens
            yes_order = self.place_limit_order(
                token_id=opportunity.yes_token_id,
                side="BUY",
                size=yes_size,
                price=opportunity.yes_price
            )
            
            no_order = self.place_limit_order(
                token_id=opportunity.no_token_id,
                side="BUY",
                size=no_size,
                price=opportunity.no_price
            )
            
            return yes_order is not None and no_order is not None
            
        elif opportunity.action == "sell_both":
            # Sell both YES and NO tokens
            yes_order = self.place_limit_order(
                token_id=opportunity.yes_token_id,
                side="SELL",
                size=yes_size,
                price=opportunity.yes_price
            )
            
            no_order = self.place_limit_order(
                token_id=opportunity.no_token_id,
                side="SELL",
                size=no_size,
                price=opportunity.no_price
            )
            
            return yes_order is not None and no_order is not None
        
        return False
