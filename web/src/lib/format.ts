import { formatUnits, getAddress, parseUnits } from 'ethers';

export const GROTH = 100_000_000; // 1 asset unit = 1e8 groth

function grothToNumber(g: number | string | undefined | null): number {
  if (g === undefined || g === null) return 0;
  const n = typeof g === 'string' ? Number(g) : g;
  return Number.isFinite(n) ? n / GROTH : 0;
}

/** groth → "0.1234" (trims trailing zeros, keeps at least `minFrac`). */
export function fmtGroth(g: number | string | undefined | null, maxFrac = 6, minFrac = 2): string {
  return fmtNumber(grothToNumber(g), maxFrac, minFrac);
}

export function fmtNumber(n: number, maxFrac = 6, minFrac = 0): string {
  if (!Number.isFinite(n)) return '–';
  return n.toLocaleString('en-US', { maximumFractionDigits: maxFrac, minimumFractionDigits: minFrac });
}

/** raw units (bigint | decimal string) → human amount string with sensible precision */
export function fmtUnits(raw: bigint | string | number | undefined | null, decimals: number, maxFrac = 6): string {
  if (raw === undefined || raw === null || raw === '') return '–';
  try {
    const s = formatUnits(typeof raw === 'number' ? BigInt(Math.trunc(raw)) : BigInt(raw), decimals);
    const n = Number(s);
    if (Number.isFinite(n)) {
      if (n !== 0 && Math.abs(n) < 1 / 10 ** maxFrac) return `<${(1 / 10 ** maxFrac).toFixed(maxFrac)}`;
      return fmtNumber(n, maxFrac);
    }
    return s;
  } catch {
    return String(raw);
  }
}

export function fmtUsd(n: number | undefined | null): string {
  if (n === undefined || n === null || !Number.isFinite(n)) return '–';
  if (n >= 1000) return '$' + n.toLocaleString('en-US', { maximumFractionDigits: 0 });
  if (n >= 1) return '$' + n.toFixed(2);
  if (n >= 0.01) return '$' + n.toFixed(2);
  return n > 0 ? '<$0.01' : '$0.00';
}

export function shortAddr(a: string | undefined | null, head = 6, tail = 4): string {
  if (!a) return '';
  if (a.length <= head + tail + 2) return a;
  return `${a.slice(0, head)}…${a.slice(-tail)}`;
}

export function checksum(a: string): string {
  try {
    return getAddress(a);
  } catch {
    return a;
  }
}

/** Timestamps arrive as epoch seconds (backend `time.time()`), epoch ms, or ISO strings. */
export function toDate(v: number | string | undefined | null): Date | null {
  if (v === undefined || v === null || v === '') return null;
  if (typeof v === 'number') return new Date(v < 1e12 ? v * 1000 : v);
  const n = Number(v);
  if (Number.isFinite(n) && /^\d+(\.\d+)?$/.test(v)) return new Date(n < 1e12 ? n * 1000 : n);
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function fmtTime(v: number | string | undefined | null): string {
  const d = toDate(v);
  if (!d) return '–';
  return d.toLocaleString('en-GB', {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function fmtAgo(v: number | string | undefined | null): string {
  const d = toDate(v);
  if (!d) return '–';
  const s = Math.round((Date.now() - d.getTime()) / 1000);
  if (Math.abs(s) < 60) return s >= 0 ? `${s}s ago` : `in ${-s}s`;
  const m = Math.round(s / 60);
  if (Math.abs(m) < 60) return m >= 0 ? `${m} min ago` : `in ${-m} min`;
  const h = Math.round(m / 60);
  if (Math.abs(h) < 48) return h >= 0 ? `${h} h ago` : `in ${-h} h`;
  const days = Math.round(h / 24);
  return days >= 0 ? `${days} d ago` : `in ${-days} d`;
}

export function fmtDuration(s: number | undefined | null): string {
  if (s === undefined || s === null || !Number.isFinite(s)) return '–';
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) {
    const h = s / 3600;
    return `${h % 1 === 0 ? h : h.toFixed(1)} h`;
  }
  const d = s / 86400;
  return `${d % 1 === 0 ? d : d.toFixed(1)} d`;
}

/** "0.1" → raw units bigint, or null when not a valid positive number */
export function parseAmount(str: string, decimals: number): bigint | null {
  const s = str.trim().replace(/,/g, '');
  if (!s || !/^\d*\.?\d*$/.test(s) || s === '.') return null;
  try {
    const v = parseUnits(s, decimals);
    return v > 0n ? v : null;
  } catch {
    return null;
  }
}

/** "0.1" → groth (number), or null */
export function parseGroth(str: string): number | null {
  const raw = parseAmount(str, 8);
  if (raw === null) return null;
  const n = Number(raw);
  return Number.isSafeInteger(n) ? n : null;
}

export function hexValue(v: string | number | bigint | undefined | null): string | undefined {
  if (v === undefined || v === null || v === '') return undefined;
  if (typeof v === 'string' && v.startsWith('0x')) return v;
  try {
    return '0x' + BigInt(v).toString(16);
  } catch {
    return undefined;
  }
}

export function explorerTx(chainId: number | undefined, hash: string): string {
  const base: Record<number, string> = {
    1: 'https://etherscan.io/tx/',
    56: 'https://bscscan.com/tx/',
    137: 'https://polygonscan.com/tx/',
    42161: 'https://arbiscan.io/tx/',
    10: 'https://optimistic.etherscan.io/tx/',
    8453: 'https://basescan.org/tx/',
    43114: 'https://snowtrace.io/tx/',
    250: 'https://ftmscan.com/tx/',
    59144: 'https://lineascan.build/tx/',
    25: 'https://cronoscan.com/tx/',
    100: 'https://gnosisscan.io/tx/',
    324: 'https://era.zksync.network/tx/',
    146: 'https://sonicscan.org/tx/',
    80094: 'https://berascan.com/tx/',
    130: 'https://uniscan.xyz/tx/',
    1868: 'https://soneium.blockscout.com/tx/',
    5000: 'https://mantlescan.xyz/tx/',
  };
  return (base[chainId ?? 1] ?? 'https://etherscan.io/tx/') + hash;
}

export function explorerAddress(chainId: number, address: string): string {
  const base: Record<number, string> = {
    1: 'https://etherscan.io/address/',
    42161: 'https://arbiscan.io/address/',
    8453: 'https://basescan.org/address/',
  };
  return (base[chainId] ?? 'https://etherscan.io/address/') + address;
}
