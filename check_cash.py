#!/usr/bin/env python3
"""
Loop autonomo: controlla cash, cerca claim, esegue claim, aspetta 10 min, ripete.
- Claim via Relayer (se hai Builder API + Safe wallet)
- Claim via CLOB SELL a 0.99 (account Magic/Proxy: vendi le quote vincenti)
Uso: python3 check_cash.py
"""

import os
import sys
import time
from urllib.parse import urlparse, quote_plus
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

# Force unbuffered output per Render/Replit (vedi log immediatamente)
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None
sys.stderr.reconfigure(line_buffering=True) if hasattr(sys.stderr, 'reconfigure') else None

# Secondi tra un giro e l'altro (default 10 minuti)
LOOP_WAIT_SECONDS = int(os.getenv("CLAIM_LOOP_WAIT_SECONDS", "600"))
# Se 1/true: esegue un solo ciclo e esce (per test)
RUN_ONCE = os.getenv("RUN_ONCE", "").strip().lower() in ("1", "true", "yes")
# Flag globale per tracciare se c'è rate limit attivo (per ridurre claim per ciclo)
_rate_limit_active = False


def _get_proxy_url() -> str:
    """URL proxy da .env."""
    proxy_url = os.getenv("PROXY_URL", "").strip()
    if not proxy_url:
        host = os.getenv("PROXY_HOST", "").strip()
        port = os.getenv("PROXY_PORT", "").strip()
        if host and port:
            user = os.getenv("PROXY_USER", "").strip()
            password = os.getenv("PROXY_PASSWORD", "").strip() or os.getenv("PROXY_PASS", "").strip()
            if user and password:
                proxy_url = f"http://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}"
            else:
                proxy_url = f"http://{host}:{port}"
    return proxy_url


def _setup_proxy() -> None:
    """Attiva proxy per CLOB e richieste HTTP."""
    proxy_url = _get_proxy_url()
    if not proxy_url:
        return
    os.environ["HTTP_PROXY"] = proxy_url
    os.environ["HTTPS_PROXY"] = proxy_url
    os.environ["ALL_PROXY"] = proxy_url
    import httpx
    import py_clob_client.http_helpers.helpers as _h
    _h._http_client = httpx.Client(http2=True, proxy=proxy_url, timeout=30.0)


def run_one_cycle(ex, poly_safe: str, proxy_url: str, try_relayer: bool, try_clob_sell: bool, signature_type: int = 0) -> int:
    """Un singolo ciclo: balance, fetch claim, esegui claim (relayer o CLOB).
    Returns: seconds to wait before next cycle (0 = use default LOOP_WAIT_SECONDS).
    """
    from claims import (
        fetch_redeemable_positions,
        get_unique_condition_ids,
        build_redeem_tx,
        execute_redeem_via_relayer,
        try_claim_via_clob_sell,
    )

    balance = ex.get_balance()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Cash: {balance:.2f} USDC", flush=True)

    # Per i claim NON serve proxy: Relayer e Data API sono accessibili direttamente
    positions = fetch_redeemable_positions(poly_safe, proxy_url=None)
    if not positions:
        print("  Claim disponibili: 0", flush=True)
        return 0

    condition_ids = get_unique_condition_ids(positions)
    print(f"  Claim disponibili: {len(condition_ids)} mercato/i — {len(positions)} posizioni")
    for pos in positions[:15]:
        title = (pos.get("title") or pos.get("slug") or "—")[:55]
        size = pos.get("size") or pos.get("currentValue") or 0
        print(f"    • {title}: {size:.2f} share")
    if len(positions) > 15:
        print(f"    ... e altre {len(positions) - 15}")
    
    # Batch execution: tutte le transazioni in un'unica chiamata al relayer
    # Non serve più limitare perché facciamo 1 chiamata invece di N
    # Il relayer supporta batch fino a molte transazioni insieme

    # 1) Tenta claim via Relayer (richiede Builder API + Safe wallet)
    claimed_relayer = 0
    ok_count = 0
    builder_key = (os.getenv("BUILDER_API_KEY") or os.getenv("BUILDER_KEY") or "").strip()
    builder_secret = (os.getenv("BUILDER_SECRET") or os.getenv("BUILDER_API_SECRET") or "").strip()
    builder_pp = (os.getenv("BUILDER_PASSPHRASE") or os.getenv("BUILDER_PASS_PHRASE") or "").strip()
    pk = (os.getenv("PRIVATE_KEY") or "").strip()
    if pk and not pk.startswith("0x"):
        pk = "0x" + pk

    if try_relayer and builder_key and builder_secret and builder_pp and pk:
        # Account Magic (Proxy): il Relayer Python non supporta Proxy → usa script Node con PROXY
        if signature_type == 1:
            import subprocess
            script_dir = os.path.dirname(os.path.abspath(__file__))
            node_script = os.path.join(script_dir, "claim-proxy", "claim-proxy.mjs")
            if os.path.isfile(node_script):
                print("  Tentativo claim via Relayer PROXY (Node)...")
                try:
                    # Node va al relayer/RPC SENZA proxy: Relayer e RPC sono accessibili direttamente
                    # Il proxy serve solo per CLOB API (balance/trading), non per claim
                    node_env = {k: v for k, v in os.environ.items() if k not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")}
                    r = subprocess.run(
                        ["node", node_script] + condition_ids,
                        cwd=script_dir,
                        env=node_env,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    # Mostra sempre stdout e stderr per debug
                    output = (r.stdout or "") + "\n" + (r.stderr or "")
                    if r.returncode == 0:
                        # Batch execution: un'unica transazione per tutti i claim
                        claimed_relayer = len(condition_ids)
                        # Se il batch è riuscito, resetta il flag rate limit
                        global _rate_limit_active
                        if claimed_relayer > 0:
                            _rate_limit_active = False
                            print(f"  ✓ Batch claim riusciti: {claimed_relayer} mercati", flush=True)
                        if r.stdout:
                            for line in r.stdout.strip().split("\n"):
                                if line.strip():
                                    print(f"  {line}")
                    else:
                        # Controlla se è rate limit 429
                        if "429" in output or "quota exceeded" in output.lower() or "RATE_LIMIT_429" in output:
                            reset_match = None
                            for line in output.split("\n"):
                                if "RATE_LIMIT_RESET_SECONDS:" in line:
                                    try:
                                        reset_sec = int(line.split(":")[-1].strip())
                                        reset_match = reset_sec
                                    except:
                                        pass
                            if reset_match and reset_match > 0:
                                reset_hours = reset_match // 3600
                                reset_mins = (reset_match % 3600) // 60
                                print(f"  ⚠️  Rate limit Relayer: quota esaurita. Reset tra ~{reset_hours}h {reset_mins}m", flush=True)
                                print(f"  ℹ️  Continuerò a provare ogni 10 minuti (1 claim per ciclo)", flush=True)
                                # Attiva flag rate limit per ridurre claim nei prossimi cicli
                                global _rate_limit_active
                                _rate_limit_active = True
                                # NON aumentare wait: continua ogni 10 minuti anche con rate limit
                                return 0  # Usa wait normale (10 minuti)
                            else:
                                print(f"  ⚠️  Rate limit Relayer: quota esaurita. Continuerò a provare ogni 10 minuti.", flush=True)
                                global _rate_limit_active
                                _rate_limit_active = True
                                return 0  # Usa wait normale (10 minuti)
                        else:
                            # Errore diverso dal rate limit: mostra TUTTI i dettagli
                            print(f"  ⚠️  Errore claim Node (codice {r.returncode}):")
                            # Mostra tutto stdout e stderr
                            all_output = (r.stdout or "") + "\n" + (r.stderr or "")
                            for line in all_output.strip().split("\n"):
                                if line.strip():
                                    # Filtra solo righe informative (non stack trace completo se troppo lungo)
                                    if len(line) < 300 or "Claim errore" in line or "RATE_LIMIT" in line or "error" in line.lower():
                                        print(f"    {line[:200]}")
                            # Non fare fallback Python per Magic (modulo non installato su Replit/Render)
                except FileNotFoundError:
                    print("  Node non trovato. Installa Node.js e in claim-proxy/ esegui: npm install")
                except subprocess.TimeoutExpired:
                    print("  Timeout claim Node.")
                except Exception as e:
                    print(f"  Errore claim Node: {e}")
            else:
                print("  Script claim-proxy/claim-proxy.mjs non trovato. Per account Magic: cd claim-proxy && npm install")
        else:
            # Safe wallet: usa Relayer Python (batch execution)
            print("  Tentativo batch claim via Relayer (Python)...")
            txs = [build_redeem_tx(cid) for cid in condition_ids]
            results = execute_redeem_via_relayer(txs, pk, builder_key, builder_secret, builder_pp)
            # Batch execution: un unico risultato per tutte le transazioni
            if results and len(results) > 0:
                result = results[0]
                if result.get("error"):
                    print(f"  Relayer: {result['error'][:150]}", flush=True)
                elif result.get("transactionHash"):
                    count = result.get("count", len(condition_ids))
                    claimed_relayer = count
                    print(f"  ✓ Batch claim (relayer): {count} mercati, tx: {result['transactionHash']}", flush=True)
                    # Resetta flag rate limit se riuscito
                    global _rate_limit_active
                    _rate_limit_active = False
            else:
                print("  Relayer: nessun risultato", flush=True)
    elif try_relayer and not (builder_key and builder_secret and builder_pp):
        print("  Claim non eseguiti: mancano BUILDER_API_KEY, BUILDER_SECRET, BUILDER_PASSPHRASE in .env")

    # 2) Fallback: claim via CLOB SELL (solo se abilitato; di solito non funziona per mercati già risolti)
    if try_clob_sell and positions:
        sell_results = try_claim_via_clob_sell(positions, ex)
        ok_count = sum(1 for r in sell_results if r.get("ok"))
        if ok_count:
            for r in sell_results:
                if r.get("ok"):
                    print(f"  Claim OK (CLOB): {r.get('title', '—')}")

    # Se ci sono ancora claim non eseguiti, avvisa
    if positions and claimed_relayer == 0 and not (try_clob_sell and ok_count > 0):
        print("  → Fai claim manuale su polymarket.com → Portfolio → clicca Claim sui mercati risolti.")
    
    return 0  # Usa il wait time di default


def main():
    print("🚀 Avvio CLAIMBOT...", flush=True)
    
    if not os.getenv("PRIVATE_KEY"):
        print("ERRORE: PRIVATE_KEY mancante in .env", file=sys.stderr)
        sys.exit(1)
    
    print("✓ PRIVATE_KEY trovato", flush=True)
    _setup_proxy()
    print("✓ Proxy configurato (solo per CLOB)", flush=True)

    api_key = os.getenv("POLYMARKET_API_KEY", "").strip()
    api_secret = os.getenv("POLYMARKET_API_SECRET", "").strip()
    api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE", "").strip()
    private_key = (os.getenv("PRIVATE_KEY") or "").strip()
    if private_key and not private_key.startswith("0x"):
        private_key = "0x" + private_key
    if private_key.startswith("0x"):
        private_key = private_key[2:]
    signature_type = int(os.getenv("SIGNATURE_TYPE", "0"))

    try:
        print("✓ Caricamento OrderExecutor...", flush=True)
        from executor import OrderExecutor
        ex = OrderExecutor(
            api_key=api_key or "",
            api_secret=api_secret or "",
            api_passphrase=api_passphrase or "",
            private_key=private_key,
            signature_type=signature_type,
        )
        print("✓ OrderExecutor creato, test connessione...", flush=True)
        # warm-up
        balance = ex.get_balance()
        print(f"✓ Connessione OK! Balance iniziale: {balance:.2f} USDC", flush=True)
    except Exception as e:
        import traceback
        print(f"❌ Errore init: {e}", file=sys.stderr, flush=True)
        print(f"Traceback: {traceback.format_exc()}", file=sys.stderr, flush=True)
        sys.exit(1)

    print("✓ Ricerca indirizzo wallet...", flush=True)
    poly_safe = (os.getenv("POLY_SAFE_ADDRESS") or os.getenv("SAFE_ADDRESS") or "").strip()
    if not poly_safe:
        try:
            from web3 import Web3
            pk = (os.getenv("PRIVATE_KEY") or "").strip()
            if pk and not pk.startswith("0x"):
                pk = "0x" + pk
            if pk:
                poly_safe = Web3().eth.account.from_key(pk).address
                print(f"✓ Indirizzo derivato da PRIVATE_KEY: {poly_safe[:10]}...{poly_safe[-8:]}", flush=True)
        except Exception as e:
            print(f"⚠️  Errore derivazione indirizzo: {e}", flush=True)
    if not poly_safe:
        print("❌ ERRORE: imposta POLY_SAFE_ADDRESS in .env", file=sys.stderr, flush=True)
        sys.exit(1)
    else:
        print(f"✓ Indirizzo wallet: {poly_safe[:10]}...{poly_safe[-8:]}", flush=True)

    proxy_url = _get_proxy_url() or None
    try_relayer = os.getenv("CLAIM_USE_RELAYER", "1").strip().lower() in ("1", "true", "yes")
    # CLOB SELL non funziona per posizioni redeemable (mercato risolto = orderbook chiuso)
    try_clob_sell = os.getenv("CLAIM_USE_CLOB_SELL", "0").strip().lower() in ("1", "true", "yes")

    print("=" * 60, flush=True)
    print("--- CLAIMBOT loop (controlla → claim → aspetta {} min) ---".format(LOOP_WAIT_SECONDS // 60), flush=True)
    print(f"  Data/Ora avvio: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"  Relayer: {'sì' if try_relayer else 'no'}", flush=True)
    print(f"  CLOB SELL: {'sì' if try_clob_sell else 'no'}", flush=True)
    print(f"  Account: {poly_safe[:10]}...{poly_safe[-8:]}", flush=True)
    print("=" * 60, flush=True)
    print()

    cycle_count = 0
    while True:
        cycle_count += 1
        try:
            print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] === CICLO #{cycle_count} ===", flush=True)
            wait_seconds = run_one_cycle(ex, poly_safe, proxy_url, try_relayer, try_clob_sell, signature_type)
            if wait_seconds <= 0:
                wait_seconds = LOOP_WAIT_SECONDS
        except KeyboardInterrupt:
            print("\nInterrotto.", flush=True)
            break
        except Exception as e:
            import traceback
            print(f"  ❌ Errore ciclo: {e}", flush=True)
            print(f"  Traceback: {traceback.format_exc()}", flush=True)
            wait_seconds = LOOP_WAIT_SECONDS
        if RUN_ONCE:
            print("  RUN_ONCE=1: un solo ciclo, exit.", flush=True)
            break
        wait_mins = wait_seconds // 60
        if wait_mins >= 60:
            wait_hours = wait_mins // 60
            wait_mins_remainder = wait_mins % 60
            print(f"  ⏳ Prossimo controllo tra {wait_hours}h {wait_mins_remainder}m...", flush=True)
        else:
            print(f"  ⏳ Prossimo controllo tra {wait_mins} minuti...", flush=True)
        print("-" * 60, flush=True)
        time.sleep(wait_seconds)


if __name__ == "__main__":
    main()
