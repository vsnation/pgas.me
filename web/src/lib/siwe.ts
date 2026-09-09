// Message templates, verbatim from API_CONTRACT.md. Whitespace matters: the backend recovers
// the signer from exactly this text.

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

export interface DestinationProofParams {
  accountId: string;
  address: string; // EIP-55
  nonce: string;
  issued: string; // ISO-8601
}

export function buildDestinationProof(p: DestinationProofParams): string {
  return `Pgas.me destination\n` + `account: ${p.accountId}\n` + `address: ${p.address}\n` + `nonce: ${p.nonce}\n` + `issued: ${p.issued}`;
}
