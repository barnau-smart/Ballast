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
    JSON,
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


class DecisionRecord(OwnedEntityMixin, Base):
    """A co-signed, IMMUTABLE decision record (table: ``decision_record``).

    The on-the-record memory of a coach decision (FR16, AD-5/AD-6): the single
    per-user, insert-then-co-sign immutable table that backs recommend/co-sign
    (and, later, replay). One row is inserted **proposed** at ``/recommend`` time
    and — on a successful execution — transitions ONCE to **cosigned** at
    ``/approve`` time.

    IMMUTABILITY: the snapshot columns are WRITE-ONCE at propose —
    ``schema_version``, ``recommendation_snapshot`` (the blessed
    action_label/reasoning/full evidence records/uncertainties/proposed
    order_intent), and ``created_at`` are NEVER mutated after insert. Co-sign
    fills the previously-NULL co-sign columns (``co_signed_at``,
    ``idempotency_key``, ``cosign_snapshot``) EXACTLY once, guarded on
    ``status == "proposed"``; a cosigned record is only ever READ afterward. The
    recommendation snapshot is never re-derived or re-touched (AD-5).

    SOLE WRITER: ``coach/decision_record.py`` is the ONLY module that constructs
    or persists this model (AD-6). ``api/coach.py`` and everything else delegate
    to it and never write this table directly (a grep canary locks this).

    Conventions: UUID primary key; ``created_at``/``co_signed_at`` are tz-aware
    UTC; money inside the JSON snapshots is fixed-point decimal STRING (never
    binary float, never ``E+`` exponent notation). ``owner_id`` comes from the
    mixin (per-user isolation via the fail-closed ``ScopedRepository``, AD-10).
    """

    __tablename__ = "decision_record"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Stamps the snapshot shape for durable replay (AD-5). Write-once at propose.
    schema_version: Mapped[int] = mapped_column(nullable=False)

    # The immutable blessed snapshot of what was PROPOSED (action_label,
    # reasoning, full evidence records, uncertainties, proposed order_intent).
    # Write-once at propose; never mutated.
    recommendation_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)

    # "proposed" at insert; transitions ONCE to "cosigned" on a successful
    # execution (guarded in coach/decision_record.cosign).
    status: Mapped[str] = mapped_column(
        String(length=16), nullable=False, default="proposed"
    )

    # When the proposed record was written (tz-aware UTC). Write-once.
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Co-sign columns — NULL until co-sign fills them EXACTLY once.
    co_signed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(length=64), nullable=True
    )
    # The executed order_intent + reconciled OrderOutcome (what was EXECUTED).
    cosign_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class DigestPreference(OwnedEntityMixin, Base):
    """A user's opt-in for the weekly email digest (table: ``digest_preference``).

    Story 5.1 (FR21): the digest is Ballast's ONE sanctioned proactive touch, so
    it is strictly opt-in and OFF by default. This is a per-user owned entity
    (AD-10): reachable ONLY through the fail-closed ``ScopedRepository`` on the
    request path, and enumerated by the SYSTEM-scope batch job (``digest.job``)
    when it decides who to send to. Exactly one row per user
    (``UniqueConstraint`` on ``owner_id``).

    ``unsubscribe_token`` is an unguessable per-user secret minted at row
    creation (``secrets.token_urlsafe``). It backs the one-click, UNAUTHENTICATED
    unsubscribe link embedded in every email — the recipient must be able to opt
    out without logging in.

    ``last_sent_week`` is the idempotency marker: the ISO year-week
    (``"YYYY-Www"``) of the most recent send. The weekly job skips any user whose
    marker already equals the current week, so a re-run (or an overlapping run)
    never double-sends. ``created_at``/``updated_at`` are tz-aware UTC.
    """

    __tablename__ = "digest_preference"
    __table_args__ = (
        UniqueConstraint("owner_id", name="uq_digest_preference_owner"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Off by default — the digest is strictly opt-in (pull-not-push, FR21).
    opted_in: Mapped[bool] = mapped_column(nullable=False, default=False)

    # Per-user secret backing the unauthenticated one-click unsubscribe link.
    unsubscribe_token: Mapped[str] = mapped_column(
        String(length=64), nullable=False, unique=True, index=True
    )

    # ISO year-week ("YYYY-Www") of the last send — the job's idempotency guard.
    # NULL until the first digest is sent to this user.
    last_sent_week: Mapped[str | None] = mapped_column(
        String(length=8), nullable=True
    )

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
