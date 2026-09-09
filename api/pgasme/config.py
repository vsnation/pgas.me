"""Settings — every knob comes from the environment (prefix PGAS_) or an .env file.

Money-moving behaviour is OFF by default and has to be armed explicitly, per asset:
  PGAS_INGRESS_ARMED=1                 quotes may carry a real DLN transaction whose hook locks funds in a pipe
  PGAS_BEAM_PIPE_PUBKEY_<ETH|DAI|WBTC>  the 33-byte receiver pubkey of OUR wallet for THAT pipe (get_pk on its cid)
Without both for the asset being quoted, /v1/quote returns an estimate only (armed=false) and
never a signable transaction. Payout execution (direct / instant) is a separate pair of flags
and is dark in this version.
"""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GROTH_PER_WEI_GRID = 10**10  # Beam is 8 decimals; ETH is 18 → 1 groth = 1e10 wei


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="PGAS_", extra="ignore")

    env: str = "dev"
    host: str = "127.0.0.1"
    port: int = 8300
    workers_enabled: bool = True
    dev_endpoints: bool = False  # /v1/dev/* (seed a balance for tests) — never in prod
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    stop_file: str = "/etc/pgasme.stop"  # kill switch: workers pause while this file exists

    # storage
    mongo_url: str = "mongodb://127.0.0.1:27017/pgasme"

    # sign-in with Ethereum
    siwe_domains: str = "localhost:5173,127.0.0.1:5173,pgas.me,www.pgas.me"
    siwe_statement: str = "Sign in to Pgas.me. This signature costs nothing and moves nothing."
    jwt_secret: str = "dev-only-change-me"
    account_salt: str = "dev-only-change-me-too"
    session_ttl_s: int = 24 * 3600
    nonce_ttl_s: int = 300

    # deBridge DLN
    dln_base: str = "https://dln.debridge.finance/v1.0"
    dln_mirror: str = "https://deswap.debridge.finance/v1.0"
    dln_stats_base: str = "https://stats-api.dln.trade/api"
    dln_affiliate_fee_percent: float = 0.0
    dln_affiliate_recipient: str = ""
    dln_referral_code: int = 84
    dln_slippage: float = 1.0
    dln_timeout_s: float = 25.0
    quote_ttl_s: int = 900

    # Ethereum
    eth_rpcs: str = (
        "https://rpc.flashbots.net,https://ethereum-rpc.publicnode.com,"
        "https://eth.drpc.org,https://rpc.ankr.com/eth,https://eth.llamarpc.com"
    )
    eth_chain_id: int = 1
    ethpipe_address: str = "0xB1d7FF9D3aCaf30e282c5F6eb1F2A6503f516a96"
    lock_confirmations: int = 12
    hook_gas: int = 250_000
    lock_scan_blocks: int = 2000  # how far back the watcher looks for NewLocalMessage
    lock_scan_chunk: int = 500  # eth_getLogs range per request

    # Beam side — the pipe receiver pubkey is derived from (our wallet master key, pipe cid), so
    # each pipe has its OWN 33-byte pubkey (wallet-api role=user,action=get_pk,cid=<that pipe's cid>).
    beam_pipe_pubkey: str = ""  # legacy name: the ETH pipe pubkey (fallback for _eth)
    beam_pipe_pubkey_eth: str = ""
    beam_pipe_pubkey_dai: str = ""
    beam_pipe_pubkey_wbtc: str = ""
    beam_wallet_api: str = (
        "http://127.0.0.1:10001/api/wallet"  # the dedicated wallet-api (claim worker, later)
    )
    ingress_armed: bool = False
    min_relayer_fee_wei: int = (
        100_000_000_000  # 1e11 wei ≈ $0.0002 — above the 0.02-BEAM-equivalent floor
    )
    # ERC-20 pipes: the smallest relayerFee we ride on the tail, in token units (≥ 1 grid unit each)
    min_relayer_fee_dai_units: int = 200_000_000_000_000  # 0.0002 DAI ≈ 0.02 BEAM
    min_relayer_fee_wbtc_units: int = 1  # 1 sat (WBTC has no grid)

    # product economics
    fee_bps: int = 200  # 2% at unlock, charged to the balance
    min_deposit_wei: int = 20_000_000_000_000_000  # 0.02 ETH (ETH-equivalent for DAI/WBTC)
    min_payout_groth: int = 1_000_000  # 0.01 ETH in groth
    denominations_groth: str = "1000000,10000000"  # 0.01, 0.1 ETH
    max_window_s: int = 30 * 86400
    max_items_per_withdrawal: int = 50

    # payout execution (both dark until float / Beam wallet exist)
    payout_instant_enabled: bool = False
    payout_direct_enabled: bool = False
    ingress_near_enabled: bool = False

    # explorer for pool stats, prices
    explorer_base: str = "https://beamsmart.net:8000"
    coingecko_base: str = "https://api.coingecko.com/api/v3"

    # workers
    watcher_interval_s: float = 15.0
    monitor_interval_s: float = 60.0
    stats_interval_s: float = 60.0
    deposit_fallback_after_s: int = (
        1800  # Fulfilled but no lock log for this long → fallback_pending
    )

    @field_validator(
        "beam_pipe_pubkey", "beam_pipe_pubkey_eth", "beam_pipe_pubkey_dai", "beam_pipe_pubkey_wbtc"
    )
    @classmethod
    def _pubkey_hex(cls, v: str) -> str:
        v = (v or "").strip().lower().removeprefix("0x")
        if v and (len(v) != 66 or any(c not in "0123456789abcdef" for c in v)):
            raise ValueError("a Beam pipe pubkey must be 33 bytes (66 hex chars)")
        return v

    def pubkey_for(self, asset_key: str) -> str:
        """The receiver pubkey for one pipe ('' when not configured)."""
        k = (asset_key or "").upper()
        if k == "ETH":
            return self.beam_pipe_pubkey_eth or self.beam_pipe_pubkey
        if k == "DAI":
            return self.beam_pipe_pubkey_dai
        if k == "WBTC":
            return self.beam_pipe_pubkey_wbtc
        return ""

    @property
    def configured_pubkeys(self) -> dict[str, str]:
        return {k: pk for k in ("ETH", "DAI", "WBTC") if (pk := self.pubkey_for(k))}

    def ingress_ready_for(self, asset_key: str) -> bool:
        return bool(self.ingress_armed and self.pubkey_for(asset_key))

    @property
    def siwe_domain_set(self) -> set[str]:
        return {d.strip() for d in self.siwe_domains.split(",") if d.strip()}

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def eth_rpc_list(self) -> list[str]:
        return [u.strip() for u in self.eth_rpcs.split(",") if u.strip()]

    @property
    def denominations(self) -> list[int]:
        return sorted(int(x) for x in self.denominations_groth.split(",") if x.strip())

    @property
    def ingress_ready(self) -> bool:
        """Armed for at least one asset (per-asset truth is ingress_ready_for)."""
        return bool(self.ingress_armed and self.configured_pubkeys)


settings = Settings()
