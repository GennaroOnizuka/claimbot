/**
 * Claim (redeem) posizioni Polymarket via Relayer con tipo PROXY (account Magic/email).
 * Uso: node claim-proxy.mjs [conditionId1] [conditionId2] ...
 * Legge .env dalla cartella padre (CLAIMBOT).
 */

import { config } from "dotenv";
import { resolve, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
// Carica .env dalla root del progetto (padre di claim-proxy)
config({ path: resolve(__dirname, "..", ".env") });

const RELAYER_URL = process.env.RELAYER_URL || "https://relayer-v2.polymarket.com";
const CHAIN_ID = parseInt(process.env.CHAIN_ID || "137", 10);
const CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045";
const USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174";

const ctfRedeemAbi = [
  {
    constant: false,
    inputs: [
      { name: "collateralToken", type: "address" },
      { name: "parentCollectionId", type: "bytes32" },
      { name: "conditionId", type: "bytes32" },
      { name: "indexSets", type: "uint256[]" },
    ],
    name: "redeemPositions",
    outputs: [],
    payable: false,
    stateMutability: "nonpayable",
    type: "function",
  },
];

async function main() {
  const conditionIds = process.argv.slice(2).filter((c) => c && c.trim());
  if (conditionIds.length === 0) {
    console.error("Uso: node claim-proxy.mjs <conditionId1> [conditionId2] ...");
    process.exit(1);
  }

  const pk = (process.env.PRIVATE_KEY || "").trim();
  const key = (process.env.BUILDER_API_KEY || process.env.BUILDER_KEY || "").trim();
  const secret = (process.env.BUILDER_SECRET || "").trim();
  const passphrase = (process.env.BUILDER_PASSPHRASE || process.env.BUILDER_PASS_PHRASE || "").trim();

  if (!pk || !key || !secret || !passphrase) {
    console.error("Imposta in .env: PRIVATE_KEY, BUILDER_API_KEY, BUILDER_SECRET, BUILDER_PASSPHRASE");
    process.exit(1);
  }

  const privateKey = pk.startsWith("0x") ? pk : "0x" + pk;

  const { createWalletClient, http, encodeFunctionData, zeroHash } = await import("viem");
  const { privateKeyToAccount } = await import("viem/accounts");
  const { polygon } = await import("viem/chains");
  const { RelayClient, RelayerTxType } = await import("@polymarket/builder-relayer-client");
  const { BuilderConfig } = await import("@polymarket/builder-signing-sdk");

  const account = privateKeyToAccount(privateKey);
  const wallet = createWalletClient({
    account,
    chain: polygon,
    transport: http(process.env.RPC_URL || "https://polygon-rpc.com"),
  });

  const builderConfig = new BuilderConfig({ localBuilderCreds: { key, secret, passphrase } });
  const client = new RelayClient(RELAYER_URL, CHAIN_ID, wallet, builderConfig, RelayerTxType.PROXY);

  function createRedeemTx(conditionId) {
    const cid = conditionId.startsWith("0x") ? conditionId : "0x" + conditionId;
    const data = encodeFunctionData({
      abi: ctfRedeemAbi,
      functionName: "redeemPositions",
      args: [USDC_ADDRESS, zeroHash, cid, [1, 2]],
    });
    return { to: CTF_ADDRESS, data, value: "0" };
  }

  // Crea TUTTE le transazioni per batch execution (una sola chiamata al relayer)
  const allTxs = conditionIds.map(cid => createRedeemTx(cid));
  console.log(`Batch: ${allTxs.length} claim in un'unica transazione`);
  
  try {
    // Esegui TUTTE le transazioni in un'unico batch (una sola chiamata al relayer)
    const response = await client.execute(allTxs, `Redeem ${allTxs.length} positions`);
    const result = await response.wait();
    
    if (result?.transactionHash) {
      console.log(`✓ Batch claim OK: ${allTxs.length} mercati, tx: ${result.transactionHash}`);
      console.log("Fatto:", allTxs.length, "/", conditionIds.length);
      process.exit(0);
    } else {
      console.error("Batch claim fallito: nessun transactionHash");
      process.exit(1);
    }
  } catch (e) {
    // Estrai messaggio errore completo
    let errMsg = e.message || String(e);
    let errData = "";
    if (e.data) {
      try {
        errData = typeof e.data === "string" ? e.data : JSON.stringify(e.data);
      } catch {}
    }
    if (e.response?.data) {
      try {
        errData = typeof e.response.data === "string" ? e.response.data : JSON.stringify(e.response.data);
      } catch {}
    }
    // Cattura anche error object completo
    let errObj = "";
    try {
      errObj = JSON.stringify(e, Object.getOwnPropertyNames(e)).substring(0, 300);
    } catch {}
    
    const fullErr = errMsg + (errData ? " | data: " + errData : "") + (errObj ? " | obj: " + errObj : "");
    
    // Rate limit 429: quota exceeded
    if (fullErr.includes("429") || fullErr.includes("quota exceeded") || fullErr.includes("Too Many Requests")) {
      const resetMatch = fullErr.match(/resets in (\d+) seconds/);
      const resetSeconds = resetMatch ? parseInt(resetMatch[1], 10) : 0;
      console.log("RATE_LIMIT_429:", resetSeconds || "unknown");
      console.log("RATE_LIMIT_RESET_SECONDS:", resetSeconds);
      process.exit(1);
    }
    
    // Mostra errore completo
    console.log("Batch claim errore:", fullErr.substring(0, 500));
    console.error("Batch claim errore:", fullErr.substring(0, 500));
    process.exit(1);
  }
}

main();
