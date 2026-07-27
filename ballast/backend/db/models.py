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

import datetime
import uuid

from decimal import Decimal

from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTableUUID
from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
)
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


class BrokerageToken(OwnedEntityMixin, Base):
    """A user's encrypted brokerage OAuth tokens (table: ``brokerage_token``).

    A per-user owned entity (AD-10): reachable ONLY through the fail-closed
    ``ScopedRepository``, so user A can never read user B's tokens.

    The ``access_token`` / ``refresh_token`` columns hold CIPHERTEXT ONLY — the
    plaintext is encrypted at the application layer (``brokers.crypto``) before
    it is ever handed to this model. The encryption key lives in the
    environment, never in this table. ``expires_at`` is timezone-aware UTC.

    NOTE: this stores tokens for the *link* (Story 2.1). Session-status /
    graceful re-auth (Story 2.2) and refresh-rotation live in later stories.
    """

    __tablename__ = "brokerage_token"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # e.g. "schwab". Which brokerage these tokens belong to.
    provider: Mapped[str] = mapped_column(String(length=32), nullable=False)

    # Encrypted (Fernet) token material — NEVER the plaintext. Sized generously
    # because ciphertext is larger than the plaintext token.
    access_token: Mapped[str] = mapped_column(String(length=2048), nullable=False)
    refresh_token: Mapped[str] = mapped_column(String(length=2048), nullable=False)

    # When the access token expires (timezone-aware UTC, ISO-8601 on the wire).
    expires_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class PortfolioCache(OwnedEntityMixin, Base):
    """A user's cached holdings — a single-writer projection (table:
    ``portfolio_cache``).

    AD-14: this is a *derived read-model* with EXACTLY ONE writer — the
    portfolio projection in ``brokers.portfolio``. The broker is authoritative;
    nothing else writes this table, and every other consumer reads it read-only
    through the fail-closed ``ScopedRepository`` (AD-10, per-user isolation).

    One row per holding. ``as_of`` and ``cash`` are the account-level snapshot
    values, denormalized onto every row: a single reconcile replaces ALL of a
    user's rows atomically with the same snapshot, so they stay consistent, and
    the reconcile-wins rule keys on ``as_of`` (a newer snapshot supersedes older
    rows; a stale one never clobbers newer truth). A cash-only/empty account
    yields zero rows (cash surfaces as 0 in that edge case — a dedicated
    balances row is a later refinement, out of scope for v1 import).

    Money columns are ``Numeric`` (Python ``Decimal``) — NEVER binary float
    (consistency convention). ``as_of`` is timezone-aware UTC.
    """

    __tablename__ = "portfolio_cache"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # The broker's instrument symbol for this holding (e.g. "VTI").
    symbol: Mapped[str] = mapped_column(String(length=32), nullable=False)

    # Position size and valuation — Decimal, never float. Precision is generous
    # for share quantities (fractional shares) and dollar values.
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=20, scale=8), nullable=False)
    market_value: Mapped[Decimal] = mapped_column(
        Numeric(precision=20, scale=2), nullable=False
    )
    # Cost basis may be absent from the broker feed.
    cost_basis: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=20, scale=2), nullable=True
    )

    # Account-level settled cash, denormalized onto each row (same across a
    # user's rows within one reconcile). Decimal, never float.
    cash: Mapped[Decimal] = mapped_column(Numeric(precision=20, scale=2), nullable=False)

    # The broker's snapshot timestamp — the reconcile-wins key (AD-14).
    as_of: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class MarketDaily(Base):
    """One end-of-day bar per (symbol, day) — GLOBAL market reference data
    (table: ``market_daily``).

    GLOBAL, NOT per-user (AD-10). Unlike ``brokerage_token`` / ``portfolio_cache``
    (owned, per-user, reached only through the fail-closed ``ScopedRepository``),
    ``market_daily`` is shared reference data with NO owner. It is therefore
    deliberately NOT an :class:`OwnedEntityMixin` and has NO ``owner_id`` column,
    and it is NOT routed through the :class:`ScopedRepository` — that repo exists
    specifically to enforce per-user isolation on OWNED entities, and market data
    has no owner to isolate. The ingestion job (``marketdata.ingest``) is the
    "non-user SYSTEM context" AD-10 anticipates: it writes this table globally, by
    construction, without any user scope. This is intentional and is NOT a scoping
    bypass — do not add an ``owner_id`` or route it through the scoped repo.

    Keyed by (``symbol``, ``day``) with a UNIQUE constraint so ingestion can UPSERT
    and stay idempotent: repeated / overlapping runs converge to exactly one row
    per symbol/day (AC2). Money columns are ``Numeric`` (Python ``Decimal``) —
    NEVER binary float. ``volume`` is a ``BigInteger`` (share counts exceed 32-bit
    for liquid names over decades). ``day`` is a calendar ``date``. These are
    DERIVED analytics (OHLC + adjusted close needed for drawdown / forward-return
    math), NOT a raw redistribution of the vendor feed (data-sourcing rule).
    ``source`` records which adapter produced the row (e.g. "fake", "tiingo") and
    ``ingested_at`` is a tz-aware UTC timestamp of when the row was written.
    """

    __tablename__ = "market_daily"
    __table_args__ = (
        UniqueConstraint("symbol", "day", name="uq_market_daily_symbol_day"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # The instrument symbol (e.g. "VTI") and the calendar day of the bar.
    symbol: Mapped[str] = mapped_column(String(length=32), nullable=False, index=True)
    day: Mapped[datetime.date] = mapped_column(Date, nullable=False, index=True)

    # OHLC + adjusted close — Decimal, never float. Precision is generous for
    # decades of price history across instruments.
    open: Mapped[Decimal] = mapped_column(Numeric(precision=20, scale=8), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(precision=20, scale=8), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(precision=20, scale=8), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(precision=20, scale=8), nullable=False)
    adj_close: Mapped[Decimal] = mapped_column(
        Numeric(precision=20, scale=8), nullable=False
    )

    # Share volume — BigInteger (liquid names over decades exceed 32-bit).
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Which adapter produced this row (e.g. "fake", "tiingo") — provenance.
    source: Mapped[str] = mapped_column(String(length=32), nullable=False)

    # When this row was ingested/last upserted (timezone-aware UTC).
    ingested_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
