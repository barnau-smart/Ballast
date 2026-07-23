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

from fastapi import Depends

from api.users import fastapi_users
from db.models import User
from db.scope import Scope

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
