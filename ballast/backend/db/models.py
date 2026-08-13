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
    Index,
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
    rows; a stale one never clobbers newer truth).

    AD-14 (Story 6.5, cash-only gap closed): the AUTHORITATIVE idle-cash source
    is now the dedicated per-user :class:`PortfolioBalance` row, NOT this table.
    The per-row ``cash`` here is a retained DENORMALIZED SNAPSHOT COPY — the
    single writer keeps writing it (harmless; it avoids a nullable ALTER and
    keeps the change schema-additive), but no reader derives idle cash from a
    holdings row anymore. A cash-only/empty account yields zero rows here yet
    still reports its true cash via ``portfolio_balance``.

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


class PortfolioBalance(OwnedEntityMixin, Base):
    """A user's account-level idle cash — the AUTHORITATIVE balances source
    (table: ``portfolio_balance``).

    AD-14 (Story 6.5, cash-only gap closed): idle cash used to be denormalized
    onto every :class:`PortfolioCache` holdings row, so an all-cash / cash-heavy
    account (few or zero holdings) surfaced ``cash = 0`` — the users with the
    MOST idle cash saw a confidently-false "no idle cash". This dedicated table
    fixes that: EXACTLY ONE logical row per user (keyed by ``owner_id`` via the
    mixin) carries the account-level ``cash`` and its snapshot ``as_of``, so cash
    survives when the holdings projection is empty.

    Written by the SAME single writer as ``portfolio_cache`` — the portfolio
    projection in ``brokers.portfolio`` (AD-14) — in the SAME reconcile commit,
    and read READ-ONLY by every consumer through the fail-closed
    ``ScopedRepository`` (AD-10, per-user isolation). Reconcile-wins keys on THIS
    row's ``as_of`` (present even for a cash-only account, unlike zero holdings
    rows), so a stale re-fetch can never clobber newer cash truth.

    Money is ``Numeric`` (Python ``Decimal``) — NEVER binary float. ``as_of`` is
    timezone-aware UTC.
    """

    __tablename__ = "portfolio_balance"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Account-level settled/idle cash — the authoritative figure (Decimal, never
    # float). One row per user; upserted in place by the single writer.
    cash: Mapped[Decimal] = mapped_column(Numeric(precision=20, scale=2), nullable=False)

    # The broker's snapshot timestamp for this balance — the reconcile-wins key
    # (AD-14), present even when the account has zero holdings.
    as_of: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # The broker's account type, normalized upper (``"MARGIN"`` / ``"CASH"``) or
    # NULL when unreported (Story 10.10). NULLABLE + purely informational — used only
    # to gently warn a margin-account user; never feeds the cash figure or any money
    # math. Written by the single writer alongside cash/as_of.
    account_type: Mapped[str | None] = mapped_column(String, nullable=True)

    # EXACTLY ONE balance row per user — the DB-level backstop for the single
    # writer's upsert (a concurrent double-insert raises IntegrityError instead
    # of silently duplicating and stranding a stale-cash row). Same discipline as
    # ``DigestPreference``.
    __table_args__ = (
        UniqueConstraint("owner_id", name="uq_portfolio_balance_owner"),
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

    LIFECYCLE (Story 6.1): the status transitions ``proposed → cosigning →
    cosigned``. A row is inserted **proposed** at ``/recommend`` time; an
    ``/approve`` first ATOMICALLY claims it (``proposed → cosigning``, the
    conditional rowcount-gated UPDATE in ``coach/decision_record.claim_for_cosign``)
    so exactly one concurrent approve places the order; on a successful placement
    it transitions ONCE to **cosigned**. A refusal/failure RELEASES the claim
    (``cosigning → proposed``) so the decision stays retryable; a transient
    ``cosigning`` row is never surfaced in history (``list_cosigned_decisions``
    filters ``status == "cosigned"``).

    IMMUTABILITY: the snapshot columns are WRITE-ONCE at propose —
    ``schema_version``, ``recommendation_snapshot`` (the blessed
    action_label/reasoning/full evidence records/uncertainties/proposed
    order_intent), and ``created_at`` are NEVER mutated after insert. The stable
    per-decision ``idempotency_key`` is minted and persisted at propose (Story
    6.1) and reused verbatim across every placement; a DB UNIQUE index makes a
    duplicate key physically un-insertable (in-convention with ``market_daily`` /
    ``digest_preference`` unique constraints). Co-sign fills the previously-NULL
    co-sign columns (``co_signed_at``, ``cosign_snapshot``) EXACTLY once, guarded
    on ``status == "cosigning"``; a cosigned record is only ever READ afterward.
    The recommendation snapshot is never re-derived or re-touched (AD-5).

    SOLE WRITER: ``coach/decision_record.py`` is the ONLY module that constructs
    or persists this model (AD-6). ``api/coach.py`` and everything else delegate
    to it and never write this table directly (a grep canary locks this).

    Conventions: UUID primary key; ``created_at``/``co_signed_at`` are tz-aware
    UTC; money inside the JSON snapshots is fixed-point decimal STRING (never
    binary float, never ``E+`` exponent notation). ``owner_id`` comes from the
    mixin (per-user isolation via the fail-closed ``ScopedRepository``, AD-10).
    """

    __tablename__ = "decision_record"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_decision_record_idempotency_key"
        ),
        # Composite index backing the paginated history read (Story 6.6): the
        # scoped ``WHERE owner_id=? AND status='cosigned' ORDER BY co_signed_at
        # DESC LIMIT ? OFFSET ?`` shape. (Built via ``create_all`` on a fresh DB;
        # a carried-over DB is reconciled with ``CREATE INDEX IF NOT EXISTS`` in
        # the test fixture and as a go-live caveat — the model will NOT ALTER an
        # already-created table, exactly as ``idempotency_key``/``broker_ref``
        # were.)
        Index(
            "ix_decision_record_owner_co_signed_at", "owner_id", "co_signed_at"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Stamps the snapshot shape for durable replay (AD-5). Write-once at propose.
    schema_version: Mapped[int] = mapped_column(nullable=False)

    # The immutable blessed snapshot of what was PROPOSED (action_label,
    # reasoning, full evidence records, uncertainties, proposed order_intent).
    # Write-once at propose; never mutated.
    recommendation_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)

    # "proposed" at insert; ATOMICALLY claimed to "cosigning" (claim_for_cosign),
    # then transitions ONCE to "cosigned" on a successful execution (cosign) — or
    # RELEASED back to "proposed" (release_claim) on a refusal/failure. The three
    # transitions all live in coach/decision_record.py (AD-6).
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
    # When the CURRENT claim entered ``cosigning`` (stamped by ``claim_for_cosign``,
    # cleared by ``release_claim``) — the RECLAIMER's bounded-age key (Story 7.2):
    # ``reclaim_orphaned_cosigning`` forward-recovers only ``cosigning`` rows whose
    # ``cosigning_at`` is older than a window, so a legitimately in-flight approve
    # is never stolen. NOT part of the immutable snapshot (it is claim-lifecycle
    # bookkeeping, not decided/executed truth); NULL means the claim time is
    # unknown, which the reclaimer treats conservatively (never reclaimed).
    # (Built via ``create_all``; adding this to the model will NOT ALTER an
    # already-created table — provisioned additively by the 7.1 startup migration,
    # exactly as ``idempotency_key``/``broker_ref`` were.)
    cosigning_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The stable per-decision idempotency key, minted+persisted at propose (Story
    # 6.1) and reused verbatim across every placement so a re-place after a
    # released claim dedupes rather than double-fills. Column stays nullable for
    # schema simplicity (a NULL never collides in Postgres), but it is populated
    # from birth; the UNIQUE index (see __table_args__) is the DB-level backstop.
    idempotency_key: Mapped[str | None] = mapped_column(
        String(length=64), nullable=True
    )
    # The executed order_intent + reconciled OrderOutcome (what was EXECUTED).
    cosign_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The broker's stable order reference, hoisted OUT of ``cosign_snapshot`` into
    # a queryable column (Story 6.3) so a later explicit reconcile (Story 6.7) can
    # find an ambiguous placement by ``broker_ref`` without parsing JSON. NULL when
    # the broker assigned no reference (e.g. a true no-order_id timeout). Indexed
    # for the explicit-reconcile lookup. (Built via ``create_all``; adding this to
    # the model will NOT ALTER an already-created table — a go-live caveat,
    # exactly as ``idempotency_key`` was.)
    broker_ref: Mapped[str | None] = mapped_column(
        String(length=64), nullable=True, index=True
    )
    # The DURABLE cross-request reconciliation of an ambiguous placement (Story
    # 6.7). ADDITIVE and write-LATEST (reconciliation may run repeatedly as an
    # order progresses ``pending → filled``, latest-known-truth). The immutable
    # ``recommendation_snapshot``/``cosign_snapshot`` are NEVER mutated — the
    # broker's later truth lands HERE — so verbatim replay of those snapshots
    # stays byte-identical. ``reconciliation_snapshot`` holds the reconciled
    # ``OrderOutcome`` in the SAME shape as ``cosign_snapshot.outcome`` (money as
    # fixed-point strings); ``reconciled_at`` is tz-aware UTC. Both NULL until a
    # reconcile runs. (Built via ``create_all``; adding these to the model will
    # NOT ALTER an already-created table — a go-live schema step, exactly as
    # ``idempotency_key``/``broker_ref`` were.)
    reconciliation_snapshot: Mapped[dict | None] = mapped_column(
        JSON, nullable=True
    )
    reconciled_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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


class PendingBuy(OwnedEntityMixin, Base):
    """A durable, deferred buy waiting for settled cash (table: ``pending_buy``, Epic 9).

    Story 9.3 (Just-in-time liquidation + deferred/resumed buy): a per-user owned
    entity (AD-10) — reachable ONLY through the fail-closed ``ScopedRepository`` —
    that DURABLY records a decided buy whose amount exceeded the user's
    instantly-spendable ``ready_to_trade`` cash at the buy step. The intent must
    survive restarts and a missed/unseen web notification, so it lives in its own
    table (not in memory, not on the pure ``portfolio_cache`` projection). It
    persists until the user resumes it (once ``ready_to_trade`` covers it) or
    cancels it — pull-only, no background settlement poller.

    ``buy_intent`` is the original :class:`~coach.recommendation.OrderIntent`
    snapshot (JSON, money as fixed-point strings — the SAME shape the decision
    snapshots use) so the resumed buy is pre-filled EXACTLY as the user first
    stated it. ``amount`` is HOISTED out of that JSON into its own ``Numeric(20,2)``
    column so ``funds_ready`` (``ready_to_trade >= amount``) is a direct decimal
    compare, never a JSON parse. ``sell_decision_id`` links back to the proposed
    money-market SELL decision (``decision_record``) when the shortfall was
    coverable (``None`` when there was nothing to liquidate).

    ``status`` is one of ``'awaiting_funds'`` (the live, resumable state),
    ``'resumed'`` (the user resumed it — a proposed buy decision was minted), or
    ``'cancelled'``. The ``(owner_id, status)`` index backs the scoped
    ``awaiting_funds`` list read.

    Money is ``Numeric`` (Python ``Decimal``) — NEVER binary float. Timestamps are
    timezone-aware UTC.
    """

    __tablename__ = "pending_buy"
    __table_args__ = (
        Index("ix_pending_buy_owner_status", "owner_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # The original OrderIntent snapshot (money as fixed-point strings), so the
    # resumed buy is pre-filled exactly as the user first stated it.
    buy_intent: Mapped[dict] = mapped_column(JSON, nullable=False)

    # The buy amount hoisted out of ``buy_intent`` for a direct ``funds_ready``
    # decimal compare (``ready_to_trade >= amount``). Decimal, never float.
    amount: Mapped[Decimal] = mapped_column(
        Numeric(precision=20, scale=2), nullable=False
    )

    # 'awaiting_funds' at insert; transitions ONCE to 'resumed' (on resume) or
    # 'cancelled' (on cancel).
    status: Mapped[str] = mapped_column(
        String(length=16), nullable=False, default="awaiting_funds"
    )

    # Links back to the proposed money-market SELL decision when the shortfall was
    # coverable; NULL when there was nothing to liquidate.
    sell_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True
    )

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # When the user resumed / cancelled this pending buy (NULL until it happens).
    resumed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CashConfig(OwnedEntityMixin, Base):
    """A user's honest cash configuration (table: ``cash_config``, Epic 9).

    Story 9.1 (Cash Intelligence): a per-user owned entity (AD-10) — reachable
    ONLY through the fail-closed ``ScopedRepository``, exactly one row per user
    (``UniqueConstraint`` on ``owner_id``) — that records how the user actually
    keeps their money, so every later cash nudge (9-2/9-3) is honest and only
    ever about money they'd genuinely invest.

    Two columns disambiguate the THREE reserve states cleanly (the honesty crux,
    AC2) — a "never-decided" reserve is NEVER silently treated as ``0``:

    - **never-decided:** ``reserve_decided = False`` (the default for a brand-new
      user). This is what triggers the calm one-time set-or-decline prompt (AC6);
      it must not be read as ``0``.
    - **declined:** ``reserve_decided = True``, ``reserve_amount = NULL`` — the
      user explicitly keeps no reserve; resolves to ``Decimal("0")``.
    - **set:** ``reserve_decided = True``, ``reserve_amount = X`` (``X >= 0``;
      exactly ``0`` is a legitimate explicit set).

    ``parked_symbols`` is the user-specified set of holding symbols to treat as
    money-market "parked" cash-equivalents (e.g. ``SWVXX``) — NEVER auto-classified
    (v1 is user-specified: simpler and honest). Stored normalized (trimmed,
    upper-cased, de-duplicated). It only affects display/classification for
    symbols the user actually holds; an unknown/unheld symbol is stored but simply
    matches nothing.

    Money is ``Numeric`` (Python ``Decimal``) — NEVER binary float.
    ``created_at``/``updated_at`` are tz-aware UTC.
    """

    __tablename__ = "cash_config"
    __table_args__ = (
        UniqueConstraint("owner_id", name="uq_cash_config_owner"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # The user's declared reserve — cash they never want touched. NULL means
    # either "never decided" or "declined" (disambiguated by ``reserve_decided``);
    # a non-NULL value is an explicit set (>= 0). Decimal, never float.
    reserve_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=20, scale=2), nullable=True
    )

    # Has the user made an explicit reserve decision (set OR declined)? False
    # until they act — the "never-decided" state that must NOT be read as 0.
    reserve_decided: Mapped[bool] = mapped_column(nullable=False, default=False)

    # User-tagged money-market/parked symbols (normalized upper-case list). A
    # brand-new user has none.
    parked_symbols: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class TargetAllocationConfig(OwnedEntityMixin, Base):
    """A user's chosen target-allocation model portfolio (table:
    ``target_allocation_config``, Epic 10).

    Story 10.1 (Allocation Coach): a per-user owned entity (AD-10) — reachable
    ONLY through the fail-closed ``ScopedRepository``, exactly one row per user
    (``UniqueConstraint`` on ``owner_id``) — that records which named model
    portfolio the user picked (Conservative / Balanced / Growth). It is the
    "where my money should be" that Story 10-2's gap-to-target engine measures
    against.

    ``model_key`` is the string key of a model in
    :data:`strategy.target_allocation.MODEL_PORTFOLIOS` (validated on write), or
    ``NULL`` for the honest **undecided** state — a brand-new user has no target,
    which drives the calm one-time set-or-decline prompt and is NEVER silently
    treated as a default model. Twin of :class:`CashConfig`.

    ``created_at``/``updated_at`` are tz-aware UTC.
    """

    __tablename__ = "target_allocation_config"
    __table_args__ = (
        UniqueConstraint("owner_id", name="uq_target_allocation_config_owner"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # The chosen model portfolio's key (e.g. "balanced"), or NULL when the user
    # has not yet picked a target. Validated against the known models on write.
    model_key: Mapped[str | None] = mapped_column(String(length=32), nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
