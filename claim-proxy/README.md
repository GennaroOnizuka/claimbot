# Claim via Relayer PROXY (account Magic/email)

Per account Polymarket **Magic/email** (SIGNATURE_TYPE=1) il Relayer Python supporta solo Safe.  
Questo script Node.js usa `RelayerTxType.PROXY` e permette di eseguire il claim da terminale o da `check_cash.py`.

## Setup (una volta)

```bash
cd claim-proxy
npm install
```

Il file `.env` va messo nella **cartella padre** (CLAIMBOT), con:

- `PRIVATE_KEY`
- `BUILDER_API_KEY`, `BUILDER_SECRET`, `BUILDER_PASSPHRASE`

## Uso da terminale

```bash
# dalla root CLAIMBOT
node claim-proxy/claim-proxy.mjs <conditionId1> [conditionId2] ...
```

I `conditionId` sono in formato `0x` + 64 caratteri esadecimali (es. dall’output di `check_cash.py`).

## Uso da Python

`check_cash.py` con **SIGNATURE_TYPE=1** e credenziali Builder chiama automaticamente questo script quando ci sono claim disponibili. Non serve avviare nulla a parte:

```bash
python3 check_cash.py
```

Requisiti: **Node.js** installato e `npm install` eseguito in `claim-proxy/`.
