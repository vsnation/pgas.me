"""The bridgeable target assets, their pipes, and their USD prices.

Mainnet addresses verified 2026-09-09 from the founder's live bridge code. A deposit lands on
Ethereum as one of these and the DLN hook calls THAT asset's pipe; the balance is kept per
asset (bETH / bDAI / bWBTC), 8 decimals on Beam. USDT is deliberately not offered (Tether
blacklist risk on the pipe). Prices (CoinGecko, cached 5 min) serve ONLY the minimum-deposit
comparison; a price failure raises PriceError and the caller decides (the quote route allows
the deposit and says so — the floor is a product rule, not a safety one).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from .config import settings

NATIVE = "0x0000000000000000000000000000000000000000"
PRICE_TTL_S = 300


@dataclass(frozen=True)
class Asset:
    key: str  # ETH | DAI | WBTC
    symbol: str  # user-facing
    beam_symbol: str  # bETH ...
    token: str  # Ethereum token address, NATIVE for ETH
    eth_decimals: int
    pipe: str  # EthPipe / EthERC20Pipe address
    beam_cid: str  # pipe shader cid (the one every call uses — NOT the asset-owner cid)
    aid: int  # Beam confidential asset id
    coingecko: str

    @property
    def native(self) -> bool:
        return self.token == NATIVE

    @property
    def grid(self) -> int:
        """Ethereum units per groth: 10**(eth_decimals-8) — a lock value must be a multiple of it."""
        return 10 ** max(0, self.eth_decimals - 8)


ASSETS: dict[str, Asset] = {
    "ETH": Asset(
        "ETH",
        "ETH",
        "bETH",
        NATIVE,
        18,
        "0xB1d7FF9D3aCaf30e282c5F6eb1F2A6503f516a96",
        "8872509d36a8e2aa7a60839a1828c372af47c0a5309f3f6186379cddec847369",
        36,
        "ethereum",
    ),
    "DAI": Asset(
        "DAI",
        "DAI",
        "bDAI",
        "0x6B175474E89094C44Da98b954EedeAC495271d0F",
        18,
        "0xAcDc8f4559741a3c8CAAB0ba74c57807A9Fe2d73",
        "02fb908e55a59ab5acc5bf6f1707a8dcdb70a944d6f2a7bff3c7af18c8e278da",
        39,
        "dai",
    ),
    "WBTC": Asset(
        "WBTC",
        "WBTC",
        "bWBTC",
        "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
        8,
        "0x604422D7eC88c45b82B71851d073eFeaA928dcEF",
        "7c66181ba4625202aae6e46afe89acbf1f839523344b0b371fc7988ac2e8c056",
        38,
        "wrapped-bitcoin",
    ),
}


def get_asset(key: str) -> Asset:
    a = ASSETS.get((key or "").upper())
    if not a:
        raise KeyError(f"unsupported target asset {key!r}; choose one of {', '.join(ASSETS)}")
    return a


def to_groth(units: int, asset: Asset) -> int:
    return units // asset.grid


class PriceError(RuntimeError):
    pass


_price_cache: dict[str, object] = {"at": 0.0, "data": None}


def clear_price_cache() -> None:
    _price_cache["at"] = 0.0
    _price_cache["data"] = None


async def usd_prices(force: bool = False) -> dict[str, float]:
    """{'ETH': 2504.9, 'DAI': 0.9999, 'WBTC': 79365.0} from CoinGecko simple/price."""
    now = time.time()
    data = _price_cache["data"]
    if not force and isinstance(data, dict) and now - float(_price_cache["at"]) < PRICE_TTL_S:
        return data
    ids = ",".join(sorted({a.coingecko for a in ASSETS.values()}))
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{settings.coingecko_base.rstrip('/')}/simple/price",
                params={"ids": ids, "vs_currencies": "usd"},
            )
        if r.status_code != 200:
            raise PriceError(f"coingecko HTTP {r.status_code}")
        body = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise PriceError(f"coingecko: {type(e).__name__}: {e}") from e
    out: dict[str, float] = {}
    for key, a in ASSETS.items():
        p = (body.get(a.coingecko) or {}).get("usd")
        if not isinstance(p, (int, float)) or p <= 0:
            raise PriceError(f"coingecko: no usd price for {a.coingecko}")
        out[key] = float(p)
    _price_cache["data"] = out
    _price_cache["at"] = now
    return out
