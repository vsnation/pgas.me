// Which ingress paths the API has open, and which tokens the Uniswap one is registered for.
//
// API_CONTRACT.md puts the flags on the public reference reads — `GET /v1/assets` and
// `GET /v1/health` carry `ingress:{uniswap,xchain}` — and a build that is behind may still carry
// them on the account instead (`ingress`, or `modes.ingress`). Every one of those is the SAME fact,
// so it is read in exactly one place — here — and every caller reads the answer rather than its own
// copy. A build that states nothing gets the defaults: both paths on, which is what the API itself
// does when no route is named (`route:"auto"` picks uniswap → direct → cross-chain).
//
// Tolerance is the point. An older API answers `/health` with `ingress_armed` and no `ingress`
// object at all; a newer one may add keys this client has never heard of. Neither is a reason to
// refuse a deposit, and neither is evidence that a path is closed — only an explicit `false` is.

export interface IngressFlags {
  /** the Uniswap V4 gateway pool + Pgas hook: one transaction on Ethereum */
  uniswap: boolean;
  /** the cross-chain order: paying from a chain that is not Ethereum */
  xchain: boolean;
}

/** What one payload actually STATES. A key nobody states is absent, never `false`. */
export type IngressPartial = Partial<IngressFlags>;

export const INGRESS_DEFAULT: IngressFlags = { uniswap: true, xchain: true };

const FLAG_KEYS = ['uniswap', 'xchain'] as const;

/** The token symbols the hackathon build registers a gateway pool for, when the API names none. */
export const UNISWAP_DEFAULT_TOKENS = ['ETH', 'WETH', 'USDC', 'USDT', 'DAI', 'WBTC'];

function asObject(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' ? (v as Record<string, unknown>) : null;
}

/** Every place an API build is known to put the ingress facts, in the order they are trusted. */
function ingressObjects(src: unknown): Record<string, unknown>[] {
  const o = asObject(src);
  if (!o) return [];
  const out: Record<string, unknown>[] = [];
  for (const v of [o.ingress, asObject(o.modes)?.ingress, o]) {
    const obj = asObject(v);
    if (obj) out.push(obj);
  }
  return out;
}

/** The flags one payload states — nothing is invented, so a silent build contributes nothing. */
export function ingressPartial(src: unknown): IngressPartial {
  const out: IngressPartial = {};
  for (const o of ingressObjects(src)) {
    for (const k of FLAG_KEYS) {
      if (out[k] === undefined && typeof o[k] === 'boolean') out[k] = o[k] as boolean;
    }
  }
  return out;
}

/** First source that states a flag wins; a flag nobody states takes its default (on). */
export function resolveIngress(...parts: (IngressPartial | null | undefined)[]): IngressFlags {
  const out: IngressFlags = { ...INGRESS_DEFAULT };
  for (const k of FLAG_KEYS) {
    for (const p of parts) {
      if (typeof p?.[k] === 'boolean') {
        out[k] = p[k] as boolean;
        break;
      }
    }
  }
  return out;
}

/**
 * The tokens the Uniswap route is registered for, as the API names them (symbols or addresses),
 * or null when it names none — null means "we were told nothing", never "there are none".
 */
export function uniswapTokenList(src: unknown): string[] | null {
  for (const o of ingressObjects(src)) {
    for (const key of ['uniswap_tokens', 'uniswap_pairs', 'pairs', 'tokens']) {
      const v = o[key];
      if (!Array.isArray(v) || v.length === 0) continue;
      const list = v
        .map((e) => {
          if (typeof e === 'string') return e;
          const obj = asObject(e);
          const first = [obj?.token, obj?.address, obj?.symbol].find((x) => typeof x === 'string');
          return typeof first === 'string' ? first : null;
        })
        .filter((s): s is string => !!s);
      if (list.length) return list;
    }
  }
  return null;
}

/**
 * Is this token one the Uniswap route takes? Matched on address when the list carries addresses and
 * on symbol when it carries symbols, so the same reader serves either shape. With no list from the
 * API the six tokens the gateway pools are registered for stand in.
 */
export function isUniswapToken(token: { address?: string; symbol?: string } | null | undefined, list: string[] | null): boolean {
  if (!token) return false;
  const names = (list ?? UNISWAP_DEFAULT_TOKENS).map((s) => s.toLowerCase());
  const address = token.address?.toLowerCase();
  const symbol = token.symbol?.toLowerCase();
  return names.some((n) => (n.startsWith('0x') ? n === address : n === symbol));
}
