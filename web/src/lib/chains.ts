// Chain constants for the client-side scanner and wallet chain switching, plus the health-checked
// public RPC fallbacks. Ported from buybeam.my: batch-balance contracts, CoinGecko ids and this
// app's own chain icons.
//
// The endpoint list itself lives in `lib/rpc.ts` since 2026-09-10 (T54), because it is no longer a
// constant: it is the app's verified order with whatever this browser's owner picked in the RPC
// settings popup at the head of it. This file asks `rpcUrls()` for that order and health-checks it
// exactly as it always did — the first entry takes every health check, so the pick is a starting
// point and never a restriction.
import { JsonRpcProvider, Network } from 'ethers';
import { activeRpcUrl, onRpcChange, probeMatches, probeRpcUrl, rpcUrls } from './rpc';

export const NATIVE_ADDRESS = '0x0000000000000000000000000000000000000000';
const NATIVE_ALIASES = new Set([
  NATIVE_ADDRESS,
  '0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',
  '0x0000000000000000000000000000000000001010', // Polygon's MATIC precompile as some feeds list it
]);

export function isNativeToken(address: string | undefined | null): boolean {
  return !address || NATIVE_ALIASES.has(address.toLowerCase());
}

/**
 * The cross-chain order router lists two non-EVM chains. They are not errors and not "unreachable"
 * — there is simply no eth_* RPC to ask, so the scanner skips them and says so.
 */
export const NON_EVM_CHAINS = new Set([7565164 /* Solana */, 728126428 /* Tron */]);

export function isNonEvmChain(chainId: number): boolean {
  return NON_EVM_CHAINS.has(chainId);
}

/** balanceFor(address[] _tokens, address _account) view returns (uint256[] balances, uint256[] decimals) */
// eth_getCode-verified 2026-09-09. Optimism is deliberately absent: 0x50188692… has NO code there
// (`eth_getCode` → "0x"), although `GET /v1/dex/chains` still advertises it — so the reader falls
// through to Multicall3, which every one of these chains does have.
export const BATCH_BALANCE_CONTRACTS: Record<number, string> = {
  1: '0x50188692d5549386d102642036bab916b998c814',
  56: '0x50188692d5549386d102642036bab916b998c814',
  137: '0x50188692d5549386d102642036bab916b998c814',
  42161: '0x50188692d5549386d102642036bab916b998c814',
  8453: '0x202eF28cA6D4d2B94C4Ea0534a8E6261581c70a4',
  250: '0x55C93b20Dd2F790AC429D6341a022A781791654A',
  43114: '0x55C93b20Dd2F790AC429D6341a022A781791654A',
  59144: '0x0e4AdD4DC86Ae1Aa0FA43Bd7e6a9fB8Be2d5504d',
};

/**
 * Multicall3, same address on every chain that has it. Verified with eth_getCode on 2026-09-09 to
 * carry 3,808 bytes of code on every listed chain without a batch-balance contract (4663, 1514, 25,
 * 999, 1776, 143, 4326). The scanner uses it for ERC-20 balances there; if the call ever fails it
 * falls back to per-token balanceOf.
 */
export const MULTICALL3 = '0xcA11bde05977b3631167028862bE2a173976CA11';

// CoinGecko asset-platform ids, read from GET /api/v3/asset_platforms by chain_identifier
// (2026-09-09): every EVM chain on the list has one.
export const COINGECKO_PLATFORMS: Record<number, string> = {
  1: 'ethereum',
  10: 'optimistic-ethereum',
  56: 'binance-smart-chain',
  137: 'polygon-pos',
  250: 'fantom',
  4663: 'robinhood',
  8453: 'base',
  42161: 'arbitrum-one',
  43114: 'avalanche',
  59144: 'linea',
  1514: 'story',
  25: 'cronos',
  999: 'hyperevm',
  1776: 'injective',
  143: 'monad',
  4326: 'megaeth',
};

// native_coin_id from the same asset_platforms response, plus the older chains we already listed.
export const COINGECKO_NATIVE_IDS: Record<number, string> = {
  1: 'ethereum',
  10: 'ethereum',
  56: 'binancecoin',
  137: 'polygon-ecosystem-token',
  250: 'fantom',
  4663: 'ethereum',
  8453: 'ethereum',
  42161: 'ethereum',
  43114: 'avalanche-2',
  59144: 'ethereum',
  1514: 'story-2',
  25: 'crypto-com-chain',
  999: 'hyperliquid',
  1776: 'injective-protocol',
  143: 'monad',
  4326: 'ethereum',
  100: 'xdai',
  324: 'ethereum',
  146: 'sonic-3',
  5000: 'mantle',
  130: 'ethereum',
  1868: 'ethereum',
  57073: 'ethereum',
  480: 'ethereum',
  2741: 'ethereum',
};

// Chain icons are this app's own files, not a third party's CDN: one SVG per chain under
// web/public/chains/, fetched once on 2026-09-09 and committed, so a badge cannot depend on someone
// else's uptime or tell them who is looking. These are the 15 EVM chains the router offers; Story
// (1514) has no icon, so its badge is simply not shown — a chip without one is not an error.
const CHAIN_ICONS = new Set([1, 10, 25, 56, 137, 143, 250, 999, 1776, 4326, 4663, 8453, 42161, 43114, 59144]);

/** This app's chain icon, or null when there is no file for the chain (the chip then shows no badge). */
export function chainIconUrl(chainId: number): string | null {
  return CHAIN_ICONS.has(chainId) ? `/chains/${chainId}.svg` : null;
}

/**
 * The ONE way a chain id is written for a wallet — `wallet_switchEthereumChain` (EIP-3326),
 * `wallet_addEthereumChain` (EIP-3085) and `eth_chainId` (EIP-695) all specify the same thing:
 * a 0x-prefixed, **unpadded**, non-zero hexadecimal string. Chain 1 is `0x1`. Never `0x01`.
 *
 * ⛔ Never `toBeHex()` for a chain id. ethers pads a quantity to whole BYTES — `toBeHex(1)` is
 * `"0x01"`, `toBeHex(10)` `"0x0a"`, `toBeHex(999)` `"0x03e7"`, `toBeHex(1514)` `"0x05ea"`,
 * `toBeHex(1776)` `"0x06f0"` — which is right on the EVM wire and wrong for this parameter.
 *
 * Paid for on 2026-09-10 on a real wallet on pgas.me: the admin pressed Deposit while already on
 * Ethereum, we sent `{chainId:"0x01"}`, the wallet compared the string it was given against the
 * ones it holds, did not find it, and answered "unrecognized chain" (4902) — so our own 4902 branch
 * politely offered to ADD Ethereum mainnet to a wallet that has had it since it was installed.
 * ("When I click Deposit, you ask me to Add ETH Network, but I have it.") MetaMask's family does not
 * even get that far: it refuses `"0x01"` with -32602 *"Expected 0x-prefixed, unpadded, non-zero
 * hexadecimal string"*, so on those wallets the Deposit button simply could not switch chains.
 *
 * The read side is deliberately the opposite shape: `parseChainId` in `lib/wallet.ts` accepts every
 * form a wallet might answer with (`'0x1'`, `'0x01'`, `1`, `'1'`, bigint). Strict out, tolerant in.
 */
export function chainIdHex(chainId: number): string {
  if (!Number.isInteger(chainId) || chainId <= 0) throw new Error(`Pgas.me was handed something that is not a chain id: ${chainId}`);
  return '0x' + chainId.toString(16);
}

export interface ChainMeta {
  chainName: string;
  nativeCurrency: { name: string; symbol: string; decimals: number };
  rpcUrls: string[];
  blockExplorerUrls: string[];
}

const eth = { name: 'Ether', symbol: 'ETH', decimals: 18 };

/** Parameters for wallet_addEthereumChain when a wallet does not know a chain. */
export const CHAIN_META: Record<number, ChainMeta> = {
  1: {
    chainName: 'Ethereum',
    nativeCurrency: eth,
    rpcUrls: ['https://ethereum-rpc.publicnode.com'],
    blockExplorerUrls: ['https://etherscan.io'],
  },
  56: {
    chainName: 'BNB Smart Chain',
    nativeCurrency: { name: 'BNB', symbol: 'BNB', decimals: 18 },
    rpcUrls: ['https://bsc-dataseed.binance.org'],
    blockExplorerUrls: ['https://bscscan.com'],
  },
  137: {
    chainName: 'Polygon',
    nativeCurrency: { name: 'POL', symbol: 'POL', decimals: 18 },
    rpcUrls: ['https://polygon-bor-rpc.publicnode.com'],
    blockExplorerUrls: ['https://polygonscan.com'],
  },
  42161: {
    chainName: 'Arbitrum One',
    nativeCurrency: eth,
    rpcUrls: ['https://arb1.arbitrum.io/rpc'],
    blockExplorerUrls: ['https://arbiscan.io'],
  },
  10: {
    chainName: 'OP Mainnet',
    nativeCurrency: eth,
    rpcUrls: ['https://mainnet.optimism.io'],
    blockExplorerUrls: ['https://optimistic.etherscan.io'],
  },
  8453: { chainName: 'Base', nativeCurrency: eth, rpcUrls: ['https://mainnet.base.org'], blockExplorerUrls: ['https://basescan.org'] },
  43114: {
    chainName: 'Avalanche C-Chain',
    nativeCurrency: { name: 'AVAX', symbol: 'AVAX', decimals: 18 },
    rpcUrls: ['https://api.avax.network/ext/bc/C/rpc'],
    blockExplorerUrls: ['https://snowtrace.io'],
  },
  250: {
    chainName: 'Fantom Opera',
    nativeCurrency: { name: 'FTM', symbol: 'FTM', decimals: 18 },
    rpcUrls: ['https://rpc.ftm.tools'],
    blockExplorerUrls: ['https://ftmscan.com'],
  },
  59144: { chainName: 'Linea', nativeCurrency: eth, rpcUrls: ['https://rpc.linea.build'], blockExplorerUrls: ['https://lineascan.build'] },
  4663: {
    chainName: 'Robinhood Chain',
    nativeCurrency: eth,
    rpcUrls: ['https://rpc.mainnet.chain.robinhood.com'],
    blockExplorerUrls: ['https://robinscan.io'],
  },
  1514: {
    chainName: 'Story',
    nativeCurrency: { name: 'IP', symbol: 'IP', decimals: 18 },
    rpcUrls: ['https://mainnet.storyrpc.io'],
    blockExplorerUrls: ['https://www.storyscan.io'],
  },
  25: {
    chainName: 'Cronos',
    nativeCurrency: { name: 'CRO', symbol: 'CRO', decimals: 18 },
    rpcUrls: ['https://evm.cronos.org'],
    blockExplorerUrls: ['https://explorer.cronos.org'],
  },
  999: {
    chainName: 'HyperEVM',
    nativeCurrency: { name: 'HYPE', symbol: 'HYPE', decimals: 18 },
    rpcUrls: ['https://rpc.hyperliquid.xyz/evm'],
    blockExplorerUrls: ['https://hyperevmscan.io'],
  },
  1776: {
    chainName: 'Injective EVM',
    nativeCurrency: { name: 'Injective', symbol: 'INJ', decimals: 18 },
    rpcUrls: ['https://sentry.evm-rpc.injective.network'],
    blockExplorerUrls: ['https://blockscout.injective.network'],
  },
  143: {
    chainName: 'Monad',
    nativeCurrency: { name: 'Monad', symbol: 'MON', decimals: 18 },
    rpcUrls: ['https://rpc.monad.xyz'],
    blockExplorerUrls: ['https://monadscan.com'],
  },
  4326: {
    chainName: 'MegaETH',
    nativeCurrency: eth,
    rpcUrls: ['https://mainnet.megaeth.com/rpc'],
    blockExplorerUrls: ['https://mega.etherscan.io'],
  },
  100: {
    chainName: 'Gnosis',
    nativeCurrency: { name: 'xDAI', symbol: 'xDAI', decimals: 18 },
    rpcUrls: ['https://rpc.gnosischain.com'],
    blockExplorerUrls: ['https://gnosisscan.io'],
  },
  324: {
    chainName: 'zkSync Era',
    nativeCurrency: eth,
    rpcUrls: ['https://mainnet.era.zksync.io'],
    blockExplorerUrls: ['https://era.zksync.network'],
  },
  146: {
    chainName: 'Sonic',
    nativeCurrency: { name: 'Sonic', symbol: 'S', decimals: 18 },
    rpcUrls: ['https://rpc.soniclabs.com'],
    blockExplorerUrls: ['https://sonicscan.org'],
  },
  80094: {
    chainName: 'Berachain',
    nativeCurrency: { name: 'BERA', symbol: 'BERA', decimals: 18 },
    rpcUrls: ['https://rpc.berachain.com'],
    blockExplorerUrls: ['https://berascan.com'],
  },
  5000: {
    chainName: 'Mantle',
    nativeCurrency: { name: 'MNT', symbol: 'MNT', decimals: 18 },
    rpcUrls: ['https://rpc.mantle.xyz'],
    blockExplorerUrls: ['https://mantlescan.xyz'],
  },
  130: {
    chainName: 'Unichain',
    nativeCurrency: eth,
    rpcUrls: ['https://mainnet.unichain.org'],
    blockExplorerUrls: ['https://uniscan.xyz'],
  },
  1868: {
    chainName: 'Soneium',
    nativeCurrency: eth,
    rpcUrls: ['https://rpc.soneium.org'],
    blockExplorerUrls: ['https://soneium.blockscout.com'],
  },
};

/**
 * `wallet_addEthereumChain` parameters for a chain, with the endpoint THIS BROWSER reads through at
 * the head of `rpcUrls` — a wallet that adds the chain then uses the same node the page does, and a
 * user who picked one because the default is blocked where they are does not get the default handed
 * back to them by the Add-chain prompt. `chainIdHex` stays the only writer of the chain id itself.
 */
export function chainMeta(chainId: number): ChainMeta | undefined {
  const meta = CHAIN_META[chainId];
  if (!meta) return undefined;
  const picked = activeRpcUrl(chainId);
  if (!picked) return meta;
  return { ...meta, rpcUrls: [...new Set([picked, ...meta.rpcUrls])] };
}

export function chainName(chainId: number | null | undefined, fromApi?: { chain_id: number; name: string }[]): string {
  if (chainId === null || chainId === undefined) return '–';
  return fromApi?.find((c) => c.chain_id === chainId)?.name ?? CHAIN_META[chainId]?.chainName ?? `Chain ${chainId}`;
}

/** The Wallets page reads destination balances (native only) on these chains. */
export const DESTINATION_CHAINS = [1, 42161, 8453];

// ---------- health-checked public fallbacks ----------
// One healthy URL per chain is kept for the session; `invalidateFallback` moves to the next URL when
// it starts failing. A URL that answers for another chain is never used. Providers are cached per
// (chain, batchMaxCount) so the scanner can ask for a batching provider without a second health check.
const healthy = new Map<number, string | null>();
const providers = new Map<string, JsonRpcProvider>();
const inflight = new Map<number, Promise<string | null>>();
const cursor = new Map<number, number>();

export function withTimeout<T>(p: Promise<T>, ms: number, label = 'timed out'): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const t = setTimeout(() => reject(new Error(label)), ms);
    p.then(
      (v) => {
        clearTimeout(t);
        resolve(v);
      },
      (e) => {
        clearTimeout(t);
        reject(e);
      },
    );
  });
}

/**
 * The endpoints for a chain IN READING ORDER. `lib/rpc.ts` owns that order — the app's own verified
 * list, with the endpoint this browser's owner picked in the settings popup moved to the front. One
 * reader: everything on the client that reads a chain (the portfolio scan, a single balance
 * re-read, a receipt wait, an allowance read) comes through here, so a pick applies to all of them
 * or to none of them.
 */
export function fallbackUrls(chainId: number): string[] {
  return rpcUrls(chainId);
}

async function healthyUrl(chainId: number): Promise<string | null> {
  if (healthy.has(chainId)) return healthy.get(chainId) ?? null;
  const running = inflight.get(chainId);
  if (running) return running;
  const p = (async () => {
    const urls = fallbackUrls(chainId);
    for (let i = cursor.get(chainId) ?? 0; i < urls.length; i++) {
      // ONE prober (T54): the same `probeRpcUrl` the settings popup's dots come from, so a dot is
      // the reader's own experience of an endpoint and not a second opinion about it. It never
      // throws — a failure is a recorded probe — so the loop reads its verdict rather than a catch.
      if (!probeMatches(await probeRpcUrl(urls[i], 4000), chainId)) continue;
      healthy.set(chainId, urls[i]);
      cursor.set(chainId, i);
      return urls[i];
    }
    healthy.set(chainId, null);
    return null;
  })();
  inflight.set(chainId, p);
  try {
    return await p;
  } finally {
    inflight.delete(chainId);
  }
}

/**
 * A provider on this chain's healthy public RPC, or null when every URL failed.
 * `batchMaxCount` 1 (the default) sends one request per call; ~50 lets ethers pack many eth_calls
 * into one HTTP round trip, which the per-token balance fallback needs.
 */
export async function getFallbackProvider(chainId: number, batchMaxCount = 1): Promise<JsonRpcProvider | null> {
  const url = await healthyUrl(chainId);
  if (!url) return null;
  const key = `${chainId}|${batchMaxCount}`;
  const existing = providers.get(key);
  if (existing) return existing;
  const net = new Network(`chain-${chainId}`, chainId);
  const prov = new JsonRpcProvider(url, net, { staticNetwork: net, batchMaxCount });
  providers.set(key, prov);
  return prov;
}

/**
 * A new pick in the settings popup makes every cached provider for that chain the WRONG endpoint,
 * and the cursor an index into a list that no longer starts where it did. So the cache for that
 * chain is dropped and the next read starts again at the head of the new order. `chainId === null`
 * is "Reset to defaults": everything goes.
 */
onRpcChange((e) => {
  if (e.kind !== 'selection') return;
  if (e.chainId === null) {
    for (const [, prov] of providers) prov.destroy();
    providers.clear();
    healthy.clear();
    cursor.clear();
    return;
  }
  for (const [key, prov] of providers) {
    if (key.startsWith(`${e.chainId}|`)) {
      prov.destroy();
      providers.delete(key);
    }
  }
  healthy.delete(e.chainId);
  cursor.delete(e.chainId);
});

export function invalidateFallback(chainId: number): void {
  for (const [key, prov] of providers) {
    if (key.startsWith(`${chainId}|`)) {
      prov.destroy();
      providers.delete(key);
    }
  }
  healthy.delete(chainId);
  cursor.set(chainId, (cursor.get(chainId) ?? 0) + 1);
}
