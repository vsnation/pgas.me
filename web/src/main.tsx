// Pgas.me web — React 18 + Vite + ethers v6, coded against API_CONTRACT.md.
//
// Structure
//   src/lib/types.ts      the API contract as TypeScript, field for field
//   src/lib/api.ts        fetch client for /api/v1 + the SIWE session token in localStorage
//   src/lib/siwe.ts       the two signed-message templates (sign-in, destination proof)
//   src/lib/wallet.ts     injected-wallet discovery (EIP-6963 + legacy globals + Farcaster)
//   src/lib/chains.ts     chain constants, batch-balance contracts, health-checked public RPCs
//   src/lib/portfolio.ts  the client-side balance scanner and CoinGecko pricing
//   src/lib/format.ts     amounts, addresses, times, explorer links
//   src/state/store.tsx   one provider: wallet connection, session + polled account, reference data, tab
//   src/components/       pieces shared by two or more pages (sign-in gate, status pills, balances)
//   src/pages/            one file per tab: Deposit, Balance, Wallets, Withdraw, Activity
//   src/App.tsx           chrome (header, wallet picker, stats footer) and the tab switch
//   src/styles.css        design tokens (light/dark) and the component styles
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './styles.css';

createRoot(document.getElementById('root') as HTMLElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
