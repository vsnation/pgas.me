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

# The ingress routes a client can be STEERED to by default (PGAS_INGRESS_DEFAULT_ROUTE, T31 D1).
# The two names are `xchain.MODE` and `uniswap.MODE` — both of those modules import this one, so
# the strings cannot be imported from them here; `tests/test_default_route.py` asserts that this
# tuple and those two constants can never drift apart (law 9: two spellings of one fact).
INGRESS_ROUTES = ("xchain", "uniswap")
DEFAULT_INGRESS_ROUTE = "xchain"

# ── The gas basis a bridge crossing is priced on (T45, 2026-09-10) ─────────────────────────────
# ⛔ ONE SAMPLE IS NOT A PRICE. `payouts.relayer_fee_for` reads `eth_feeHistory` once — base × 2
# plus the median tip over 10 blocks — and multiplies by `PGAS_RELAYER_FEE_MARGIN`. On
# 2026-09-10 mainnet gas went 0.66 → 2.18 gwei inside an hour: two orders funded 14,733 groth in
# the trough and d028 met 24,299 at the release. So the basis is `max(the live read, the p75 of
# the last GAS_BASIS_WINDOW_S of the `gas_samples` series)`, and the series is written by exactly
# one writer (the deposit watcher's pass, `payouts.record_gas_sample`).
#
# Constants and not knobs on purpose: they describe the SHAPE of the rule (a day's worth of
# evidence, kept for two days so an operator can still read what priced a crossing), and the two
# numbers that ARE product decisions — the subsidy and the headroom floor — are the env knobs
# below. The TTL is deliberately longer than the window: a series that expired exactly when it
# stopped counting would leave nothing to explain a charge with.
GAS_BASIS_WINDOW_S = 24 * 3600
GAS_SAMPLE_TTL_S = 48 * 3600
GAS_BASIS_PERCENTILE = 75

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
    # POST /v1/deposits per rate_window_s. It is authenticated, but a session is free (any key
    # can sign in) and since 2026-09-10 a registration whose transaction NO endpoint can see is
    # ACCEPTED as an unverified claim — so the route writes a row for a hash nobody has proven.
    # Both caps together are what keeps that from being unbounded storage and unbounded pages.
    deposit_account_limit: int = 10  # PGAS_DEPOSIT_ACCOUNT_LIMIT
    deposit_ip_limit: int = 30  # PGAS_DEPOSIT_IP_LIMIT
    # ── the operator's read-only admin panel (routers/admin.py). EMPTY IS A REFUSAL, NEVER AN
    # OPEN DOOR: unset — or too short to be a secret, held to the same `secret_problem` test
    # jwt_secret and account_salt face at boot — makes every /admin route answer 404, the answer
    # an unrouted path gives, so a prober cannot even learn the family is mounted. Generate with
    # `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`, keep it in
    # /etc/pgasme.env (0600), and never log, print or commit it.
    admin_key: str = ""  # PGAS_ADMIN_KEY

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
    # Which of the two open routes `route:"auto"` resolves to — the operator's preference, NOT
    # a fourth flag: it can never open a route the flags above have closed (uniswap.default_route
    # clamps it to what is actually open, so a client never preselects a button that can only
    # 409). Refused at parse time on an unknown value: a typo that silently fell back to the
    # other route would be a toggle that does nothing and says nothing.
    ingress_default_route: str = DEFAULT_INGRESS_ROUTE  # PGAS_INGRESS_DEFAULT_ROUTE
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
    # ── A registered transaction no endpoint can SEE yet (2026-09-10 incident). Registration
    # opens the row `verified: false` instead of refusing, and every watcher pass asks the whole
    # pool again. This is how long that may go on before the row is failed and the operator
    # paged: long enough for a mempool that only private relays carry, finite so an unseen hash
    # is never a row that waits forever.
    unseen_tx_ttl_s: int = 2 * 3600  # PGAS_UNSEEN_TX_TTL_S
    # ── How long `Rpc.transaction_anywhere` may spend asking the WHOLE pool whether it has a
    # transaction. The endpoints are asked CONCURRENTLY (they are independent questions), so
    # this is the wall-clock ceiling for the answer, not a per-endpoint timeout × the pool size:
    # asking four endpoints one after another at the 8 s pool timeout is 32 s inside a request
    # the user is waiting on. A deadline that expires with nobody having answered is UNREADABLE,
    # never "not seen" — the caller retries, it never concludes.
    tx_lookup_deadline_s: float = 6.0  # PGAS_TX_LOOKUP_DEADLINE_S
    # ── How far back the scanner may look for the QUOTE that a pipe lock belongs to when no
    # deposit row claims it. The quote is only a candidate if it was created BEFORE the block,
    # and identity (from + pipe + this quote's own sendFunds calldata) is what actually matches
    # it — this bounds the search, it does not authorise it.
    quote_attribution_window_s: int = 86400  # PGAS_QUOTE_ATTRIBUTION_WINDOW_S

    # ── Uniswap V4 ingress (pgasme/uniswap.py). EVERY address here is EMPTY by default and an
    # empty one is never guessed: the route refuses instead. A wrong hook or router address is
    # calldata the user's wallet would sign and the chain would reject, so these are read from
    # the environment the operator provisioned and validated at parse time.
    uniswap_hook: str = ""  # PgasIngressHook (its low 14 address bits are 0x2888)
    uniswap_router: str = ""  # PgasRouter — the `to` of every deposit transaction we issue
    # The chain's deployed V4Quoter, read with eth_call and never written. Pinned like the three
    # addresses below it (same reasoning, same fork test); it is Uniswap's, not ours.
    uniswap_quoter: str = "0x52F0E24D1c21C8A0cB1e5a5dD6198556BD9E1203"
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

    # ── THE TWO-STEP ROUTE (U2, 2026-09-10) — nothing of ours is deployed. ────────────────────
    # The user swaps their token for the target asset on UNISWAP'S OWN Universal Router, into
    # their OWN wallet, and then deposits what actually arrived through the unchanged `direct`
    # path. Step 1 never touches a pipe, so it is never a deposit and is never armed.
    #
    # The four addresses below are Uniswap's canonical mainnet deployments — not ours. They are
    # PINNED here rather than left empty, because an empty one would not be "refuse instead of
    # guess": there is exactly one V4 Universal Router on Ethereum and inventing a second source
    # of truth for it is law 9. What makes a pin evidence instead of an assumption is that
    # `contracts/test/fork/UniswapTwoStep.t.sol` reads each one's CODE and a DISCRIMINATING view
    # on chain, and then executes the API's own calldata through the router.
    # ⚠️ Mainnet values. This route is Ethereum-only (a `uniswap` quote with another source chain
    # is refused in routers/quote.py), so there is no chain to switch them for; a fork or a test
    # chain sets them from the environment.
    #
    # Universal Router, v4-capable: `poolManager()` answers the canonical PoolManager (the pre-v4
    # router at 0x3fC91A3a… has no such function at all), and command 0x10 (V4_SWAP) dispatches
    # into the v4 action decoder rather than reverting `InvalidCommandType` (proven with cast,
    # 2026-09-10: 0x3f → InvalidCommandType(63); 0x10 → SliceOutOfBounds).
    uniswap_universal_router: str = "0x66a9893cC07D91D95644AEDD05D03f95e1dBA8Af"
    # Permit2 — how the Universal Router pulls an ERC-20 input (`V4Router._pay` →
    # `permit2TransferFrom`). Two allowances stand between the user and a swap that works: the
    # token's own allowance to Permit2, and Permit2's allowance for the router. Both are READ.
    uniswap_permit2: str = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
    # The v4 PoolManager, read (never written) with `extsload` for the pool's slot0 + liquidity.
    uniswap_pool_manager: str = "0x000000000004444c5dc75cB358380D2e3dE08A90"
    # How long the swap the user signs stays valid, and how long the Permit2 allowance it needs
    # is granted for — ONE number, because the swap cannot land after its own deadline and an
    # allowance that outlives it is a standing permission nobody asked for.
    uniswap_deadline_s: int = 1200  # PGAS_UNISWAP_DEADLINE_S — 20 minutes
    # ── The ONE-TRANSACTION HOOK route (PgasIngressHook + PgasRouter): designed, reviewed twice,
    # and NOT DEPLOYED (admin 2026-09-10: "don't deploy, make it 2 clicks"). The contracts stay in
    # contracts/ as a reviewed reference and the code that builds their calldata stays here behind
    # this flag — off, so `uniswap` means the two-step route everywhere. Turning it on requires the
    # hook and router addresses as well, and `uniswap.addresses_ok()` says so.
    uniswap_hook_enabled: bool = False  # PGAS_UNISWAP_HOOK_ENABLED

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
    # ── ONE RECEIVER KEY PER DEPOSIT (WO-20260910-3). OFF by default: with it off every quote is
    # armed with the pipe's legacy cid-derived key and nothing about the calldata, the quote row
    # or the claim differs by a byte from the build before this flag existed. With it ON a quote
    # allocates its own index (`pgasme/receiver_keys.py`) and the calldata names that key.
    # ⛔ IT GATES ISSUANCE, NOT RECOGNITION. Turning it off stops NEW indexed keys; it never
    # stops the scanner recognising, the watcher looking for, or the claim signing for a key
    # that was already issued — a deposit on its way to one has no refund path.
    # ⛔ IT NEEDS THE PATCHED PIPE APP AT `beam_shader`, AND THE TWO ARE FLIPPED TOGETHER. The
    # shipped one ignores `index=` and answers the legacy key for every index;
    # `receiver_keys.pk_for_index` refuses that answer, so with this on and the stock wasm still
    # configured every armed quote answers 503 instead of quietly issuing one shared receiver
    # key under N different index numbers. K1 staged the patched build BESIDE the deployed one
    # (`pipe_app.indexed.wasm`), so `beam_shader` still names the old file until K3 moves it.
    receiver_key_per_deposit: bool = False  # PGAS_RECEIVER_KEY_PER_DEPOSIT
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
    # ⛔ A TECHNICAL FLOOR, NOT AN ECONOMIC ONE (2026-09-10). This used to be the floor under a
    # DERIVED minimum — `ceil(relayer_fee_now × 10000 / fee_bps)`, because the bridge fee came out
    # of our 2% — which at ordinary mainnet gas is ~0.045 ETH per wallet, for a product whose
    # whole point is funding a fresh wallet with a few dollars. The bridge fee is charged
    # explicitly per item now (`bridge_fee_groth`), so nothing economic depends on the size of a
    # payout; what is left is that the amount must be positive and land on the asset's grid.
    # Raise it only to impose a product rule (routers/withdrawals.min_amount_groth).
    min_payout_groth: int = 1  # 1 groth: a positive amount on the grid, nothing more
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

    # ── THE INSTANT PAYOUT DISTRIBUTOR (2026-09-10, T34; pgasme/distributor.py). An Ethereum
    # EOA that pays the user directly in one block instead of crossing the bridge. Everything
    # here is inert while `payout_instant_enabled` is 0.
    # ⛔ THE KEY LIVES IN A 0600 FILE ON THE BOX AND IN THIS PROCESS — never in this repository,
    # never in an argument list (`ps` publishes those), never in a log line. The file is READ,
    # its mode is CHECKED, and a file any other account can read is refused outright.
    distributor_key_file: str = ""  # PGAS_DISTRIBUTOR_KEY_FILE, e.g. /root/.pgasme/distributor-1.env
    # The float policy: top up once it falls BELOW the floor, and go all the way to the target.
    # A refill is a b2e crossing of its own (~66 min, its own bridge fee), so refilling to the
    # floor would turn one crossing into a permanent stream of them.
    distributor_float_min_wei: int = 50_000_000_000_000_000  # 0.05 ETH — the trigger
    distributor_float_target_wei: int = 200_000_000_000_000_000  # 0.2 ETH — the destination
    instant_gas_limit: int = 21_000  # a plain ETH transfer; there is no other kind here
    # What the FLOAT GATE adds to the measured gas price so a base-fee tick between the gate and
    # the block cannot make a payout unpayable. Below 1 reads as 1 (distributor.with_headroom).
    instant_gas_headroom: float = 1.25
    # How long a signed transaction may sit without a receipt before the SAME BYTES are handed
    # to the pool again. ⛔ A re-broadcast never re-signs: one order signs one transaction, so
    # at most one of anything can ever fill.
    instant_rebroadcast_after_s: float = 120.0
    # …and how long it may sit in flight before the operator hears about it. The money is on the
    # wire by then, so this is a page, not a refusal — nothing is retried differently. It is ALSO
    # the point at which a transaction nobody can find becomes eligible for its ONE fee-bumped
    # replacement on the same nonce (payouts._instant_fee_bump).
    instant_stuck_after_s: float = 15 * 60.0
    # ⛔ THE REORG FLOOR. How many blocks must sit on top of the receipt before the release is
    # booked and the row reads `sent`. A receipt one block deep is a candidate, not a fact: a
    # re-organisation removes it, and the ledger has no un-send — the user would be credited
    # `sent` for ETH that never left. Two blocks is ~25 s on mainnet, which is inside the
    # instant path's own ~1 minute promise. Never 0: that is the pre-T34b behaviour.
    instant_confirmations: int = 2

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
    # ── THE WORKING FLOAT (2026-09-10, T33). A max-privacy output is LOCKED after it settles —
    # the wallet reported `available_mp 0 / maturing_mp 1652864` ten hours after three shield
    # chunks confirmed — so value shielded is value no payout can spend for up to three days.
    # The shield therefore keeps a float unshielded: this floor, AND every scheduled-but-
    # unreleased liability (amount + bridge fee) grossed up by the buffer below. Only the excess
    # is chunked. Both are recomputed per pass from live numbers — a policy that reserves a
    # constant is the §WE-SET-IT-WE-DONT-READ-IT trap again.
    shield_keep_groth: int = 5_000_000  # 0.05 ETH-equivalent kept spendable at the treasury
    shield_liability_buffer_bps: int = 1000  # +10% on the liabilities the float has to cover
    # ── WHICH SOURCE A CROSSING MAY BE FUNDED FROM (2026-09-10, admin: "in case we don't have
    # available ETH for unlock to let people withdraw, let's keep bETH out of Lelantus"; "as
    # Beam is private by default we can mix those BEAMs"). With this on, the float is the
    # shielded registry PLUS the treasury's unshielded balance, a release picks ONE source
    # (regular first — it carries no lock) and books its contract txid to that source's address.
    # It also RETIRES the §9.3/S2 gate (see `beam_send_inputs_proven`), which is a real privacy
    # trade-off, stated in `payouts.s2_unshielded_gate`. Turn it off for the stricter posture.
    payout_spend_unshielded: bool = True
    payout_interval_s: float = 30.0
    beam_confirmations: int = 61  # b2e: the relayer acts after ~61 Beam confirmations
    beam_claim_fee_groth: int = 2_000_000  # 0.02 BEAM per claim
    beam_shield_fee_groth: int = 1_100_000  # 0.011 BEAM per shielded send
    beam_fee_alert_groth: int = 500_000_000  # page below 5 BEAM (§6.4 step 12)
    # ── HOW MANY COINS, NOT HOW MUCH VALUE (T36). Beam locks a whole UTXO while a transaction
    # that spends it is pending, so what bounds concurrency is the COUNT of spendable coins:
    # 9.8 BEAM in one coin funds ONE call. T40b's F12 made a regular crossing spend BEAM twice
    # (BeamPay's fee on the funding transfer, then the wallet's on the invocation), so a fee
    # coin is consumed per LEG. `beam status` prints target vs actual and names the split
    # command; `python -m pgasme.beam split` is the operator's way to reach it.
    beam_fee_coins: int = 20  # PGAS_BEAM_FEE_COINS — spendable BEAM coins that can pay a fee
    # the size of one: the floor under it only. The real size is `fee_budget("send")` when that
    # is higher, because a coin below what a call costs is not a fee coin at all.
    beam_fee_coin_groth: int = 15_000_000  # 0.15 BEAM
    utxo_target_coins: int = 12  # PGAS_UTXO_TARGET_COINS — spendable coins per PAYOUT asset
    # our own max_privacy address, created through BeamPay (`/create_wallet
    # {wallet_type: max_privacy}`) so the shielded float is a balance BeamPay can report;
    # created once and stored when empty. Its BeamPay balance IS the float.
    beam_mp_address: str = ""
    relayer_fee_margin: float = 1.5  # bridge_fee.py FEE_MARGIN — insurance against REFUSAL
    max_relayer_share: float = 0.10  # refuse a crossing whose relayer fee exceeds this share
    # The CYCLE-level economic gate the share gate cannot be: the relayer fee we pay at release
    # measured against the bridge fee this order actually funded (row.bridge_fee_groth — or, for
    # a row written before 2026-09-10, the 2% that WAS the budget then; payouts.bridge_budget_groth
    # is the one reader). 1.0 = never cross at a loss.
    # ⚠️ 4.0 SINCE T45 (2026-09-10; the box had been running 2.0). A bridge fee is $0.10–0.20 at
    # ordinary mainnet gas, so absorbing even a 4× spike costs cents — and the alternative is an
    # order the user has already paid for in full sitting for hours waiting for gas to come back
    # down. What we absorb is written on the row as `relayer_subsidy_groth`, so the loss is a
    # number an operator can add up rather than an invisible one.
    # ⚠️ IT REACHES BOTH SIDES through `relayer_subsidy()` — the release gate AND the headroom
    # inside the charge (`routers/withdrawals.headroom_for` divides by it). At 4× the far-dated
    # margin (3×) divides to 0.75 and the `bridge_headroom_min` floor wins, so the headroom curve
    # is FLAT at 1.25 for every window. That is the intended trade: the gate carries the wait,
    # and since T45 whatever the crossing does not use is refunded at settlement anyway
    # (`payouts.bridge_fee_refund_groth`), so quoting a 3× crossing 30 days out bought nothing.
    max_relayer_subsidy: float = 4.0
    # The FLOOR under the headroom the bridge fee carries (routers/withdrawals.headroom_for).
    # An order released NOW used to fund exactly today's gas: on 2026-09-10 two ASAP orders were
    # quoted 14,733 groth and the release pass measured 15,793 seconds later — a 7% base-fee
    # tick — and both were held "refusing to cross at a loss" on a crossing the user had paid
    # for. Gas moves between two adjacent blocks; a 1× quote cannot survive that.
    # ⚠️ IT STAYS AT 1.25 AFTER T45, and that is now a PRODUCT choice rather than a compromise:
    # the refund at settlement makes headroom costless to the user (they get back every groth the
    # crossing does not spend), so raising it would only park more of their money for the length
    # of the wait, and lowering it would put the release gate back to work for nothing.
    bridge_headroom_min: float = 1.25
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
    # ── the token lists we host ourselves (pgasme/tokens.py). The refresher writes
    # <evm chain id>.json (+ .json.gz + manifest.json) here and nginx serves the directory as
    # /tokens/; the API only READS the manifest, for /v1/health. A directory it cannot write is
    # a refusal that pages once — never a crash, and never a silently empty list, because the
    # client falls back to /v1/dex/tokens whenever the static file is not there.
    tokens_dir: str = "/opt/pgasme/tokens"  # PGAS_TOKENS_DIR (tests point it at a tmp_path)
    tokens_refresh_interval_s: float = 6 * 3600.0  # PGAS_TOKENS_REFRESH_INTERVAL_S

    @field_validator("ingress_default_route")
    @classmethod
    def _known_route(cls, v: str) -> str:
        v = (v or "").strip().lower() or DEFAULT_INGRESS_ROUTE
        if v not in INGRESS_ROUTES:
            raise ValueError(
                f"PGAS_INGRESS_DEFAULT_ROUTE must be one of {', '.join(INGRESS_ROUTES)} (got {v!r})"
            )
        return v

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


def relayer_subsidy() -> float:
    """THE effective subsidy multiple — one reader, read the same way by both sides of it.

    `PGAS_MAX_RELAYER_SUBSIDY` is one number with two jobs: it is how much MORE than the bridge
    fee an order funded the treasury will still cross for (`payouts._payout_scheduled`), and it
    is therefore what the charge at request time has to carry headroom for
    (`routers/withdrawals.headroom_for`, which divides the far-dated margin by it). Law 9 — two
    implementations of one fact will disagree and one of them will reach money — and they did:
    `headroom_for` read `float(x or 0) or 1.0`, so an unset or 0 knob was 1×, while the gate read
    the raw `0` and `fee > charged × 0` HOLDS EVERY ORDER EVER WRITTEN, including the ones
    charged a full 1× crossing. An operator typing "0" for "no subsidy" got exactly that trap.

    So: 0, unset, unreadable or anything below 1 all mean **1.0 — never cross at a loss, never
    quote below cost**. There is no knob here for "cross only below cost": a value under 1 would
    hold orders that funded their crossing in full, which is the same outage by another route."""
    try:
        v = float(settings.max_relayer_subsidy or 0)
    except (TypeError, ValueError):  # a knob we cannot read is not a knob we may guess at
        v = 0.0
    return v if v >= 1.0 else 1.0


def bridge_headroom_min() -> float:
    """THE floor under the headroom a bridge fee carries — one reader, same shape as above.

    `window_margin` is 1× for an order released now, which quotes the crossing at exactly the
    gas of the block the quote was taken in. On 2026-09-10 that lost twice within seconds: two
    ASAP orders funded 14,733 groth and the release pass, one base-fee tick later, wanted
    15,793 — so orders the user had paid for in full were held "refusing to cross at a loss".

    Anything below 1 reads as **1.0**, for the identical reason `relayer_subsidy` refuses it: a
    headroom under 1× quotes a crossing the treasury is out of pocket on at TODAY's gas, which
    is the same outage the knob was raised to prevent, arriving from the other side."""
    try:
        v = float(settings.bridge_headroom_min or 0)
    except (TypeError, ValueError):
        v = 0.0
    return v if v >= 1.0 else 1.0
