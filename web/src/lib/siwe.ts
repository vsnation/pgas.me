// The EIP-4361 sign-in message, verbatim from API_CONTRACT.md. Whitespace matters: the backend
// recovers the signer from exactly this text.
//
// This is the ONLY message the app ever asks a wallet to sign. The destination-proof template that
// used to live here went with the Wallets page (2026-09-09): a payout address is typed, not proved.

export interface SiweParams {
  host: string;
  origin: string;
  address: string; // EIP-55
  statement: string;
  chainId: number;
  nonce: string;
  issuedAt: string; // ISO-8601
}

export function buildSiweMessage(p: SiweParams): string {
  return (
    `${p.host} wants you to sign in with your Ethereum account:\n` +
    `${p.address}\n` +
    `\n` +
    `${p.statement}\n` +
    `\n` +
    `URI: ${p.origin}\n` +
    `Version: 1\n` +
    `Chain ID: ${p.chainId}\n` +
    `Nonce: ${p.nonce}\n` +
    `Issued At: ${p.issuedAt}`
  );
}
