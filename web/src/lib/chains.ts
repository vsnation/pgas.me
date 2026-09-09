// Chain constants for the client-side scanner and wallet chain switching, plus the health-checked
// public RPC fallbacks. Ported from buybeam.my: batch-balance contracts, ordered public endpoints
// (the first entry takes every health check, so the most tolerant goes first), CoinGecko ids and
// deBridge's chain icons.
import { JsonRpcProvider, Network } from 'ethers';

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
 * deBridge lists two non-EVM chains. They are not errors and not "unreachable" — there is simply no
 * eth_* RPC to ask, so the scanner skips them and says so.
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
 * carry 3,808 bytes of code on every DLN chain without a batch-balance contract (4663, 1514, 25,
 * 999, 1776, 143, 4326). The scanner uses it for ERC-20 balances there; if the call ever fails it
 * falls back to per-token balanceOf.
 */
export const MULTICALL3 = '0xcA11bde05977b3631167028862bE2a173976CA11';

// Verified 2026-09-09, twice per URL: (a) curl eth_chainId returns this chain's id, (b) fetch() from
// a page on http://127.0.0.1:4173 succeeds (CORS). Only URLs passing BOTH are listed. Every DLN EVM
// chain has at least two. Dropped for CORS: eth.merkle.io (1), op-pokt.nodies.app (10). Dropped for
// auth/rate limits: rpc.ankr.com/*, *.llamarpc.com, bsc.drpc.org, polygon-rpc.com, story.drpc.org,
// injective.drpc.org, monad-rpc.publicnode.com, rpc.arrowrpc.com.
const FALLBACK_RPCS: Record<number, string[]> = {
  // ---- the 15 EVM chains deBridge/DLN offers today ----
  // rpc.flashbots.net is NOT here: it answers eth_chainId and eth_getCode and then refuses eth_call
  // with "rpc method is not whitelisted" — it is a transaction relay, not a reader.
  1: [
    'https://ethereum-rpc.publicnode.com',
    'https://eth.drpc.org',
    'https://cloudflare-eth.com',
    'https://1rpc.io/eth',
    'https://rpc.mevblocker.io',
  ],
  10: ['https://mainnet.optimism.io', 'https://optimism-rpc.publicnode.com', 'https://optimism.drpc.org', 'https://1rpc.io/op'],
  56: [
    'https://bsc-dataseed.binance.org',
    'https://bsc-dataseed1.binance.org',
    'https://bsc-dataseed2.binance.org',
    'https://bsc-dataseed3.binance.org',
    'https://bsc-dataseed4.binance.org',
    'https://bsc-rpc.publicnode.com',
    'https://bsc-dataseed1.defibit.io',
    'https://bsc-dataseed1.ninicoin.io',
  ],
  137: ['https://polygon-bor-rpc.publicnode.com', 'https://polygon.drpc.org', 'https://1rpc.io/matic'],
  4663: ['https://rpc.mainnet.chain.robinhood.com', 'https://robinhood-rpc.publicnode.com'],
  8453: ['https://mainnet.base.org', 'https://base-rpc.publicnode.com', 'https://base.drpc.org', 'https://1rpc.io/base'],
  42161: ['https://arb1.arbitrum.io/rpc', 'https://arbitrum-one-rpc.publicnode.com', 'https://arbitrum.drpc.org', 'https://1rpc.io/arb'],
  43114: [
    'https://api.avax.network/ext/bc/C/rpc',
    'https://avalanche-c-chain-rpc.publicnode.com',
    'https://avalanche.drpc.org',
    'https://1rpc.io/avax/c',
  ],
  59144: ['https://rpc.linea.build', 'https://linea-rpc.publicnode.com', 'https://linea.drpc.org', 'https://1rpc.io/linea'],
  1514: ['https://mainnet.storyrpc.io', 'https://story-mainnet-evm.itrocket.net', 'https://evm-rpc.story.mainnet.dteam.tech'],
  25: [
    'https://evm.cronos.org',
    'https://cronos-evm-rpc.publicnode.com',
    'https://cronos.drpc.org',
    'https://1rpc.io/cro',
    'https://rpc.vvs.finance',
  ],
  999: [
    'https://rpc.hyperliquid.xyz/evm',
    'https://hyperliquid.drpc.org',
    'https://rpc.hyperlend.finance',
    'https://hyperliquid-json-rpc.stakely.io',
  ],
  1776: ['https://sentry.evm-rpc.injective.network', 'https://injectiveevm-rpc.polkachu.com'],
  143: ['https://rpc.monad.xyz', 'https://monad.drpc.org'],
  4326: ['https://mainnet.megaeth.com/rpc', 'https://megaeth.rpc.thirdweb.com', 'https://megaeth.drpc.org'],
  // ---- not on the DLN list today: kept so a chain deBridge adds later is not blind (unverified) ----
  250: ['https://rpc.ftm.tools', 'https://fantom-rpc.publicnode.com'],
  100: ['https://rpc.gnosischain.com', 'https://gnosis-rpc.publicnode.com'],
  324: ['https://mainnet.era.zksync.io'],
  146: ['https://rpc.soniclabs.com'],
  80094: ['https://rpc.berachain.com'],
  1329: ['https://evm-rpc.sei-apis.com'],
  5000: ['https://rpc.mantle.xyz'],
  2741: ['https://api.mainnet.abs.xyz'],
  130: ['https://mainnet.unichain.org'],
  1868: ['https://rpc.soneium.org'],
  57073: ['https://rpc-gel.inkonchain.com'],
  480: ['https://worldchain-mainnet.g.alchemy.com/public'],
  9745: ['https://rpc.plasma.to'],
};

// CoinGecko asset-platform ids, read from GET /api/v3/asset_platforms by chain_identifier
// (2026-09-09): every DLN EVM chain has one.
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

// buybeam.my's CHAIN_ICON_NAMES + getChainIcon, verified 2026-09-09 (every name below answers 200
// image/svg+xml). Story has no chain asset on the CDN, so its badge is simply not shown.
const CHAIN_ICON_NAMES: Record<number, string> = {
  1: 'eth',
  10: 'optimism',
  56: 'bsc',
  137: 'polygon',
  250: 'fantom',
  4663: 'robinhood',
  8453: 'base',
  42161: 'arbitrum',
  43114: 'avalanche',
  59144: 'linea',
  25: 'cronos',
  999: 'hyperliquid',
  1776: 'injective',
  143: 'monad',
  4326: 'mega-eth',
};

/** deBridge's chain icon, or null when the CDN has none (the chip then shows no badge). */
export function chainIconUrl(chainId: number): string | null {
  const name = CHAIN_ICON_NAMES[chainId];
  return name ? `https://app.debridge.com/assets/images/chain/${name}.svg` : null;
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

async function probeChainId(url: string, ms = 4000): Promise<number> {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), ms);
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'eth_chainId', params: [] }),
      signal: ctrl.signal,
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const j = (await res.json()) as { result?: string };
    if (typeof j.result !== 'string') throw new Error('no result');
    return parseInt(j.result, 16);
  } finally {
    clearTimeout(t);
  }
}

export function fallbackUrls(chainId: number): string[] {
  return FALLBACK_RPCS[chainId] ?? [];
}

async function healthyUrl(chainId: number): Promise<string | null> {
  if (healthy.has(chainId)) return healthy.get(chainId) ?? null;
  const running = inflight.get(chainId);
  if (running) return running;
  const p = (async () => {
    const urls = fallbackUrls(chainId);
    for (let i = cursor.get(chainId) ?? 0; i < urls.length; i++) {
      try {
        if ((await probeChainId(urls[i])) !== chainId) continue;
        healthy.set(chainId, urls[i]);
        cursor.set(chainId, i);
        return urls[i];
      } catch {
        // next URL
      }
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
