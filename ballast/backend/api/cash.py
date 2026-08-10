"""Cash-configuration endpoints (Story 9.1, Epic 9 Cash Intelligence).

Two authenticated, user-scoped endpoints back the Settings "Cash setup" card and
the set-or-decline prompt (``GET``/``PUT /api/cash/config``) — both funnel through
the fail-closed scope (AD-10), so a user only ever reads or changes their OWN
config.

Money crosses the wire as fixed-point strings via ``WireMoney`` (never binary
float, never ``E+``/``E-``); a negative reserve is refused with a calm 422 the
same way ``/api/portfolio/refresh`` surfaces a config fault. This story only
records the config — the yield-aware missed-growth math is Story 9-2.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_scope
from cash.config import get_or_create_config, set_config
from db.scope import Scope
from db.session import get_async_session
from money import WireMoney

logger = logging.getLogger("ballast.api.cash")

router = APIRouter(prefix="/api/cash", tags=["cash"])


# --- Schemas -----------------------------------------------------------------


class CashConfigOut(BaseModel):
    """The current user's cash configuration.

    ``reserve_amount`` is the RAW stored amount: the set value, or ``null`` when
    never-decided OR declined (``reserve_decided`` disambiguates). Money renders
    as a fixed-point string via ``WireMoney``.
    """

    reserve_amount: WireMoney | None = None
    reserve_decided: bool
    parked_symbols: list[str]


class CashConfigIn(BaseModel):
    """A desired cash configuration from the Settings card / set-or-decline.

    ``reserve_amount`` in as ``Decimal | None`` (Pydantic coerces the JSON
    string/number). ``reserve_decided`` records that the user has made an explicit
    decision (set OR declined) — a PUT is always an explicit act, so it is
    ``True`` by default. ``reserve_amount = null`` with ``reserve_decided = true``
    means "I don't keep a reserve" (declined → resolves to 0).
    """

    reserve_amount: Decimal | None = None
    reserve_decided: bool = True
    parked_symbols: list[str] = []


# --- Endpoints ---------------------------------------------------------------


def _to_out(config) -> CashConfigOut:
    return CashConfigOut(
        reserve_amount=config.reserve_amount,
        reserve_decided=config.reserve_decided,
        parked_symbols=list(config.parked_symbols or []),
    )


@router.get("/config", response_model=CashConfigOut)
async def read_config(
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> CashConfigOut:
    """Return the current user's cash config (calm default for a new user)."""
    config = await get_or_create_config(scope, session)
    return _to_out(config)


@router.put("/config", response_model=CashConfigOut)
async def update_config(
    body: CashConfigIn,
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> CashConfigOut:
    """Set the reserve (or decline) and the parked-symbol set (takes effect now).

    An invalid reserve (negative, non-finite, out of range, or over two decimal
    places) is a config fault the user can fix — surfaced as a calm 422, never a
    raw 500 (NFR8). The whole config is written in one atomic commit through the
    fail-closed scoped helper, so a user can only ever change their OWN config and
    a failure never half-applies.
    """
    try:
        config = await set_config(
            scope,
            session,
            amount=body.reserve_amount,
            decided=body.reserve_decided,
            symbols=body.parked_symbols,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _to_out(config)
