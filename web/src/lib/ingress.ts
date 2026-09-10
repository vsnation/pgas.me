// Which ingress paths the API has open, and which tokens the Uniswap one is registered for.
//
// The API is the ONE writer of this fact. It publishes it on the public reference reads —
// `GET /v1/dex/assets` carries `ingress:{uniswap, xchain, direct, uniswap_tokens:[…]}` and
// `GET /v1/health` carries `ingress:{uniswap, xchain, direct}` — and `/v1/account` carries
// `uniswap`/`xchain` inside its own `ingress`. Every one of those is the SAME fact, so it is read in
// exactly one place — here — and every caller reads the answer rather than its own copy.
//
// **A flag the API does not state is a flag that is off** (operator, 2026-09-10). The hook, the
// gateway pools and the routes are deployed by a human; a client that assumes the Uniswap path is
// open would offer "Pay with Uniswap V4" against a hook that does not exist yet. `xchain` keeps the
// other default because that path has been live and stated for weeks, and a build that has not
// learned to publish flags is still running it.
//
// Tolerance is the rest of the point. An older API answers `/health` with `ingress_armed` and no
// `ingress` object at all, or 404s the route entirely; a newer one may add keys this client has
// never heard of. Neither is an error, and neither is evidence about a path it did not name.

export interface IngressFlags {
  /** the Uniswap V4 gateway pool + Pgas hook: one transaction on Ethereum */
  uniswap: boolean;
  /** the cross-chain order: paying from a chain that is not Ethereum */
  xchain: boolean;
}

/**
 * Which ingress the API would pick for itself when both are open (`PGAS_INGRESS_DEFAULT_ROUTE`,
 * T31 D1). It is a PREFERENCE, not a flag: it says nothing about whether either path is open.
 */
export type IngressRoute = 'xchain' | 'uniswap';

/** What one payload actually STATES. A key nobody states is absent — never `true`, never `false`. */
export type IngressPartial = Partial<IngressFlags> & { default_route?: IngressRoute };

/**
 * What a path is when nobody states it. `uniswap` off: the API is the only thing that knows whether
 * the hook is deployed, and silence is not a yes. `xchain` on: it predates the flags entirely.
 */
export const INGRESS_DEFAULT: IngressFlags = { uniswap: false, xchain: true };

const FLAG_KEYS = ['uniswap', 'xchain'] as const;

/**
 * The tokens the gateway pools are registered for, used ONLY when the API states the flag without
 * naming a list. When it sends `uniswap_tokens`, that list is the truth and this one is not read.
 */
export const UNISWAP_DEFAULT_TOKENS = ['ETH', 'WETH', 'USDC', 'USDT', 'DAI', 'WBTC'];

function asObject(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' ? (v as Record<string, unknown>) : null;
}

/**
 * The `ingress` objects on a payload, in the order they are trusted: its own `ingress`, the
 * `modes.ingress` an older build used, and — for the flags only — the payload itself, so a caller
 * that already holds a flags object can pass it straight in.
 */
function ingressObjects(src: unknown, includeSelf = true): Record<string, unknown>[] {
  const o = asObject(src);
  if (!o) return [];
  const out: Record<string, unknown>[] = [];
  for (const v of [o.ingress, asObject(o.modes)?.ingress, ...(includeSelf ? [o] : [])]) {
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
    if (out.default_route === undefined && (o.default_route === 'xchain' || o.default_route === 'uniswap')) {
      out.default_route = o.default_route;
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// The route toggle (T31 D2)
// ---------------------------------------------------------------------------
// Two ingresses open at once is the first time this client has had to CHOOSE. Three facts, and one
// reader each: what the API prefers (`ingress.default_route`), what the user last picked (stored
// here), and what this pair can actually take (Ethereum, and a registered Uniswap pair).

const ROUTE_KEY = 'pgas.route.v1';

/** The user's own choice, or null when they have never made one (or storage is unavailable). */
export function storedRoute(): IngressRoute | null {
  try {
    const v = localStorage.getItem(ROUTE_KEY);
    return v === 'xchain' || v === 'uniswap' ? v : null;
  } catch {
    return null;
  }
}

export function storeRoute(route: IngressRoute): void {
  try {
    localStorage.setItem(ROUTE_KEY, route);
  } catch {
    // storage unavailable: the choice lives for this page only
  }
}

/**
 * Which route the page starts on: the user's own choice, else the API's stated default, else the
 * path this client has always led with when it is open.
 *
 * ⚠️ An API that states NO default is not saying "cross-chain": it is a build from before the
 * setting existed, and every one of those led with Uniswap the moment the flag was on. Silence
 * leaves behaviour where it was — the same rule as the flags above, applied to a preference.
 */
export function initialRoute(stated: IngressRoute | null | undefined, stored: IngressRoute | null): IngressRoute {
  return stored ?? stated ?? 'uniswap';
}

/** First source that states a flag wins; a flag nobody states takes its default above. */
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
 * The tokens the Uniswap route is registered for. The live shape is `ingress.uniswap_tokens` as
 * `[{address, symbol, decimals}]`; a list of plain strings is read the same way. Read only from an
 * `ingress` object — never from the payload's own keys, where `tokens` means something else.
 * Null means "we were told nothing", never "there are none".
 */
export function uniswapTokenList(src: unknown): string[] | null {
  for (const o of ingressObjects(src, false)) {
    for (const key of ['uniswap_tokens', 'uniswap_pairs', 'pairs', 'tokens']) {
      const v = o[key];
      if (!Array.isArray(v) || v.length === 0) continue;
      const list = v
        .map((e) => {
          if (typeof e === 'string') return e;
          const obj = asObject(e);
          // address first: it is the identity, and two chains' "USDC" are not one token
          const first = [obj?.address, obj?.token, obj?.symbol].find((x) => typeof x === 'string');
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
