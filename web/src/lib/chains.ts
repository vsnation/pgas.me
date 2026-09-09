// Chain constants for the client-side scanner and wallet chain switching, plus the health-checked
// public RPC fallbacks. Ported from buybeam.my: batch-balance contracts, ordered public endpoints
// (the first entry takes every health check, so the most tolerant goes first), CoinGecko ids.
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

/** balanceFor(address[] _tokens, address _account) view returns (uint256[] balances, uint256[] decimals) */
export const BATCH_BALANCE_CONTRACTS: Record<number, string> = {
  1: '0x50188692d5549386d102642036bab916b998c814',
  56: '0x50188692d5549386d102642036bab916b998c814',
  137: '0x50188692d5549386d102642036bab916b998c814',
  42161: '0x50188692d5549386d102642036bab916b998c814',
  10: '0x50188692d5549386d102642036bab916b998c814',
  8453: '0x202eF28cA6D4d2B94C4Ea0534a8E6261581c70a4',
  250: '0x55C93b20Dd2F790AC429D6341a022A781791654A',
  43114: '0x55C93b20Dd2F790AC429D6341a022A781791654A',
  59144: '0x0e4AdD4DC86Ae1Aa0FA43Bd7e6a9fB8Be2d5504d',
};

const FALLBACK_RPCS: Record<number, string[]> = {
  1: [
    'https://rpc.flashbots.net',
    'https://ethereum-rpc.publicnode.com',
    'https://eth.drpc.org',
    'https://rpc.ankr.com/eth',
    'https://eth.llamarpc.com',
  ],
  56: ['https://bsc-dataseed.binance.org', 'https://bsc-dataseed1.binance.org', 'https://bsc-rpc.publicnode.com'],
  137: ['https://polygon-rpc.com', 'https://polygon-bor-rpc.publicnode.com', 'https://rpc.ankr.com/polygon'],
  42161: ['https://arb1.arbitrum.io/rpc', 'https://arbitrum-one-rpc.publicnode.com', 'https://rpc.ankr.com/arbitrum'],
  10: ['https://mainnet.optimism.io', 'https://optimism-rpc.publicnode.com'],
  8453: ['https://mainnet.base.org', 'https://base-rpc.publicnode.com'],
  43114: ['https://api.avax.network/ext/bc/C/rpc', 'https://avalanche-c-chain-rpc.publicnode.com'],
  250: ['https://rpc.ftm.tools', 'https://fantom-rpc.publicnode.com'],
  59144: ['https://rpc.linea.build'],
  25: ['https://evm.cronos.org'],
  100: ['https://rpc.gnosischain.com', 'https://gnosis-rpc.publicnode.com'],
  324: ['https://mainnet.era.zksync.io'],
  146: ['https://rpc.soniclabs.com'],
  80094: ['https://rpc.berachain.com'],
  999: ['https://rpc.hyperliquid.xyz/evm'],
  1329: ['https://evm-rpc.sei-apis.com'],
  5000: ['https://rpc.mantle.xyz'],
  2741: ['https://api.mainnet.abs.xyz'],
  1514: ['https://mainnet.storyrpc.io'],
  130: ['https://mainnet.unichain.org'],
  1868: ['https://rpc.soneium.org'],
  57073: ['https://rpc-gel.inkonchain.com'],
  480: ['https://worldchain-mainnet.g.alchemy.com/public'],
  9745: ['https://rpc.plasma.to'],
};

export const COINGECKO_PLATFORMS: Record<number, string> = {
  1: 'ethereum',
  56: 'binance-smart-chain',
  137: 'polygon-pos',
  42161: 'arbitrum-one',
  10: 'optimistic-ethereum',
  8453: 'base',
  43114: 'avalanche',
  250: 'fantom',
  59144: 'linea',
};

export const COINGECKO_NATIVE_IDS: Record<number, string> = {
  1: 'ethereum',
  42161: 'ethereum',
  10: 'ethereum',
  8453: 'ethereum',
  59144: 'ethereum',
  56: 'binancecoin',
  137: 'matic-network',
  43114: 'avalanche-2',
  250: 'fantom',
  25: 'crypto-com-chain',
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
    rpcUrls: ['https://polygon-rpc.com'],
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
  25: {
    chainName: 'Cronos',
    nativeCurrency: { name: 'CRO', symbol: 'CRO', decimals: 18 },
    rpcUrls: ['https://evm.cronos.org'],
    blockExplorerUrls: ['https://cronoscan.com'],
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
// One healthy provider per chain is kept for the session; `invalidateFallback` moves to the next
// URL when it starts failing. A URL that answers for another chain is never used.
const healthy = new Map<number, JsonRpcProvider | null>();
const inflight = new Map<number, Promise<JsonRpcProvider | null>>();
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

export async function getFallbackProvider(chainId: number): Promise<JsonRpcProvider | null> {
  if (healthy.has(chainId)) return healthy.get(chainId) ?? null;
  const running = inflight.get(chainId);
  if (running) return running;
  const p = (async () => {
    const urls = fallbackUrls(chainId);
    for (let i = cursor.get(chainId) ?? 0; i < urls.length; i++) {
      try {
        if ((await probeChainId(urls[i])) !== chainId) continue;
        const net = new Network(`chain-${chainId}`, chainId);
        const prov = new JsonRpcProvider(urls[i], net, { staticNetwork: net, batchMaxCount: 1 });
        healthy.set(chainId, prov);
        cursor.set(chainId, i);
        return prov;
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

export function invalidateFallback(chainId: number): void {
  healthy.get(chainId)?.destroy();
  healthy.delete(chainId);
  cursor.set(chainId, (cursor.get(chainId) ?? 0) + 1);
}
