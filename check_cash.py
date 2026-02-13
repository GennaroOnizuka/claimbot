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

# Secondi tra un giro e l'altro (default 10 minuti)
LOOP_WAIT_SECONDS = int(os.getenv("CLAIM_LOOP_WAIT_SECONDS", "600"))
# Se 1/true: esegue un solo ciclo e esce (per test)
RUN_ONCE = os.getenv("RUN_ONCE", "").strip().lower() in ("1", "true", "yes")


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


def run_one_cycle(ex, poly_safe: str, proxy_url: str, try_relayer: bool, try_clob_sell: bool, signature_type: int = 0) -> None:
    """Un singolo ciclo: balance, fetch claim, esegui claim (relayer o CLOB)."""
    from claims import (
        fetch_redeemable_positions,
        get_unique_condition_ids,
        build_redeem_tx,
        execute_redeem_via_relayer,
        try_claim_via_clob_sell,
    )

    balance = ex.get_balance()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Cash: {balance:.2f} USDC")

    positions = fetch_redeemable_positions(poly_safe, proxy_url=proxy_url or None)
    if not positions:
        print("  Claim disponibili: 0")
        return

    condition_ids = get_unique_condition_ids(positions)
    print(f"  Claim disponibili: {len(condition_ids)} mercato/i — {len(positions)} posizioni")
    for pos in positions[:15]:
        title = (pos.get("title") or pos.get("slug") or "—")[:55]
        size = pos.get("size") or pos.get("currentValue") or 0
        print(f"    • {title}: {size:.2f} share")
    if len(positions) > 15:
        print(f"    ... e altre {len(positions) - 15}")

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
                    # Node va al relayer/RPC senza proxy (evita "plain HTTP sent to HTTPS port")
                    node_env = {k: v for k, v in os.environ.items() if k not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")}
                    r = subprocess.run(
                        ["node", node_script] + condition_ids,
                        cwd=script_dir,
                        env=node_env,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    if r.returncode == 0:
                        claimed_relayer = len(condition_ids)
                        if r.stdout:
                            for line in r.stdout.strip().split("\n"):
                                if line.strip():
                                    print(f"  {line}")
                    else:
                        if r.stderr:
                            print(f"  Node PROXY: {r.stderr.strip()[:200]}")
                        # fallback al relayer Python (fallirà per Magic)
                        txs = [build_redeem_tx(cid) for cid in condition_ids]
                        results = execute_redeem_via_relayer(txs, pk, builder_key, builder_secret, builder_pp)
                        errors = [x.get("error") for x in results if isinstance(x, dict) and x.get("error")]
                        if errors:
                            msg = errors[0][:90]
                            if len(errors) > 1 and all(e[:50] == errors[0][:50] for e in errors):
                                print(f"  Relayer: {msg} ({len(errors)} mercati)")
                            else:
                                for e in errors:
                                    print(f"  Relayer: {e[:90]}")
                except FileNotFoundError:
                    print("  Node non trovato. Installa Node.js e in claim-proxy/ esegui: npm install")
                except subprocess.TimeoutExpired:
                    print("  Timeout claim Node.")
                except Exception as e:
                    print(f"  Errore claim Node: {e}")
            else:
                print("  Script claim-proxy/claim-proxy.mjs non trovato. Per account Magic: cd claim-proxy && npm install")
        else:
            # Safe wallet: usa Relayer Python
            print("  Tentativo claim via Relayer...")
            txs = [build_redeem_tx(cid) for cid in condition_ids]
            results = execute_redeem_via_relayer(txs, pk, builder_key, builder_secret, builder_pp)
            errors = [r.get("error") for r in results if isinstance(r, dict) and r.get("error")]
            for r in results:
                if isinstance(r, dict) and not r.get("error"):
                    claimed_relayer += 1
                    print(f"  Claim (relayer): inviato")
            if errors:
                msg = errors[0][:90]
                if len(errors) > 1 and all(e[:50] == errors[0][:50] for e in errors):
                    print(f"  Relayer: {msg} ({len(errors)} mercati)")
                else:
                    for e in errors:
                        print(f"  Relayer: {e[:90]}")
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


def main():
    if not os.getenv("PRIVATE_KEY"):
        print("ERRORE: PRIVATE_KEY mancante in .env", file=sys.stderr)
        sys.exit(1)

    _setup_proxy()

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
        from executor import OrderExecutor
        ex = OrderExecutor(
            api_key=api_key or "",
            api_secret=api_secret or "",
            api_passphrase=api_passphrase or "",
            private_key=private_key,
            signature_type=signature_type,
        )
        # warm-up
        ex.get_balance()
    except Exception as e:
        print(f"Errore init: {e}", file=sys.stderr)
        sys.exit(1)

    poly_safe = (os.getenv("POLY_SAFE_ADDRESS") or os.getenv("SAFE_ADDRESS") or "").strip()
    if not poly_safe:
        try:
            from web3 import Web3
            pk = (os.getenv("PRIVATE_KEY") or "").strip()
            if pk and not pk.startswith("0x"):
                pk = "0x" + pk
            if pk:
                poly_safe = Web3().eth.account.from_key(pk).address
        except Exception:
            pass
    if not poly_safe:
        print("ERRORE: imposta POLY_SAFE_ADDRESS in .env", file=sys.stderr)
        sys.exit(1)

    proxy_url = _get_proxy_url() or None
    try_relayer = os.getenv("CLAIM_USE_RELAYER", "1").strip().lower() in ("1", "true", "yes")
    # CLOB SELL non funziona per posizioni redeemable (mercato risolto = orderbook chiuso)
    try_clob_sell = os.getenv("CLAIM_USE_CLOB_SELL", "0").strip().lower() in ("1", "true", "yes")

    print("--- CLAIMBOT loop (controlla → claim → aspetta {} min) ---".format(LOOP_WAIT_SECONDS // 60))
    print("  Relayer:", "sì" if try_relayer else "no")
    print()

    while True:
        try:
            run_one_cycle(ex, poly_safe, proxy_url, try_relayer, try_clob_sell, signature_type)
        except KeyboardInterrupt:
            print("\nInterrotto.")
            break
        except Exception as e:
            print(f"  Errore ciclo: {e}")
        if RUN_ONCE:
            print("  RUN_ONCE=1: un solo ciclo, exit.")
            break
        print(f"  Prossimo controllo tra {LOOP_WAIT_SECONDS // 60} minuti...")
        time.sleep(LOOP_WAIT_SECONDS)


if __name__ == "__main__":
    main()
