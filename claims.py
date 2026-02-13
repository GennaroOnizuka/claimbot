"""
Claim (redeem) vinciti su Polymarket.

Riferimenti documentazione:
- Data API positions (redeemable): https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user
- CTF Redeem: https://docs.polymarket.com/developers/CTF/redeem
- Relayer (gasless): https://docs.polymarket.com/developers/builders/relayer-client
- Deployment CTF: https://docs.polymarket.com/developers/CTF/deployment-resources
"""

import os
from typing import List, Dict, Any, Optional

# Data API
DATA_API_BASE = "https://data-api.polymarket.com"
POSITIONS_PATH = "/positions"

# Polygon (doc Polymarket)
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# parentCollectionId = bytes32(0) for Polymarket; indexSets = [1, 2] for binary
REDEEM_INDEX_SETS = [1, 2]

# ABI minimo per encode redeemPositions
REDEEM_POSITIONS_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "type": "function",
    }
]


def fetch_redeemable_positions(
    user_address: str,
    proxy_url: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """
    Recupera le posizioni claimabili (redeemable) per un indirizzo.
    Data API: GET /positions?user=<addr>&redeemable=true
    """
    import httpx

    url = f"{DATA_API_BASE}{POSITIONS_PATH}"
    params = {"user": user_address, "redeemable": "true", "limit": limit}
    kwargs = {"params": params, "timeout": 30.0}
    if proxy_url:
        kwargs["proxy"] = proxy_url

    with httpx.Client(http2=True, **({"proxy": proxy_url} if proxy_url else {})) as client:
        resp = client.get(url, params=params, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, list) else []


def build_redeem_tx(condition_id: str) -> Dict[str, str]:
    """
    Costruisce la transazione per redeemPositions su CTF (Polygon).
    condition_id: bytes32 hex (0x...).
    Ritorna dict con to, data, value per il relayer.
    """
    from web3 import Web3

    w3 = Web3()
    if not condition_id.startswith("0x"):
        condition_id = "0x" + condition_id
    cond_hex = condition_id[2:].lower().rjust(64, "0")[:64]
    condition_id_bytes = bytes.fromhex(cond_hex)
    parent_zero = b"\x00" * 32

    ctf = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_ADDRESS),
        abi=REDEEM_POSITIONS_ABI,
    )
    fn = ctf.functions.redeemPositions(
        Web3.to_checksum_address(USDC_ADDRESS),
        parent_zero,
        condition_id_bytes,
        REDEEM_INDEX_SETS,
    )
    data_hex = fn._encode_transaction_data()
    if not data_hex.startswith("0x"):
        data_hex = "0x" + data_hex
    return {"to": CTF_ADDRESS, "data": data_hex, "value": "0"}


def execute_redeem_via_relayer(
    transactions: List[Dict[str, str]],
    private_key: str,
    builder_key: str,
    builder_secret: str,
    builder_passphrase: str,
    relayer_url: str = "https://relayer-v2.polymarket.com/",
    chain_id: int = 137,
    proxy_url: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Esegue le transazioni di redeem tramite Polymarket Relayer (gasless).
    Richiede credenziali Builder API. Il client Python supporta solo Safe wallet;
    con account Magic/email (SIGNATURE_TYPE=1) il relayer può fallire: in quel caso
    fare claim manuale su polymarket.com → Portfolio.
    Ritorna lista di risultati (transactionHash, state, ecc.) o [{"error": "..."}].
    """
    try:
        from py_builder_relayer_client.client import RelayClient
        from py_builder_relayer_client.models import SafeTransaction, OperationType
        from py_builder_signing_sdk.config import BuilderConfig
        from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
    except ImportError as e:
        return [{"error": f"Relayer non disponibile: {e}. pip install py-builder-relayer-client py-builder-signing-sdk"}]

    pk = (private_key.strip() if private_key.startswith("0x") else "0x" + private_key) if private_key else ""
    if not pk or not builder_key or not builder_secret or not builder_passphrase:
        return []

    try:
        creds = BuilderApiKeyCreds(key=builder_key, secret=builder_secret, passphrase=builder_passphrase)
        builder_config = BuilderConfig(local_builder_creds=creds)
    except Exception as e:
        return [{"error": f"BuilderConfig: {e}"}]

    client = RelayClient(relayer_url, chain_id, pk, builder_config)
    results = []
    for tx in transactions:
        try:
            safe_tx = SafeTransaction(
                to=tx["to"],
                operation=OperationType.Call,
                data=tx["data"],
                value=tx.get("value", "0"),
            )
            response = client.execute([safe_tx], "Redeem positions")
            if response:
                results.append({"transactionID": getattr(response, "transaction_id", None), "transactionHash": getattr(response, "transaction_hash", None)})
        except Exception as e:
            err = str(e).strip()
            if "not deployed" in err.lower() or "expected safe" in err.lower():
                results.append({"error": "Relayer supporta solo Safe wallet. Con account Magic fai claim su polymarket.com → Portfolio."})
            else:
                results.append({"error": err})
    return results


def get_unique_condition_ids(positions: List[Dict[str, Any]]) -> List[str]:
    """Estrae conditionId unici dalla lista di posizioni (per una redeem per condition)."""
    seen = set()
    out = []
    for p in positions:
        cid = (p.get("conditionId") or p.get("condition_id") or "").strip()
        if cid and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def try_claim_via_clob_sell(
    positions: List[Dict[str, Any]],
    executor,  # OrderExecutor
) -> List[Dict[str, Any]]:
    """
    Tenta di "claimare" vendendo le quote vincenti su CLOB a 0.99 (mercato risolto ≈ 1$).
    Funziona con account Magic/Proxy. Per ogni posizione: token_id = asset, SELL size @ 0.99.
    Ritorna lista di {"ok": True/False, "title": str, "error": str opzionale}.
    """
    results = []
    for pos in positions:
        token_id = (pos.get("asset") or pos.get("tokenId") or "").strip()
        size = pos.get("size") or pos.get("currentValue") or 0
        title = (pos.get("title") or pos.get("slug") or "—")[:50]
        if not token_id or not size or float(size) < 0.01:
            results.append({"ok": False, "title": title, "error": "asset/size mancanti"})
            continue
        try:
            size_f = float(size)
            # CLOB: size in quote (shares); prezzo 0.99 per outcome risolto vincente
            out = executor.place_limit_order(
                token_id=token_id,
                side="SELL",
                size=size_f,
                price=0.99,
            )
            results.append({"ok": out is not None, "title": title, "order": out})
        except Exception as e:
            results.append({"ok": False, "title": title, "error": str(e)})
    return results
