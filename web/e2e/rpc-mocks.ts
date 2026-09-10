// T54's RPC doubles. `mocks.ts` answers only the FIRST endpoint of each chain — enough to prove a
// scan works, not enough to prove WHICH endpoint the browser chose. This file adds routes for every
// endpoint the app offers on the mocked chains and records the order they were called in, plus the
// three shapes a custom endpoint can have: one that is the chain it claims, one that answers for a
// different chain, and one that does not answer at all.
//
// It ADDS routes rather than editing the shared `mocks.ts` (one writer per resource): Playwright
// matches handlers in reverse registration order, so installing this after `blockExternal` and
// `MockRpc` is what makes it win.
import type { Page, Route } from '@playwright/test';
import { DEMO_HOLDINGS, MockRpc } from './mocks';

/**
 * The endpoints `src/lib/rpc.ts` offers for the five EVM chains the mock API lists, in its order.
 * The spec asserts the popup renders exactly this for Ethereum, so a change to the app's table
 * fails a test rather than silently drifting away from the double.
 */
export const ENDPOINTS: Record<number, string[]> = {
  1: [
    'https://ethereum-rpc.publicnode.com',
    'https://eth.drpc.org',
    'https://cloudflare-eth.com',
    'https://1rpc.io/eth',
    'https://rpc.mevblocker.io',
  ],
  42161: ['https://arb1.arbitrum.io/rpc', 'https://arbitrum-one-rpc.publicnode.com', 'https://arbitrum.drpc.org', 'https://1rpc.io/arb'],
  8453: ['https://mainnet.base.org', 'https://base-rpc.publicnode.com', 'https://base.drpc.org', 'https://1rpc.io/base'],
  1514: ['https://mainnet.storyrpc.io', 'https://story-mainnet-evm.itrocket.net', 'https://evm-rpc.story.mainnet.dteam.tech'],
  25: [
    'https://evm.cronos.org',
    'https://cronos-evm-rpc.publicnode.com',
    'https://cronos.drpc.org',
    'https://1rpc.io/cro',
    'https://rpc.vvs.finance',
  ],
};

/** A custom endpoint that really is Base. */
export const CUSTOM_OK = 'https://my-base-node.test/rpc';
/** A custom endpoint that answers — for Ethereum. Offered for Base, it must be refused. */
export const CUSTOM_WRONG_CHAIN = 'https://wrong-node.test/rpc';
/** A custom endpoint nothing answers at. */
export const CUSTOM_DEAD = 'https://dead-node.test/rpc';

export interface RpcHit {
  url: string;
  chainId: number;
  method: string;
}

function exact(url: string): RegExp {
  return new RegExp(`^${url.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}/?(\\?.*)?$`);
}

/**
 * Every endpoint of every mocked chain, answered from the SAME double `mocks.ts` uses (its
 * `answer` is private to TypeScript, not to the runtime — one implementation of the fixture, and a
 * cast rather than a copy of it), with every call recorded.
 */
export class RpcRecorder {
  readonly hits: RpcHit[] = [];
  private readonly rpc = new MockRpc(DEMO_HOLDINGS);

  async install(page: Page): Promise<void> {
    for (const [id, urls] of Object.entries(ENDPOINTS)) {
      for (const url of urls) await page.route(exact(url), (route) => this.answer(Number(id), url, route));
    }
    await page.route(exact(CUSTOM_OK), (route) => this.answer(8453, CUSTOM_OK, route));
    await page.route(exact(CUSTOM_WRONG_CHAIN), (route) => this.answer(1, CUSTOM_WRONG_CHAIN, route));
    await page.route(exact(CUSTOM_DEAD), (route) => route.abort('connectionrefused'));
  }

  reset(): void {
    this.hits.length = 0;
  }

  /** The endpoints this chain was read through, each once, in the order they were first called. */
  order(chainId: number): string[] {
    const seen: string[] = [];
    for (const h of this.hits) if (h.chainId === chainId && !seen.includes(h.url)) seen.push(h.url);
    return seen;
  }

  private answer(chainId: number, url: string, route: Route) {
    let body: unknown;
    try {
      body = JSON.parse(route.request().postData() ?? 'null');
    } catch {
      body = null;
    }
    const inner = this.rpc as unknown as { answer(chainId: number, method: string, params: unknown[]): unknown };
    const one = (r: { id?: unknown; method?: string; params?: unknown[] }) => {
      const id = r?.id ?? 1;
      this.hits.push({ url, chainId, method: String(r?.method) });
      try {
        return { jsonrpc: '2.0', id, result: inner.answer(chainId, String(r?.method), (r?.params ?? []) as unknown[]) };
      } catch (e) {
        return { jsonrpc: '2.0', id, error: { code: -32000, message: (e as Error).message } };
      }
    };
    const out = Array.isArray(body) ? body.map(one) : one(body as never);
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(out) });
  }
}
