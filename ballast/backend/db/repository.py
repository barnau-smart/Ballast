"""The single funnel for per-user persistence — fail-closed (AD-10).

``ScopedRepository`` is the ONLY sanctioned way later stories (portfolio_cache
→ 2.3, decision_record → Epic 4) read or write per-user data. It is generic
over any :class:`~db.models.OwnedEntityMixin`-based model and REQUIRES a
:class:`~db.scope.Scope` at construction. If the scope is missing/``None`` it
raises immediately — there is no way to construct the repository into an
"all rows" state by omission (fail-closed, AC1).

Behaviour by scope kind:

- **USER scope** (a cage): ``list``/``get`` filter ``owner_id == scope.user_id``;
  ``get`` of another user's row returns ``None``; ``add`` stamps
  ``owner_id = scope.user_id`` (a user can never forge a row owned by someone
  else). No method returns or mutates another user's rows (AC3).
- **SYSTEM scope** (explicit cross-user, AC2): ``list``/``get`` span all
  owners — the ONLY cross-user path, and it must be typed out
  (``Scope.system()``). ``add`` REQUIRES an explicit ``owner_id`` keyword,
  because a system-wide repository has no single owner to stamp; omitting it
  raises rather than guessing.

Usage (from a request handler, obtaining scope via ``api.deps.get_scope``)::

    async def handler(scope: Scope = Depends(get_scope),
                      session: AsyncSession = Depends(get_async_session)):
        repo = ScopedRepository(PortfolioCache, scope, session)
        rows = await repo.list()  # only THIS user's rows
"""

from __future__ import annotations

from typing import Generic, TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import OwnedEntityMixin
from db.scope import Scope

ModelT = TypeVar("ModelT", bound=OwnedEntityMixin)


class ScopedRepository(Generic[ModelT]):
    """A per-scope repository over an owned model. Construct with a Scope."""

    def __init__(
        self,
        model: type[ModelT],
        scope: Scope,
        session: AsyncSession,
    ) -> None:
        # Fail-closed: a missing scope is NEVER treated as all-access.
        if scope is None:
            raise ValueError(
                "ScopedRepository requires an explicit Scope "
                "(Scope.for_user(...) or Scope.system()); refusing to run "
                "unscoped (fail-closed)."
            )
        if not isinstance(scope, Scope):
            raise TypeError(
                "ScopedRepository requires a Scope instance, got "
                f"{type(scope).__name__}."
            )
        if session is None:
            raise ValueError("ScopedRepository requires an AsyncSession.")
        if model is None or not isinstance(model, type):
            raise TypeError("ScopedRepository requires an owned model class.")

        self._model = model
        self._scope = scope
        self._session = session

    async def list(self) -> list[ModelT]:
        """Return all rows visible to this scope.

        USER scope: only the scoped user's rows. SYSTEM scope: all rows.
        """
        stmt = select(self._model)
        if not self._scope.is_system:
            stmt = stmt.where(self._model.owner_id == self._scope.user_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get(self, id: UUID) -> ModelT | None:
        """Return the row with primary key ``id`` if visible to this scope.

        USER scope: returns ``None`` for a row owned by anyone else (no leak).
        SYSTEM scope: returns the row regardless of owner.
        """
        # Filter ownership in SQL so a non-owned row is NEVER loaded into the
        # session under a user scope (defense-in-depth: no foreign row hydrated
        # into the identity map, even transiently).
        pk = self._model.__mapper__.primary_key[0]
        stmt = select(self._model).where(pk == id)
        if not self._scope.is_system:
            stmt = stmt.where(self._model.owner_id == self._scope.user_id)
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def add(self, *, owner_id: UUID | None = None, **fields: object) -> ModelT:
        """Create and persist a new row, stamping the owner from the scope.

        USER scope: ``owner_id`` is ALWAYS the scoped user — a user cannot
        forge a row owned by someone else. Passing an ``owner_id`` that does
        not match the scope raises.

        SYSTEM scope: no single owner exists to stamp, so an explicit
        ``owner_id`` keyword is REQUIRED; omitting it raises (a system
        repository will not silently create ownerless/misowned rows).

        Returns the flushed instance (its server/default-generated id is
        populated). The caller controls commit via the session.
        """
        if self._scope.is_system:
            if owner_id is None:
                raise ValueError(
                    "add() under a SYSTEM scope requires an explicit owner_id "
                    "(a system repository has no single owner to stamp)."
                )
            resolved_owner = owner_id
        else:
            scope_owner = self._scope.user_id
            if owner_id is not None and owner_id != scope_owner:
                raise ValueError(
                    "add() under a user scope cannot set an owner_id other "
                    "than the scoped user (cannot forge another owner)."
                )
            resolved_owner = scope_owner

        obj = self._model(owner_id=resolved_owner, **fields)
        self._session.add(obj)
        await self._session.flush()
        return obj
