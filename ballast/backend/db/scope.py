"""The fail-closed access scope — the security spine's root object (AD-10).

Every persistence access in Ballast must carry an explicit ``Scope``. A
``Scope`` is *fail-closed by construction*: there is NO default, empty, or
"all rows" scope you can accidentally fall into. It always carries either

- a concrete authenticated user id (``Scope.for_user(user_id)``), or
- the explicit, deliberately-typed SYSTEM marker (``Scope.system()``).

The ONLY way to access data across users is ``Scope.system()`` — which a
caller must literally type out, so it is greppable and auditable (used by
non-user jobs in ``marketdata/`` and ``digest/``). A user scope is a cage:
the ``ScopedRepository`` built on it can never reach another user's rows.

Passing ``None``/omitting a required scope raises — it is never silently
treated as all-access. See ``ScopedRepository`` in ``db/repository.py``.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from uuid import UUID


class _ScopeKind(enum.Enum):
    """Discriminates the two — and only two — legal kinds of scope."""

    USER = "user"
    SYSTEM = "system"


# Private construction guard. Only the two factory classmethods below hold this
# token, so a bare ``Scope(...)`` from application code is rejected in
# ``__post_init__``. This makes the "no back-door, all-access scope" guarantee
# structural — not merely a documented convention.
_CONSTRUCTOR_GUARD = object()


@dataclass(frozen=True)
class Scope:
    """A fail-closed access scope. Construct ONLY via the two class methods.

    Instances are immutable (frozen). ``Scope(...)`` cannot be called directly
    from application code — it raises. Use :meth:`for_user` or :meth:`system`
    so the invariants are guaranteed. The private fields carry either a
    concrete user id (USER kind) or nothing (SYSTEM kind).
    """

    _kind: _ScopeKind
    _user_id: UUID | None
    # Excluded from equality/repr/hash; exists solely to gate construction.
    _guard: object = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        # Structural fail-closed guarantee: reject any construction that did not
        # go through the factory classmethods, and reject invariant-violating
        # combinations even if the guard were somehow supplied.
        if self._guard is not _CONSTRUCTOR_GUARD:
            raise TypeError(
                "Scope cannot be constructed directly; use Scope.for_user(...) "
                "or Scope.system() so cross-user access stays explicit and "
                "auditable (fail-closed)."
            )
        if self._kind is _ScopeKind.USER and self._user_id is None:
            raise ValueError("A USER scope requires a concrete user id (fail-closed).")
        if self._kind is _ScopeKind.SYSTEM and self._user_id is not None:
            raise ValueError("A SYSTEM scope must not carry a user id.")
        if self._kind not in (_ScopeKind.USER, _ScopeKind.SYSTEM):
            raise ValueError(f"Unknown scope kind: {self._kind!r}.")

    @classmethod
    def for_user(cls, user_id: UUID) -> "Scope":
        """Scope every access to a single authenticated user's rows.

        ``user_id`` must be a real UUID. Passing ``None`` (or a non-UUID)
        raises immediately — a missing user id is never treated as all-access
        (fail-closed, AC1).
        """
        if user_id is None:
            raise ValueError(
                "Scope.for_user requires a concrete user id; "
                "None is not a valid scope (fail-closed)."
            )
        if not isinstance(user_id, UUID):
            raise TypeError(
                f"Scope.for_user requires a UUID, got {type(user_id).__name__}."
            )
        return cls(_ScopeKind.USER, user_id, _CONSTRUCTOR_GUARD)

    @classmethod
    def system(cls) -> "Scope":
        """The explicit, named cross-user scope for non-user jobs.

        This is the ONLY path that spans all owners. It must be typed out
        deliberately by the caller (market-data ingestion, digest, batch
        jobs) — never reached by default. Greppable on purpose.
        """
        return cls(_ScopeKind.SYSTEM, None, _CONSTRUCTOR_GUARD)

    @property
    def is_system(self) -> bool:
        """True iff this is the explicit SYSTEM (cross-user) scope."""
        return self._kind is _ScopeKind.SYSTEM

    @property
    def user_id(self) -> UUID:
        """The scoped user's id.

        Raises if called on a SYSTEM scope — a system scope has no single
        owner, and asking for one is a programming error (guard with
        :attr:`is_system` first).
        """
        if self._kind is not _ScopeKind.USER:
            raise ValueError(
                "Scope.user_id is undefined for a SYSTEM scope; "
                "check `scope.is_system` before accessing user_id."
            )
        if self._user_id is None:  # invariant of USER kind — raise (survives -O)
            raise ValueError("USER scope has no user id (invariant violated).")
        return self._user_id

    def __repr__(self) -> str:  # pragma: no cover - debug aid, no data leak
        if self._kind is _ScopeKind.SYSTEM:
            return "Scope(SYSTEM)"
        return f"Scope(user={self._user_id})"
