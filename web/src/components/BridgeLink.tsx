// The bridge's own block explorer (admin, 2026-09-10: "Here is the link to block explorer of the
// bridge …?tx={block_height}, so user can track his withdrawals and funding his wallets").
//
// ⛔ IT KEYS ON THE BEAM BLOCK HEIGHT, not on a txid and not on a kernel id. That is the whole
// reason this is a component and not an inline `<a>`: the URL is built in ONE place, from ONE
// number, and that number comes from the API (`beam_height` on the public deposit and payout
// rows) rather than from anything this client derives. `format.ts` owns the Ethereum explorers
// (`explorerTx`) for the same reason; this owns the Beam one.
//
// ⚠️ A height nobody recorded is `null`, and a null draws NOTHING. There is no placeholder, no
// disabled link and no "pending" text: a link built on a height we guessed would point at some
// other block's bridge traffic and read to the user as evidence about their own money.

/** The base of the bridge explorer. One string, one file. */
const BRIDGE_EXPLORER = 'https://beamterminal.0xmx.net/#/explorer/bridge';

/**
 * The explorer URL for one Beam block height, or null when there is nothing to link to.
 *
 * Anything that is not a positive whole block is a null: 0 is a block (somebody else's), and a
 * string, a NaN or a negative is a row we cannot read. The API answers `null` in exactly these
 * cases too — this is the client refusing to invent one, not a second opinion about the number.
 */
export function bridgeExplorerUrl(height: number | null | undefined): string | null {
  if (typeof height !== 'number' || !Number.isFinite(height) || !Number.isInteger(height) || height <= 0) return null;
  return `${BRIDGE_EXPLORER}?tx=${height}`;
}

/**
 * "track on the bridge explorer ↗" — rendered only when the crossing has a Beam block.
 *
 * `label` is what the row calls the thing being tracked; the default suits both a deposit's claim
 * and a payout's crossing, which is the point of having one component for the two.
 */
export function BridgeLink({ height, label = 'track on the bridge explorer' }: { height?: number | null; label?: string }) {
  const href = bridgeExplorerUrl(height);
  if (!href) return null;
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="small"
      data-testid="bridge-link"
      title={`Beam block ${height}`}
      onClick={(e) => e.stopPropagation()} // the row it sits in toggles; the link does not
    >
      {label} ↗
    </a>
  );
}
