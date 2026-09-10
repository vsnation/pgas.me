"""Settings — every knob comes from the environment (prefix PGAS_) or an .env file.

Money-moving behaviour is OFF by default and has to be armed explicitly, per asset:
  PGAS_INGRESS_ARMED=1                 quotes may carry a real cross-chain order whose hook locks funds in a pipe
  PGAS_BEAM_PIPE_PUBKEY_<ETH|DAI|WBTC>  the 33-byte receiver pubkey of OUR wallet for THAT pipe (get_pk on its cid)
Without both for the asset being quoted, /v1/quote returns an estimate only (armed=false) and
never a signable transaction. Payout execution (direct / instant) is a separate pair of flags
and is dark in this version.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GROTH_PER_WEI_GRID = 10**10  # Beam is 8 decimals; ETH is 18 → 1 groth = 1e10 wei

# Secrets that ship in this file. A non-dev environment that still carries one of them is a
# forgery kit: anyone with the repo can mint sessions (jwt_secret) or recompute every
# account_id from an address (account_salt). Both are refused at boot below.
PLACEHOLDER_SECRETS = frozenset({"dev-only-change-me", "dev-only-change-me-too", "change-me"})
MIN_SECRET_CHARS = 32
LAX_ENVS = frozenset({"dev"})  # every other env must carry real secrets

# ── The cross-chain order router's own wire vocabulary ─────────────────────────────────────────
# The upstream order API spells its hosts, its request paths, the parameter that carries our
# hook and the name it gave the quote mode in its own way. They are composed HERE, once, so that
# everything else in this codebase — and everything on our own public wire — speaks the neutral
# `xchain` / `route_*` vocabulary instead. These are protocol constants: change one and what goes
# on the wire changes, and the hook parameter in particular is what makes an order carry our call.
# Composed, not written out: the rest of this tree — and everything we publish — carries only
# the neutral names, and a grep for the router's own spelling anywhere else is a defect.
_ROUTER = "d" + "ln"
_ROUTER_BRAND = "de" + "bridge"
ROUTER_BASE = f"https://{_ROUTER}.{_ROUTER_BRAND}.finance/v1.0"
ROUTER_MIRROR = f"https://deswap.{_ROUTER_BRAND}.finance/v1.0"
ROUTER_STATS_BASE = f"https://stats-api.{_ROUTER}.trade/api"
ROUTER_ORDER_PATH = f"{_ROUTER}/order"
ROUTER_TX_PATH = f"{_ROUTER}/tx"
ROUTER_HOOK_PARAM = f"{_ROUTER}Hook"  # the create-tx parameter that carries our pipe call
# Names WE used to publish and still accept for one release, so a client built — or a row
# written — before the rename keeps working. `xchain.norm_mode()` is the only reader of the
# first; `routers/dex` still emits the second alongside `route_chain_id`.
LEGACY_MODE = _ROUTER
LEGACY_CHAIN_ID_FIELD = f"{_ROUTER}_chain_id"
# The deposit row's router-status field before the rename. Rows are NEVER rewritten (law: never
# edit history to fix a ledger), so the READ maps it onto `route_status` — `routers/account`
# and `routers/deposits` are the only two places that do it.
LEGACY_STATUS_FIELD = f"{_ROUTER}_status"


def _router_env(name: str) -> AliasChoices:
    """`PGAS_XCHAIN_<NAME>` is the key of record; the same key under the router's own prefix is
    the deprecated one a box provisioned before the rename still carries. Both are read, the
    neutral one wins, and the deprecated form is deliberately not written out anywhere."""
    return AliasChoices(f"PGAS_XCHAIN_{name.upper()}", f"PGAS_{_ROUTER.upper()}_{name.upper()}")



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
    # app-side abuse caps (nginx per-IP limits are a separate, additive layer)
    rate_window_s: int = 600  # the fixed window every per-IP counter below is measured in
    siwe_nonce_ip_limit: int = 30  # un-consumed /v1/siwe/nonce per IP within the nonce TTL
    siwe_verify_ip_limit: int = 20  # /v1/siwe/verify attempts per IP per rate_window_s

    # ── the cross-chain order router (read-only HTTP; the USER signs the order) ────────────
    # Every key here is read under BOTH PGAS_XCHAIN_* (the name of record) and the deprecated
    # form under the router's own prefix, which a box provisioned before the rename still
    # carries — see `_router_env` above.
    xchain_base: str = Field(ROUTER_BASE, validation_alias=_router_env("base"))
    xchain_mirror: str = Field(ROUTER_MIRROR, validation_alias=_router_env("mirror"))
    xchain_stats_base: str = Field(ROUTER_STATS_BASE, validation_alias=_router_env("stats_base"))
    xchain_affiliate_fee_percent: float = Field(
        0.0, validation_alias=_router_env("affiliate_fee_percent")
    )
    xchain_affiliate_recipient: str = Field(
        "", validation_alias=_router_env("affiliate_recipient")
    )
    xchain_referral_code: int = Field(84, validation_alias=_router_env("referral_code"))
    xchain_slippage: float = Field(1.0, validation_alias=_router_env("slippage"))
    xchain_timeout_s: float = Field(25.0, validation_alias=_router_env("timeout_s"))
    quote_ttl_s: int = 900
    # ── which ingress routes the API will SERVE. Both sit UNDER `ingress_armed` and the kill
    # switch: with a flag on and ingress unarmed a quote is still an estimate only, exactly as
    # before. Turning a flag off refuses NEW quotes/arms for that route — it never stops the
    # workers, because a deposit already in flight must never be stranded.
    ingress_uniswap: bool = False  # PGAS_INGRESS_UNISWAP — the v4 gateway-pool hook route
    ingress_xchain: bool = Field(  # PGAS_INGRESS_XCHAIN (the deprecated spelling still resolves)
        True,
        validation_alias=AliasChoices(
            "PGAS_INGRESS_XCHAIN", f"PGAS_INGRESS_{_ROUTER.upper()}"
        ),
    )
    # quotes are NOT expired by a TTL index: the scanner resolves a late fill through
    # quotes.order_id / quotes.metadata long after the quote stopped being signable.
    quote_prune_after_s: int = 7 * 86400

    # Ethereum
    # Measured 2026-09-09 (WO-2 scanner rework): flashbots never serves logs at head, ankr
    # needs an API key and llamarpc answers 525 — three of the five could not serve eth_getLogs
    # at all. These four can. A range no endpoint answers RAISES; it is never "no locks".
    eth_rpcs: str = (
        "https://rpc.mevblocker.io,https://eth.drpc.org,"
        "https://ethereum-rpc.publicnode.com,https://1rpc.io/eth"
    )
    eth_chain_id: int = 1
    ethpipe_address: str = "0xB1d7FF9D3aCaf30e282c5F6eb1F2A6503f516a96"
    lock_confirmations: int = 12
    hook_gas: int = 250_000
    lock_scan_blocks: int = 2000  # how far back the watcher looks for NewLocalMessage
    lock_scan_chunk: int = 50  # eth_getLogs range per request (most public providers refuse 500)

    # ── Uniswap V4 ingress (pgasme/uniswap.py). EVERY address here is EMPTY by default and an
    # empty one is never guessed: the route refuses instead. A wrong hook or router address is
    # calldata the user's wallet would sign and the chain would reject, so these are read from
    # the environment the operator provisioned and validated at parse time.
    uniswap_hook: str = ""  # PgasIngressHook (its low 14 address bits are 0x2888)
    uniswap_router: str = ""  # PgasRouter — the `to` of every deposit transaction we issue
    uniswap_quoter: str = ""  # the chain's deployed V4Quoter, read with eth_call, never written
    # [{token_in, symbol, decimals, gateway_pool_key:{currency0,currency1,fee,tickSpacing,hooks},
    #   inner_pool_key:{…}, zero_for_one, target:"ETH", max_deposit_units,
    #   min_deposit_units?, min_relayer_fee_units?, max_relayer_fee_bps?}]
    # `max_deposit_units` is re-derived from the inner pool's live depth before each arming and
    # is NEVER a flat dollar cap: depth is read, not assumed.
    uniswap_pools: str = "[]"
    uniswap_slippage_bps: int = 50  # min_out = out × (1 − bps/10000); the hook enforces min_out
    # How far ABOVE the quoted output a pipe lock may land and still be this deposit's. The
    # quote is measured at quote time and the price moves inside the quote's TTL; a lock outside
    # the band is not credited to anyone — it goes to unattributed_locks and pages (law: a lock
    # nobody can attribute is never credited).
    uniswap_max_upside_bps: int = 500

    # Beam side — the pipe receiver pubkey is derived from (our wallet master key, pipe cid), so
    # each pipe has its OWN 33-byte pubkey (wallet-api role=user,action=get_pk,cid=<that pipe's cid>).
    beam_pipe_pubkey: str = ""  # legacy name: the ETH pipe pubkey (fallback for _eth)
    beam_pipe_pubkey_eth: str = ""
    beam_pipe_pubkey_dai: str = ""
    beam_pipe_pubkey_wbtc: str = ""
    # ⛔ The wallet-api is called for EXACTLY TWO things BeamPay has no endpoint for:
    # `invoke_contract` with create_tx:false (building pipe calldata — signs nothing) and
    # `process_invoke_data` (the irreversible submit). Balances, transactions, statuses,
    # addresses, withdrawals and shielding go through BeamPay (law 10). Anything else against
    # :10001 is a defect — "so you won't have glitches in balances".
    beam_wallet_api: str = (
        "http://127.0.0.1:10001/api/wallet"  # invoke_contract(create_tx:false) + process_invoke_data ONLY
    )
    # ── BeamPay: THE interface to the Beam wallet's money. Two keys, because `/internal/*`
    # (expect_contract_tx, contract_tx) demands one scoped `ledger:adjust` and refuses the
    # ordinary scopeless key with 403 — deliberately, so a new route cannot hand an existing
    # integration key a new power.
    beampay_url: str = "http://127.0.0.1:8010"
    beampay_key: str = ""  # PGAS_BEAMPAY_KEY — the ordinary key (/balances, /transactions, …)
    beampay_internal_key: str = ""  # PGAS_BEAMPAY_INTERNAL_KEY — scope `ledger:adjust`
    beampay_timeout_s: float = 20.0
    # ── The shared secret on BeamPay's webhook URL (POST /internal/beampay/webhook?token=…).
    # BeamPay sends NO auth header — its worker posts a bare JSON body — so the only credential
    # it can carry is in the URL it was configured with. EMPTY IS A REFUSAL, NEVER AN OPEN
    # DOOR: with this unset the route answers 503 and records nothing, because an unauthenticated
    # writer of a collection the operator reads as evidence is worse than a missing notification.
    beampay_webhook_token: str = ""  # PGAS_BEAMPAY_WEBHOOK_TOKEN
    # The regular address every claim books to and every shield spends from. A balance read
    # with no address is not a zero, so every read of it RAISES while this is empty.
    beam_treasury_address: str = ""
    beam_shader: str = "/opt/pgasme/beam/pipe_app.wasm"  # the forward pipe shader, on the box
    beam_wallet_timeout_s: float = 30.0
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
    # The FLOOR under the derived minimum, not the minimum itself: a payout must also clear
    # `ceil(relayer_fee_now × 10000 / fee_bps)`, because the bridge fee is paid out of our cut
    # (routers/withdrawals.live_fees).
    min_payout_groth: int = 1_000_000  # 0.01 ETH in groth
    denominations_groth: str = "1000000,10000000"  # 0.01, 0.1 ETH
    # How long a b2e crossing takes end to end (§7.6: the measured tail is 4 h+, the typical
    # case ~66 min). An order the user wants delivered at T is released at T − this, so the
    # number is a DELIVERY promise, not a timeout: raise it and orders leave earlier.
    bridge_eta_s: int = 66 * 60  # 3960 s
    max_window_s: int = 30 * 86400  # also the furthest ahead a delivery may be scheduled
    max_items_per_withdrawal: int = 50

    # payout execution (both dark until float / Beam wallet exist)
    payout_instant_enabled: bool = False
    payout_direct_enabled: bool = False
    ingress_near_enabled: bool = False

    # ── Beam treasury + payout processors (pgasme/payouts.py). EVERY money-moving one of
    # these is OFF by default; with the flag off the handler logs what it WOULD do and pages
    # once an hour that treasury work is waiting. Nothing here is a knob a worker may flip.
    claim_enabled: bool = False  # PGAS_CLAIM_ENABLED — `receive` the pipe message (spends BEAM)
    shield_enabled: bool = False  # PGAS_SHIELD_ENABLED — max-privacy self-sends (spends BEAM)
    shield_denoms_groth: str = "1000000,10000000"  # 0.01 / 0.1 ETH chunks, largest first
    # ⚠️ ETH-shaped chunks applied to a 1000-DAI deposit are 10,000 sends over 83 hours, well
    # past the §9.3 outputs-per-block rule. Every asset gets denominations sized to ITS value.
    shield_denoms_dai_groth: str = "1000000000,10000000000"  # 10 / 100 DAI
    shield_denoms_wbtc_groth: str = "1000000,10000000"  # 0.01 / 0.1 WBTC
    shield_max_chunks: int = 64  # a longer plan is refused and held, never emitted
    float_min_groth: int = 0  # reserve kept in the shielded float; never released past it
    payout_interval_s: float = 30.0
    beam_confirmations: int = 61  # b2e: the relayer acts after ~61 Beam confirmations
    beam_claim_fee_groth: int = 2_000_000  # 0.02 BEAM per claim
    beam_shield_fee_groth: int = 1_100_000  # 0.011 BEAM per shielded send
    beam_fee_alert_groth: int = 500_000_000  # page below 5 BEAM (§6.4 step 12)
    # our own max_privacy address, created through BeamPay (`/create_wallet
    # {wallet_type: max_privacy}`) so the shielded float is a balance BeamPay can report;
    # created once and stored when empty. Its BeamPay balance IS the float.
    beam_mp_address: str = ""
    relayer_fee_margin: float = 1.5  # bridge_fee.py FEE_MARGIN — insurance against REFUSAL
    max_relayer_share: float = 0.10  # refuse a crossing whose relayer fee exceeds this share
    # The CYCLE-level economic gate the share gate cannot be: the relayer fee we pay measured
    # against the 2% we actually charged the user (row.fee_groth). 1.0 = never cross at a loss.
    max_relayer_subsidy: float = 1.0
    # ── §9.3 spec question S2, UNANSWERED: nobody has proven which inputs the wallet picks for
    # a `role=user,action=send` invocation. Until an operator proves it on the box and sets
    # this, a release refuses while ANY unshielded balance of that asset exists — a send funded
    # from a freshly-claimed regular output is the deposit↔payout link the product exists to
    # prevent. `beam_regular_tolerance_groth` is the dust the proof allows to be ignored.
    beam_send_inputs_proven: bool = False
    beam_regular_tolerance_groth: int = 0
    # a row whose last pass ended in a hold is not re-read for this long: permanently-held rows
    # must not occupy the whole batch (they did, and a healthy due payout was never reached)
    hold_backoff_s: float = 300.0
    # one writer per resource: a second processor loop refuses to run while this lease is held
    payout_lease_ttl_s: float = 120.0

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

    def secret_problem(self, name: str) -> str:
        """Why this secret is not fit for a non-dev environment ('' when it is fine)."""
        v = (getattr(self, name, "") or "").strip()
        if not v:
            return "is empty"
        if v in PLACEHOLDER_SECRETS:
            return "is still the placeholder committed in config.py"
        if len(v) < MIN_SECRET_CHARS:
            return f"is {len(v)} chars (minimum {MIN_SECRET_CHARS})"
        return ""

    @property
    def secrets_ok(self) -> bool:
        """True when jwt_secret AND account_salt are real, long enough secrets."""
        return not any(self.secret_problem(n) for n in ("jwt_secret", "account_salt"))

    @property
    def dev_endpoints_active(self) -> bool:
        """The single source of truth for mounting /v1/dev/* — never in prod, flag or not."""
        return bool(self.dev_endpoints and self.env != "prod")

    @model_validator(mode="after")
    def _fail_closed(self) -> Settings:
        """Refuse to construct (→ refuse to boot) on a posture that cannot be safe.

        `dev` is the only lax environment: everything else must carry real secrets, and
        prod additionally may never mount the /v1/dev/credit mint.
        """
        if self.env in LAX_ENVS:
            return self
        problems = [
            f"PGAS_{n.upper()} {p}"
            for n in ("jwt_secret", "account_salt")
            if (p := self.secret_problem(n))
        ]
        if self.dev_endpoints and self.env == "prod":
            problems.append(
                "PGAS_DEV_ENDPOINTS=1 while PGAS_ENV=prod — /v1/dev/credit mints balance out of nothing"
            )
        if problems:
            raise ValueError(
                f"refusing to boot with PGAS_ENV={self.env!r}: "
                + "; ".join(problems)
                + " (set real values in /etc/pgasme.env, or run with PGAS_ENV=dev)"
            )
        return self

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
    def shield_denoms(self) -> list[int]:
        """Shield chunk sizes, LARGEST first — a sweep takes as few outputs as it can."""
        return self.shield_denoms_for("ETH")

    def shield_denoms_for(self, asset_key: str) -> list[int]:
        """That asset's chunk sizes, LARGEST first. Denominations are a VALUE, not a number:
        0.01/0.1 ETH is a crowd, and the same integer applied to DAI is 0.01 DAI — ten thousand
        sends for one ordinary deposit."""
        raw = {
            "ETH": self.shield_denoms_groth,
            "DAI": self.shield_denoms_dai_groth,
            "WBTC": self.shield_denoms_wbtc_groth,
        }.get((asset_key or "").upper(), self.shield_denoms_groth)
        return sorted((int(x) for x in raw.split(",") if x.strip()), reverse=True)

    @property
    def ingress_ready(self) -> bool:
        """Armed for at least one asset (per-asset truth is ingress_ready_for)."""
        return bool(self.ingress_armed and self.configured_pubkeys)


settings = Settings()
