"""SQLAlchemy ORM models (async).

The canonical user identity for Ballast. Built on FastAPI-Users'
``SQLAlchemyBaseUserTableUUID`` which gives us:
- a UUID primary key (``id``) — the identity every later per-user scoped query
  keys on (AD-10),
- ``email`` (unique, indexed),
- ``hashed_password`` — the password is stored hashed, never plaintext,
- the standard ``is_active`` / ``is_superuser`` / ``is_verified`` flags.

Do NOT add plaintext password columns or hand-roll hashing.
"""

from __future__ import annotations

import uuid

from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTableUUID
from sqlalchemy import ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all Ballast ORM models."""


class User(SQLAlchemyBaseUserTableUUID, Base):
    """The Ballast user account (table: ``user``).

    Inherits the UUID primary key, unique email, and hashed_password columns
    from FastAPI-Users' base table. This is the identity Story 1.4's
    fail-closed scoped-repository layer will build on.

    NOTE: ``User`` is the *owner*, not an owned entity — it deliberately does
    NOT use ``OwnedEntityMixin`` and is never routed through the
    ``ScopedRepository`` (FastAPI-Users manages it).
    """


class OwnedEntityMixin:
    """Mixin for every per-user table (portfolio_cache, decision_record, ...).

    Provides a single indexed ``owner_id`` UUID column that FKs to
    ``"user".id``. The fail-closed ``ScopedRepository`` (``db/repository.py``)
    filters and stamps rows by this column, so applying this mixin is what
    makes a table participate in AD-10 per-user isolation.

    Do NOT apply this to ``User`` — the user IS the owner, not an owned row.

    Example::

        class PortfolioCache(OwnedEntityMixin, Base):
            __tablename__ = "portfolio_cache"
            id: Mapped[uuid.UUID] = mapped_column(primary_key=True, ...)
            # owner_id is provided by the mixin.
    """

    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
