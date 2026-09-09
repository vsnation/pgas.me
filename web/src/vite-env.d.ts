/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** WalletConnect Cloud project id; empty → the WalletConnect row is shown as "not configured". */
  readonly VITE_WALLETCONNECT_PROJECT_ID?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
