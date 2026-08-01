"""Portfolio read + refresh endpoints (Story 2.3).

Exposes the user's cached portfolio (the AD-14 single-writer projection) as
plain JSON, plus an on-demand refresh that reconciles the cache from the
authoritative Broker Port. This is the RAW cache read — the plain-English
dashboard rendering is Story 2.4 (not built here).

Both endpoints are authenticated + user-scoped (reuse 1.3 ``current_user`` via
1.4 ``get_scope``) and read/write ONLY the current user's cache through the
fail-closed ``ScopedRepository`` (AD-10). Per AD-11, portfolio read/import is a
read-class surface and is NOT gated on a live brokerage session — it continues
to work in degraded mode (only execution is gated, Story 2.2).
"""

from __future__ import annotations

import datetime
import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_scope
from brokers.factory import get_broker
from brokers.port import BrokerPort
from brokers.portfolio import PortfolioView, get_portfolio, reconcile_portfolio
from db.scope import Scope
from db.session import get_async_session
from money import WireMoney
from strategy.index_core import is_index_core

logger = logging.getLogger("ballast.api.portfolio")

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


# --- Schemas -----------------------------------------------------------------


class HoldingOut(BaseModel):
    """One cached holding — plain projection data, no secrets.

    ``is_core`` maps the holding to the index-core strategy (Story 2.5, FR6):
    True when it is part of the broad index-fund core, False otherwise. It is
    DERIVED at read time from the ``strategy.index_core`` reference — never
    stored on ``portfolio_cache`` (which stays a pure broker projection, AD-14),
    so the core definition can evolve without a re-import.
    """

    symbol: str
    quantity: WireMoney
    market_value: WireMoney
    cost_basis: WireMoney | None = None
    is_core: bool = False


class PortfolioOut(BaseModel):
    """The cached portfolio snapshot for the current user.

    ``as_of`` is the broker snapshot timestamp the cache was reconciled from,
    or ``None`` if the portfolio has never been imported for this user.
    """

    holdings: list[HoldingOut]
    cash: WireMoney
    as_of: datetime.datetime | None


def _to_out(view: PortfolioView) -> PortfolioOut:
    return PortfolioOut(
        holdings=[
            HoldingOut(
                symbol=h.symbol,
                quantity=h.quantity,
                market_value=h.market_value,
                cost_basis=h.cost_basis,
                is_core=is_index_core(h.symbol),
            )
            for h in view.holdings
        ],
        cash=view.cash,
        as_of=view.as_of,
    )


# --- Endpoints ---------------------------------------------------------------


@router.get("", response_model=PortfolioOut)
async def read_portfolio(
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> PortfolioOut:
    """Return the current user's cached holdings + cash (READ-ONLY).

    Reads the AD-14 projection through the scoped repo; never writes. Not gated
    on a live session (degraded mode keeps reads working — AD-11).
    """
    view = await get_portfolio(scope, session)
    return _to_out(view)


@router.post("/refresh", response_model=PortfolioOut)
async def refresh_portfolio(
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
    broker: BrokerPort = Depends(get_broker),
) -> PortfolioOut:
    """Force a reconcile of the cache from the authoritative broker (AD-14).

    Delegates to the single-writer projection; reconcile-wins keyed on ``as_of``
    means a stale re-fetch never clobbers newer cached truth.
    """
    view = await reconcile_portfolio(scope, session, broker)
    return _to_out(view)
