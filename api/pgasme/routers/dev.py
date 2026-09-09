"""POST /v1/dev/credit — seed a balance for UI / e2e tests.

Mounted only when settings.dev_endpoints_active (PGAS_DEV_ENDPOINTS=1 AND env != prod), and
guarded again here at the level it protects: a router mounted by hand still cannot mint
balance on a production process.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import auth, ledger
from ..assets import ASSETS
from ..config import settings

router = APIRouter(prefix="/v1/dev", tags=["dev"])


class CreditIn(BaseModel):
    asset: str = "ETH"
    groth: int = Field(gt=0, le=10**14)


@router.post("/credit")
async def credit(body: CreditIn, acct=auth.Account):
    if not settings.dev_endpoints_active:
        raise HTTPException(404, "not found")
    asset = body.asset.upper()
    if asset not in ASSETS:
        raise HTTPException(400, f"unknown asset {body.asset!r}")
    ref = "dev:" + secrets.token_hex(6)
    entry = await ledger.credit(
        acct["account_id"], asset, body.groth, ref, "dev credit (test only)"
    )
    return {
        "credited": body.groth,
        "asset": asset,
        "ref": ref,
        "balances": await ledger.balances(acct["account_id"]),
        "entry": entry,
    }
