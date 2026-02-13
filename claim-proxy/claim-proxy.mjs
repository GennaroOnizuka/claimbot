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

  let ok = 0;
  let rateLimitHit = false;
  let rateLimitResetSeconds = 0;
  for (const cid of conditionIds) {
    try {
      const tx = createRedeemTx(cid);
      const response = await client.execute([tx], "redeem positions");
      const result = await response.wait();
      if (result?.transactionHash) {
        console.log("Claim OK:", cid.slice(0, 18) + "...", "tx:", result.transactionHash);
        ok++;
      } else {
        console.log("Claim fallito:", cid.slice(0, 18) + "...");
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
        rateLimitHit = true;
        const resetMatch = fullErr.match(/resets in (\d+) seconds/);
        if (resetMatch) {
          rateLimitResetSeconds = parseInt(resetMatch[1], 10);
        }
        console.log("RATE_LIMIT_429:", rateLimitResetSeconds || "unknown");
        break; // Non provare altri claim se quota esaurita
      }
      
      // Mostra errore completo su stdout (così Python lo vede sempre)
      console.log("Claim errore", cid.slice(0, 18) + "...", fullErr.substring(0, 500));
      // Anche su stderr per sicurezza
      console.error("Claim errore", cid.slice(0, 18) + "...", fullErr.substring(0, 500));
    }
  }
  if (rateLimitHit) {
    console.log("RATE_LIMIT_RESET_SECONDS:", rateLimitResetSeconds);
  }
  console.log("Fatto:", ok, "/", conditionIds.length);
  process.exit(ok === conditionIds.length ? 0 : 1);
}

main();
