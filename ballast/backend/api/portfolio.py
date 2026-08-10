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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_scope
from brokers.factory import get_reading_broker
from brokers.port import BrokerPort
from brokers.portfolio import PortfolioView, get_portfolio, reconcile_portfolio
from brokers.schwab_adapter import SchwabAccountSelectionError
from cash.config import (
    get_config,
    normalize_symbols,
    parked_market_value,
    resolve_reserve,
)
from db.models import CashConfig
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
    is_parked: bool = False


class CashStatesOut(BaseModel):
    """The honest three-state split of the user's cash (Story 9.1, Epic 9).

    Additive to :class:`PortfolioOut` — the existing ``cash`` field is unchanged.
    All three states are DERIVED at read time from the user's :class:`CashConfig`
    (never stored on the pure ``portfolio_cache`` projection, AD-14):

    - ``ready_to_trade`` — settlement cash (== ``PortfolioOut.cash``, the
      authoritative ``portfolio_balance`` figure).
    - ``parked`` — Σ market value of the user's parked (money-market) holdings.
    - ``reserved`` — the RESOLVED reserve: the amount if set, ``0`` if declined,
      ``null`` if never-decided (never silently 0 — that is the honesty crux).
    - ``reserve_decided`` — whether the user has made an explicit reserve decision
      (drives the calm one-time set-or-decline prompt when ``False``).
    """

    ready_to_trade: WireMoney
    parked: WireMoney
    reserved: WireMoney | None = None
    reserve_decided: bool


class PortfolioOut(BaseModel):
    """The cached portfolio snapshot for the current user.

    ``as_of`` is the broker snapshot timestamp the cache was reconciled from,
    or ``None`` if the portfolio has never been imported for this user.

    ``cash_states`` (Story 9.1) is an ADDITIVE honest three-state view of cash;
    the original ``holdings``/``cash``/``as_of`` fields are unchanged (reconcile +
    missed-growth depend on them).
    """

    holdings: list[HoldingOut]
    cash: WireMoney
    as_of: datetime.datetime | None
    cash_states: CashStatesOut


def _to_out(view: PortfolioView, config: CashConfig | None) -> PortfolioOut:
    # Parked classification is DERIVED at read time from the user's config
    # (case-insensitive), exactly as ``is_core`` is derived — never stored on the
    # pure broker projection (AD-14). An unheld tagged symbol simply matches
    # nothing here. ``config`` is ``None`` for a user who has never set one — a
    # calm never-decided default, and the read path never writes one (AD-11:
    # ``GET`` stays read-only). Reuse ``normalize_symbols`` so the read-path
    # compare rule can't drift from how the symbols were stored.
    parked_set = set(normalize_symbols(config.parked_symbols)) if config else set()

    def _is_parked(symbol: str) -> bool:
        return bool(symbol) and symbol.strip().upper() in parked_set

    holdings = [
        HoldingOut(
            symbol=h.symbol,
            quantity=h.quantity,
            market_value=h.market_value,
            cost_basis=h.cost_basis,
            is_core=is_index_core(h.symbol),
            is_parked=_is_parked(h.symbol),
        )
        for h in view.holdings
    ]

    # Single source of the parked-sum rule (shared with the missed-growth read)
    # so the two endpoints can never drift. Same rule as ``_is_parked`` above.
    parked_total = parked_market_value(view.holdings, config)

    return PortfolioOut(
        holdings=holdings,
        cash=view.cash,
        as_of=view.as_of,
        cash_states=CashStatesOut(
            ready_to_trade=view.cash,
            parked=parked_total,
            reserved=resolve_reserve(config) if config is not None else None,
            reserve_decided=config.reserve_decided if config is not None else False,
        ),
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
    config = await get_config(scope, session)
    return _to_out(view, config)


@router.post("/refresh", response_model=PortfolioOut)
async def refresh_portfolio(
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
    broker: BrokerPort = Depends(get_reading_broker),
) -> PortfolioOut:
    """Force a reconcile of the cache from the authoritative broker (AD-14).

    Delegates to the single-writer projection; reconcile-wins keyed on ``as_of``
    means a stale re-fetch never clobbers newer cached truth. Uses
    ``get_reading_broker`` so a Schwab refresh authenticates with THIS user's
    decrypted token (Story 6.5); the fake path passes through untouched.
    """
    try:
        view = await reconcile_portfolio(scope, session, broker)
    except SchwabAccountSelectionError as exc:
        # A multi-account login with no explicit selection (or a non-matching
        # SCHWAB_ACCOUNT_ID) is a CONFIG fault the operator can fix — surface it
        # calmly (never a raw 500), symmetric with the approve path's refusal
        # (NFR8 calm/honest voice). No cache was written.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    config = await get_config(scope, session)
    return _to_out(view, config)
