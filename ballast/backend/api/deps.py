"""Request-scoped dependencies that funnel handlers into the security spine.

The one dependency here — :func:`get_scope` — turns the authenticated user
(Story 1.3's ``current_user``) into a fail-closed :class:`~db.scope.Scope`.
Every future per-user endpoint MUST obtain its scope this way and build a
:class:`~db.repository.ScopedRepository` from it, rather than touching raw
sessions. That is what keeps AD-10 unavoidable: there is no request path to a
user's data that is not scoped to that user.

No user-data endpoints exist yet (they arrive in later stories) — this
dependency exists now so those stories reach for the funnel by default.

Usage example (a later story's endpoint)::

    from fastapi import Depends
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.deps import get_scope
    from db.repository import ScopedRepository
    from db.scope import Scope
    from db.session import get_async_session

    @app.get("/api/portfolio")
    async def read_portfolio(
        scope: Scope = Depends(get_scope),
        session: AsyncSession = Depends(get_async_session),
    ):
        repo = ScopedRepository(PortfolioCache, scope, session)
        return await repo.list()  # only the authenticated user's rows

Non-user (job) code does NOT use this dependency: it constructs
``Scope.system()`` explicitly — the sole, greppable cross-user path.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.users import fastapi_users
from brokers.session import BrokerageSession, get_brokerage_session
from db.models import User
from db.scope import Scope
from db.session import get_async_session

# Reuse Story 1.3's authenticated-user dependency (active users only). An
# unauthenticated request is rejected 401 here before any scope is built.
_current_active_user = fastapi_users.current_user(active=True)


def get_scope(user: User = Depends(_current_active_user)) -> Scope:
    """Build a fail-closed user :class:`Scope` from the authenticated user.

    Returns ``Scope.for_user(user.id)``. Because ``current_user`` already
    rejects unauthenticated/inactive requests, a handler depending on this
    always receives a concrete, single-user scope — never a system or empty
    one. Jobs that need cross-user access must construct ``Scope.system()``
    themselves, deliberately.
    """
    return Scope.for_user(user.id)


# Plain-language, jargon-free copy surfaced when an execution-class action is
# attempted without a live brokerage session (NFR6/NFR8). It is calm and never
# an error dump: the weekly reconnect is normal, not a failure.
RECONNECT_MESSAGE = (
    "Your Schwab connection needs a quick reconnect before this can go through. "
    "This is normal — it happens about once a week. Reconnect and you'll be right "
    "back where you left off."
)

# HTTP status for a blocked execution-class action. 409 Conflict = the request
# is well-formed and authenticated, but the account is in a state (session not
# live) that must be resolved first. Deliberately NOT 401 (auth IS valid) and
# NOT 500 (nothing broke).
_RECONNECT_STATUS = 409


async def require_live_broker_session(
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> BrokerageSession:
    """Gate an EXECUTION-class endpoint on a *live* brokerage session (AD-11).

    Attach this ONLY to endpoints that place or act on live brokerage state
    (order execution — Epic 4). It returns the live :class:`BrokerageSession`
    when the session is live, and otherwise raises a calm, plain-language
    "reconnect needed" response (HTTP 409, routed through the app's error
    envelope). An order is NEVER placed on an ``expired``/``unlinked`` session.

    DEGRADED MODE (AD-11): read + coach endpoints MUST NOT depend on this — they
    continue to work while the session is expired. Only live-session actions are
    gated. Read/coach handlers depend on :func:`get_scope` alone.

    No real execution endpoint exists yet (Epic 4). Epic 4 plugs in like so::

        @router.post("/api/orders")
        async def place_order(
            order: OrderRequest,
            broker_session: BrokerageSession = Depends(require_live_broker_session),
            scope: Scope = Depends(get_scope),
            session: AsyncSession = Depends(get_async_session),
        ):
            # Only reached when the session is live; safe to place the order.
            ...
    """
    broker_session = await get_brokerage_session(scope, session)
    if not broker_session.is_live:
        # Never leak token values or internal state; just the calm prompt.
        raise HTTPException(status_code=_RECONNECT_STATUS, detail=RECONNECT_MESSAGE)
    return broker_session
