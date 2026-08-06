"""The SOLE writer/reader of :class:`~db.models.DecisionRecord` (FR16, AD-5/AD-6).

The Coach Engine is the ONLY writer of decision records: every construction,
insert, and co-sign of a :class:`~db.models.DecisionRecord` goes through THIS
module. ``api/coach.py`` delegates here and never touches the model itself (a
grep canary locks this, mirroring the AD-7 sole-``place_order``-caller canary).

Three-state lifecycle (Story 6.1): ``proposed → cosigning → cosigned``. The
atomic ``cosigning`` claim closes the in-flight concurrent-approve window — only
the transaction that wins the conditional ``proposed → cosigning`` UPDATE ever
places the order; a refusal/failure RELEASES the claim (``cosigning → proposed``)
so the decision stays retryable.

Facets, one immutable row:

- :func:`record_proposal` writes ONE **proposed** record at ``/recommend`` time —
  an immutable snapshot of the blessed recommendation (action_label, reasoning,
  full evidence records, uncertainties, proposed order_intent) stamped with a
  :data:`DECISION_RECORD_SCHEMA_VERSION` for durable replay (AD-5) AND a STABLE
  per-decision ``idempotency_key`` (minted HERE, once, via
  :func:`~coach.execution.mint_idempotency_key`) that every placement of the
  decision reuses verbatim. It goes through the fail-closed
  :class:`~db.repository.ScopedRepository`, so the owner is stamped from the scope
  (a user can never forge a row owned by someone else).
- :func:`claim_for_cosign` performs the atomic, scoped, rowcount-gated
  ``proposed → cosigning`` UPDATE and COMMITS it (releasing the row lock) BEFORE
  the broker is ever called — so no DB row lock is held across the ``place_order``
  network call. It is the ONLY transition into ``cosigning`` and returns whether
  the caller won (``rowcount == 1``).
- :func:`release_claim` reverses an unfulfilled claim (``cosigning → proposed``,
  scoped, committed) so a refused/failed decision returns to ``proposed`` and can
  be approved again; idempotent (a no-op if the row is not ``cosigning``).
- :func:`cosign` fills the previously-NULL co-sign columns EXACTLY once (guarded
  on ``status == "cosigning"`` — only a CLAIMED record may be cosigned):
  ``status="cosigned"``, ``co_signed_at``, the executed ``order_intent``, and the
  reconciled ``OrderOutcome``. The ``idempotency_key`` is already set at propose
  and is NOT re-written. The recommendation snapshot / ``schema_version`` /
  ``created_at`` are NEVER mutated.
- :func:`load_decision` reads a record by id THROUGH the scoped repository (a row
  owned by another user is invisible → ``None``).

Money inside every persisted JSON snapshot is a fixed-point decimal STRING via
``format(Decimal, "f")`` — never binary float, never ``E+`` exponent notation
(so an extreme amount never serializes as ``"1E+27"``). Timestamps are tz-aware
UTC. Callers control ``commit`` on the session (the repository only flushes).
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from brokers.port import OrderOutcome, OrderStatus
from coach.execution import mint_idempotency_key
from coach.recommendation import Duration, OrderIntent, OrderType, Session
from coach.validation import BlessedRecommendation
from db.atomic import conditional_claim, lock_row
from db.models import DecisionRecord
from db.repository import ScopedRepository
from db.scope import Scope
from money import format_money

logger = logging.getLogger("ballast.coach.decision_record")

#: The snapshot-shape version stamped onto every proposed record (AD-5). Bump
#: this when the persisted snapshot shape changes so replay can adapt.
DECISION_RECORD_SCHEMA_VERSION = 1

#: Column width of ``decision_record.broker_ref`` (VARCHAR(64), see ``db.models``).
#: A broker reference is written to this queryable column at placement
#: (:func:`persist_broker_ref`) and again at :func:`cosign`. A value longer than
#: the column would raise a DB ``DataError`` at commit — AFTER a live order already
#: exists — converting a successful placement into an uncaught 500 with the row
#: stranded in ``cosigning`` and NO durable ref (the exact zombie Story 7.2
#: eliminates). A real Schwab order id is a short integer, far under this, so the
#: guard below never triggers in practice; it is a defensive backstop for a
#: malformed/oversized ref from any broker.
BROKER_REF_MAX_LEN = 64


def _fit_broker_ref(broker_ref: str | None) -> str | None:
    """Clamp a broker reference to the queryable column width (defensive).

    Returns the ref unchanged when it fits — the only case for a real Schwab order
    id. If it somehow exceeds :data:`BROKER_REF_MAX_LEN`, logs a warning and
    truncates so the post-placement write can never raise a ``DataError`` after a
    live order was placed: a truncated-but-present ref in the queryable column
    (with the FULL ref still preserved verbatim in ``cosign_snapshot`` /
    ``OrderOutcome``) is strictly more recoverable than a stranded row with none.
    """
    if broker_ref is not None and len(broker_ref) > BROKER_REF_MAX_LEN:
        logger.warning(
            "broker_ref length %d exceeds column width %d; truncating for the "
            "queryable column (full ref preserved in the cosign snapshot).",
            len(broker_ref),
            BROKER_REF_MAX_LEN,
        )
        return broker_ref[:BROKER_REF_MAX_LEN]
    return broker_ref


def _money(value: Decimal) -> str:
    """Return a fixed-point decimal STRING (never binary float, never ``E+``).

    ``format(Decimal, "f")`` renders in plain fixed-point notation, so an extreme
    amount serializes as e.g. ``"1000000000000000000000000000"`` rather than the
    lossy/exponent ``str(Decimal)`` form ``"1E+27"``. Delegates to the shared
    :func:`money.format_money` (the single money→wire serializer) so this snapshot
    string and the live ``/approve`` outcome cannot silently diverge.
    """
    return format_money(value)


def _order_intent_json(order_intent: OrderIntent) -> dict:
    """Serialize an :class:`OrderIntent` for a JSON snapshot (money fixed-point).

    OMIT-WHEN-DEFAULT (Story 8.1, regression-critical): the base
    ``{symbol, side, amount}`` shape is always emitted; the order-model fields are
    added ONLY when non-default — ``order_type`` when ``!= MARKET``, ``session``
    when ``!= REGULAR``, ``duration`` when ``!= DAY``, and ``limit_price`` /
    ``stop_price`` only when not ``None``. So a MARKET intent serializes to exactly
    ``{symbol, side, amount}`` (byte-identical to pre-8.1, AC 5 — the
    ``schema_version == 1`` / ``_ORDER_INTENT_JSON`` regression guards stay green)
    while a LIMIT intent additively carries ``order_type: "limit"`` + ``limit_price``
    (AC 1). This keeps :data:`DECISION_RECORD_SCHEMA_VERSION` at 1 (a pure additive
    superset needing no replay adaptation).
    """
    result = {
        "symbol": order_intent.symbol,
        "side": order_intent.side.value,
        "amount": _money(order_intent.amount),
    }
    if order_intent.order_type != OrderType.MARKET:
        result["order_type"] = order_intent.order_type.value
    if order_intent.limit_price is not None:
        result["limit_price"] = _money(order_intent.limit_price)
    if order_intent.stop_price is not None:
        result["stop_price"] = _money(order_intent.stop_price)
    if order_intent.session != Session.REGULAR:
        result["session"] = order_intent.session.value
    if order_intent.duration != Duration.DAY:
        result["duration"] = order_intent.duration.value
    return result


def _snapshot(blessed: BlessedRecommendation) -> dict:
    """Freeze the blessed recommendation into the immutable proposed snapshot.

    Captures action_label, reasoning, the proposed ``order_intent`` (or ``None``),
    the FULL evidence records (AD-12 ``EvidenceRecord.to_dict()`` shape — precedent
    snapshotted at decision time, never recomputed later), and uncertainties.
    """
    order_intent = (
        None if blessed.order_intent is None else _order_intent_json(blessed.order_intent)
    )
    return {
        "action_label": blessed.action_label,
        "reasoning": blessed.reasoning,
        "order_intent": order_intent,
        "evidence": [record.to_dict() for record in blessed.evidence],
        "uncertainties": list(blessed.uncertainties),
    }


async def record_proposal(
    blessed: BlessedRecommendation,
    *,
    scope: Scope,
    session: AsyncSession,
) -> DecisionRecord:
    """Persist ONE **proposed** decision record for the blessed recommendation.

    Writes the immutable snapshot (plus :data:`DECISION_RECORD_SCHEMA_VERSION`)
    AND a STABLE per-decision ``idempotency_key`` (Story 6.1) — minted HERE, once,
    via :func:`~coach.execution.mint_idempotency_key`, and reused verbatim across
    every placement of this decision so a re-place after a released claim dedupes
    rather than double-fills. Goes through the fail-closed
    :class:`~db.repository.ScopedRepository`, so the owner is stamped from
    ``scope``. Returns the flushed row (its ``id`` is populated); the CALLER
    commits.
    """
    repo = ScopedRepository(DecisionRecord, scope, session)
    return await repo.add(
        schema_version=DECISION_RECORD_SCHEMA_VERSION,
        recommendation_snapshot=_snapshot(blessed),
        status="proposed",
        idempotency_key=mint_idempotency_key(),
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )


async def claim_for_cosign(
    decision_id: UUID,
    *,
    scope: Scope,
    session: AsyncSession,
) -> bool:
    """Atomically claim a **proposed** record for co-sign (proposed→cosigning).

    The ONLY transition into ``cosigning`` (AD-6, sole writer). Issues a scoped,
    conditional ``UPDATE decision_record SET status='cosigning' WHERE id=:id AND
    owner_id=:scope_user AND status='proposed'`` — Postgres serializes the row
    update, so of any number of concurrent approves EXACTLY ONE gets
    ``rowcount == 1`` (the winner) and every other gets ``rowcount == 0``. The
    claim is COMMITTED here, releasing the row lock BEFORE the broker is called —
    no lock is held across the ``place_order`` network round-trip (the point of
    the intermediate state). Returns ``True`` iff this caller won the claim.

    Requires a USER scope (the approve seam is never system-scoped); the explicit
    ``owner_id`` predicate is what enforces per-user isolation on the raw UPDATE.

    Routes the conditional UPDATE through the generalized
    :func:`~db.atomic.conditional_claim` primitive (Story 7.2) — the ONE shared
    atomic-claim mechanism — while KEEPING the commit + system-scope-rejection
    guards here (the primitive is model-agnostic and does not commit). Also stamps
    ``cosigning_at`` (tz-aware UTC now) so a later crash-orphaned ``cosigning`` row
    carries a bounded-age key the reclaimer can safely act on.
    """
    if scope.is_system:
        raise ValueError(
            "claim_for_cosign requires a USER scope; the approve seam is never "
            "system-scoped (fail-closed)."
        )
    won = await conditional_claim(
        session,
        update(DecisionRecord)
        .where(
            DecisionRecord.id == decision_id,
            DecisionRecord.owner_id == scope.user_id,
            DecisionRecord.status == "proposed",
        )
        .values(
            status="cosigning",
            cosigning_at=datetime.datetime.now(datetime.timezone.utc),
        ),
    )
    await session.commit()
    return won


async def release_claim(
    decision_id: UUID,
    *,
    scope: Scope,
    session: AsyncSession,
) -> None:
    """Release an unfulfilled claim (cosigning→proposed), scoped + committed.

    Reverses a :func:`claim_for_cosign` that did NOT reach co-sign (a refusal or
    failure), so the decision returns to ``proposed`` and can be approved again —
    it is never stuck in ``cosigning``. Idempotent: a scoped, conditional
    ``UPDATE … SET status='proposed' WHERE id AND owner AND status='cosigning'``
    is a no-op (``rowcount == 0``) if the row is not currently claimed. Sole
    writer (AD-6); requires a USER scope.

    Routes through the shared :func:`~db.atomic.conditional_claim` primitive (Story
    7.2), keeping the commit + system-scope guard here. Also CLEARS ``cosigning_at``
    back to ``NULL`` — the row is no longer in a claim, so it must not carry a stale
    age key the reclaimer could later act on.
    """
    if scope.is_system:
        raise ValueError(
            "release_claim requires a USER scope; the approve seam is never "
            "system-scoped (fail-closed)."
        )
    await conditional_claim(
        session,
        update(DecisionRecord)
        .where(
            DecisionRecord.id == decision_id,
            DecisionRecord.owner_id == scope.user_id,
            DecisionRecord.status == "cosigning",
        )
        .values(status="proposed", cosigning_at=None),
    )
    await session.commit()


async def persist_broker_ref(
    decision_id: UUID,
    broker_ref: str,
    *,
    scope: Scope,
    session: AsyncSession,
) -> bool:
    """Persist ``broker_ref`` DURABLY on a claimed row, in its own commit (7.2).

    The recoverable-placement step: called the instant ``place_order`` returns a
    ``broker_ref``, BEFORE the cosign, so the queryable reference is durable
    independent of the later cosign/commit. If that cosign/commit then fails, the
    row stays ``cosigning`` but carries ``broker_ref`` (recoverable by the 6.7
    reconcile or the reclaimer) instead of the old place-then-persist-in-one-commit
    zombie with a NULL ``broker_ref``.

    A scoped, rowcount-gated conditional ``UPDATE … SET broker_ref WHERE id AND
    owner_id AND status='cosigning'`` routed through the shared
    :func:`~db.atomic.conditional_claim` primitive, then committed here. Gated on
    ``status='cosigning'`` so only the caller's own live claim is written (a raced
    release/cosign makes it a no-op → ``False``). Sole writer (AD-6); requires a
    USER scope. Returns whether the ref was persisted (``rowcount == 1``).
    """
    if scope.is_system:
        raise ValueError(
            "persist_broker_ref requires a USER scope; the approve seam is never "
            "system-scoped (fail-closed)."
        )
    won = await conditional_claim(
        session,
        update(DecisionRecord)
        .where(
            DecisionRecord.id == decision_id,
            DecisionRecord.owner_id == scope.user_id,
            DecisionRecord.status == "cosigning",
        )
        .values(broker_ref=_fit_broker_ref(broker_ref)),
    )
    await session.commit()
    return won


async def lock_decision(
    decision_id: UUID,
    *,
    scope: Scope,
    session: AsyncSession,
) -> DecisionRecord | None:
    """Load a decision record UNDER a row lock (``SELECT … FOR UPDATE``), scoped (7.2).

    Serializes concurrent local persists on ONE decision across sessions via the
    shared :func:`~db.atomic.lock_row` primitive: a second reconcile of the same
    decision blocks on the ``FOR UPDATE`` until the first commits, then reads the
    just-committed state — which is what makes ``record_reconciliation``'s in-writer
    monotonic terminal guard (its guard lives in a JSON column, not a portable
    ``WHERE``) effective across sessions, so a transiently-worse read can never
    regress a persisted terminal money truth. A foreign/unknown id is invisible →
    ``None`` (the API maps that to 404). Requires a USER scope (the reconcile seam
    is never system-scoped); the explicit ``owner_id`` predicate enforces per-user
    isolation on the raw locked select.
    """
    if scope.is_system:
        raise ValueError(
            "lock_decision requires a USER scope; the reconcile seam is never "
            "system-scoped (fail-closed)."
        )
    return await lock_row(
        session, DecisionRecord, entity_id=decision_id, owner_id=scope.user_id
    )


async def load_decision(
    decision_id: UUID,
    *,
    scope: Scope,
    session: AsyncSession,
) -> DecisionRecord | None:
    """Load a decision record by id THROUGH the scoped repository (per-user).

    A row owned by another user is invisible under a user scope → returns
    ``None`` (the API maps that to a 404).
    """
    repo = ScopedRepository(DecisionRecord, scope, session)
    return await repo.get(decision_id)


@dataclass
class DecisionPage:
    """One bounded page of co-signed decision records (Story 6.6).

    ``rows`` are the (at most ``limit``) newest-first cosigned records for the
    page; ``has_more`` is ``True`` when at least one further row exists beyond
    this page (computed by fetching ``limit + 1`` and trimming the extra).
    """

    rows: list[DecisionRecord]
    has_more: bool


async def list_cosigned_decisions(
    *,
    scope: Scope,
    session: AsyncSession,
    limit: int,
    offset: int = 0,
) -> DecisionPage:
    """List a bounded page of THIS user's co-signed decisions, newest first (6.6).

    Reads the owner's rows THROUGH the fail-closed
    :class:`~db.repository.ScopedRepository` (a foreign row is never visible).
    Scoping, the ``status == "cosigned"`` filter, ``co_signed_at`` DESC ordering,
    and the ``limit``/``offset`` window ALL execute in SQL via
    :meth:`~db.repository.ScopedRepository.list_page` — the whole table is never
    loaded (Story 6.6, backed by the ``(owner_id, co_signed_at)`` composite
    index). ``limit + 1`` rows are fetched so an extra row signals a further page
    (``has_more``) without a second COUNT query; the extra is trimmed off.
    Read-only — this SOLE reader never mutates or re-derives a record
    (AD-5/AD-6). The CALLER owns any commit (there is nothing to commit here).
    """
    repo = ScopedRepository(DecisionRecord, scope, session)
    rows = await repo.list_page(
        order_by=DecisionRecord.co_signed_at,
        descending=True,
        limit=limit + 1,
        offset=offset,
        filters=(DecisionRecord.status == "cosigned",),
    )
    has_more = len(rows) > limit
    return DecisionPage(rows=rows[:limit], has_more=has_more)


async def prune_stale_proposed_decisions(
    *,
    session: AsyncSession,
    older_than_days: int,
    now: datetime.datetime | None = None,
) -> int:
    """Delete never-co-signed **proposed** records older than the window (6.6).

    The SYSTEM-scope retention writer for never-co-signed records: a ``proposed``
    row was never executed (no order exists, its ``idempotency_key`` never reached
    the broker), so deleting stale ones bounds the table. Issues a single
    ``DELETE FROM decision_record WHERE status='proposed' AND created_at < cutoff``
    where ``cutoff = now - older_than_days``, commits, and returns the number of
    rows deleted.

    The predicate HARD-PINS ``status == "proposed"`` so a ``cosigned`` (or a
    transient ``cosigning``) row can NEVER be deleted — cosigned records are
    immutable and permanent; their on-the-record history is preserved. This is
    the SOLE writer module for :class:`~db.models.DecisionRecord` (AD-6); the
    delete lives here and nowhere else.

    ``older_than_days`` must be non-negative: a negative window would push the
    cutoff into the FUTURE and delete recent (even seconds-old) proposed rows, so
    a misconfigured retention setting is refused rather than silently destructive.
    """
    if older_than_days < 0:
        raise ValueError(
            "older_than_days must be >= 0 "
            f"(got {older_than_days}); a negative window would prune recent "
            "proposed records."
        )
    cutoff = (
        now or datetime.datetime.now(datetime.timezone.utc)
    ) - datetime.timedelta(days=older_than_days)
    result = await session.execute(
        delete(DecisionRecord).where(
            DecisionRecord.status == "proposed",
            DecisionRecord.created_at < cutoff,
        )
    )
    await session.commit()
    return result.rowcount


#: The indeterminate/non-terminal outcome status the reclaimer stamps onto a
#: forward-recovered orphan (Story 7.2). ``pending`` is non-terminal (reconcilable),
#: so the recovered record is re-reconcilable via the 6.7 reconcile and is NEVER
#: presented as a confirmed fill. Carries the persisted ``broker_ref`` (may be NULL).
_RECLAIM_OUTCOME_STATUS = OrderStatus.PENDING


def _recovery_cosign_snapshot(record: DecisionRecord) -> dict:
    """Build the recovery ``cosign_snapshot`` for a reclaimed orphan (Story 7.2).

    The forward-recovery snapshot for a crash-orphaned ``cosigning`` row: the
    EXECUTED ``order_intent`` is taken verbatim from the immutable proposed
    ``recommendation_snapshot['order_intent']`` (already a fixed-point-money JSON
    dict — never recomputed, AD-5), and the outcome is an INDETERMINATE
    (non-terminal) :data:`_RECLAIM_OUTCOME_STATUS` carrying the persisted
    ``broker_ref`` (which may be NULL), ``filled_qty`` ``"0"`` and ``avg_price``
    ``None`` — all money as fixed-point strings, the SAME shape
    :func:`cosign`/:func:`record_reconciliation` write. This makes the row a
    re-reconcilable, needs-reconfirmation cosigned record: the reclaimer NEVER
    re-places, searches, or guesses a fill — an ambiguous placement stays pending
    for the 6.7 reconcile or an explicit human re-confirmation.
    """
    snapshot = record.recommendation_snapshot or {}
    # Pass the proposed order_intent JSON through verbatim (already money-fixed).
    # A ``None`` proposed intent (possible on the offline default plan) still
    # yields a well-formed snapshot with a null executed intent.
    #
    # KNOWN degraded-recovery behavior (Story 8.1, by design — NOT a regression):
    # the executed intent is taken from the coach's PROPOSED (MARKET-only)
    # snapshot, but a LIMIT's ``order_type``/``limit_price`` are human-entered at
    # /approve and never enter the proposed snapshot. So a crash-orphaned LIMIT
    # order forward-recovered here snapshots as MARKET. This is out of scope for
    # Story A (limit fields are human-entered; resting-order lifecycle is Story B)
    # — left intentionally so a future reviewer doesn't flag it as a regression.
    return {
        "order_intent": snapshot.get("order_intent"),
        "outcome": {
            "status": _RECLAIM_OUTCOME_STATUS.value,
            "filled_qty": _money(Decimal("0")),
            "avg_price": None,
            "broker_ref": record.broker_ref,
            "account_ref": None,
        },
    }


async def reclaim_orphaned_cosigning(
    *,
    session: AsyncSession,
    older_than: datetime.timedelta,
    now: datetime.datetime | None = None,
    limit: int | None = None,
) -> int:
    """Forward-recover crash-orphaned ``cosigning`` rows older than a window (7.2).

    The SYSTEM-scope reclaimer for the go-live "permanent zombie" gap: a
    ``cosigning`` row orphaned by a mid-claim crash is invisible to both history
    (which filters ``status == "cosigned"``) and the pruner (which only touches
    ``proposed``), so without a reclamation path it strands forever — possibly with
    a LIVE order. This forward-recovers each such orphan to a ``cosigned``,
    indeterminate, needs-reconfirmation, re-reconcilable record so the ordinary 6.7
    reconcile (by ``broker_ref``) or a human resolves it.

    BOUNDED + SAFE:

    - Only ``cosigning`` rows whose ``cosigning_at`` is STRICTLY older than
      ``now - older_than`` are candidates — a legitimately in-flight approve
      (within the window) is NEVER touched. A NULL ``cosigning_at`` (claim time
      unknown) is treated conservatively and NEVER reclaimed (the ``<`` comparison
      excludes NULL), to avoid acting on anything ambiguous.
    - Each transition is a rowcount-gated conditional ``cosigning → cosigned``
      UPDATE through the shared :func:`~db.atomic.conditional_claim` primitive that
      stamps ``co_signed_at`` and a recovery ``cosign_snapshot`` (executed intent
      from the proposed snapshot; INDETERMINATE outcome carrying the persisted
      ``broker_ref``). Because a RACING live cosign also does ``cosigning →
      cosigned``, the rowcount gate makes the reclaimer a no-op if it lost — the
      live cosign wins and the reclaimer never double-completes.
    - NEVER re-places an order, searches, or guesses a fill; NEVER releases a
      possibly-placed row back to ``proposed`` (which would permit a double-place).

    Idempotent: a re-run finds no ``cosigning`` rows matching (the prior run moved
    them to ``cosigned``) → a full no-op. A negative ``older_than`` is refused
    (it would pull the cutoff into the FUTURE and reclaim just-claimed in-flight
    rows). ``now`` is injectable for tests. Returns the count this call actually
    transitioned (rowcount == 1 each). SYSTEM-scope: the reclaimer spans all owners
    by construction (a background recovery has no single user), so it queries +
    writes without a per-user cage — but stays the sole writer of the model (AD-6),
    every ``update(DecisionRecord)`` living here.

    BOUNDED + ISOLATED for unattended scheduling (2026-08-06 hardening):

    - ``limit`` caps how many candidate orphans one call loads and processes, so a
      backlog can never load the whole table into memory. Any overflow is left for
      the next tick. ``None`` means unbounded (the original behavior). A
      non-positive ``limit`` is refused.
    - Each reclamation is COMMITTED PER ROW inside its own ``try``/``except``. One
      poison row (e.g. a DB error on its UPDATE/commit) is rolled back and skipped
      — it never discards the reclamations already committed earlier in the batch
      nor aborts the remaining candidates (it is simply retried next tick). This
      replaces the earlier single end-commit, where one failure lost the whole
      batch.
    """
    if older_than < datetime.timedelta(0):
        raise ValueError(
            "older_than must be non-negative "
            f"(got {older_than!r}); a negative window would reclaim recent, "
            "legitimately in-flight cosigning rows."
        )
    if limit is not None and limit <= 0:
        raise ValueError(f"limit must be positive when provided (got {limit!r}).")
    effective_now = now or datetime.datetime.now(datetime.timezone.utc)
    if effective_now.tzinfo is None:
        # Defensive: an injected naive ``now`` would yield a naive cutoff compared
        # against the tz-aware ``cosigning_at`` (TIMESTAMPTZ) and stamp a naive
        # ``co_signed_at`` — normalize to UTC (tz-aware everywhere else).
        effective_now = effective_now.replace(tzinfo=datetime.timezone.utc)
    cutoff = effective_now - older_than

    # Enumerate candidate orphans (SYSTEM scope spans all owners). A NULL
    # ``cosigning_at`` is excluded by the ``<`` comparison — never reclaimed.
    # ``limit`` bounds per-call memory/work; overflow waits for the next tick.
    stmt = select(DecisionRecord).where(
        DecisionRecord.status == "cosigning",
        DecisionRecord.cosigning_at.isnot(None),
        DecisionRecord.cosigning_at < cutoff,
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    candidates = list(result.scalars().all())

    # Materialize each row's recovery data (id + recovery snapshot) BEFORE the
    # per-row commits below. Snapshot build is pure (no I/O) over already-loaded
    # columns; doing it up front keeps the loop from touching ORM objects that a
    # commit could expire (safe here — the app session maker sets
    # ``expire_on_commit=False`` — but robust regardless of the caller's session).
    planned = [(record.id, _recovery_cosign_snapshot(record)) for record in candidates]

    reclaimed = 0
    for record_id, recovery_snapshot in planned:
        # Rowcount-gated cosigning→cosigned committed PER ROW: a racing live cosign
        # wins the gate (this no-ops); a per-row failure is isolated so it can
        # neither undo the already-committed reclamations nor abort the batch.
        try:
            won = await conditional_claim(
                session,
                update(DecisionRecord)
                .where(
                    DecisionRecord.id == record_id,
                    DecisionRecord.status == "cosigning",
                )
                .values(
                    status="cosigned",
                    co_signed_at=effective_now,
                    cosign_snapshot=recovery_snapshot,
                ),
            )
            if won:
                await session.commit()
                reclaimed += 1
            else:
                # Lost the rowcount gate to a racing live cosign — discard the
                # no-op transaction so the next row starts clean.
                await session.rollback()
        except Exception:
            logger.exception(
                "reclaim_orphaned_cosigning: failed to reclaim record id=%s; "
                "rolled back this row and continuing (retried next run).",
                record_id,
            )
            await session.rollback()
    return reclaimed


def cosign(
    record: DecisionRecord,
    *,
    order_intent: OrderIntent,
    outcome: OrderOutcome,
    idempotency_key: str,
) -> None:
    """Co-sign a **cosigning** (claimed) record EXACTLY once (cosigning→cosigned).

    Fills the previously-NULL co-sign columns: ``status="cosigned"``,
    ``co_signed_at`` (tz-aware UTC now), and ``cosign_snapshot`` (the EXECUTED
    order_intent + reconciled outcome, money fixed-point). Guarded on
    ``record.status == "cosigning"`` (Story 6.1) so co-sign is reachable only from
    a record the caller atomically claimed via :func:`claim_for_cosign`, and is
    still one-shot. The ``idempotency_key`` is already persisted at propose and is
    NOT re-written; the ``idempotency_key`` argument (the key actually placed) is
    asserted to match the persisted key so a caller can never silently place under
    a different key than the stable one. The recommendation snapshot /
    ``schema_version`` / ``created_at`` are never touched. The CALLER commits.
    """
    if record.status != "cosigning":
        raise ValueError(
            "Only a claimed (cosigning) decision record can be co-signed "
            f"(co-sign is one-shot); record status is {record.status!r}."
        )
    if record.idempotency_key != idempotency_key:
        raise ValueError(
            "The placed idempotency key must match the stable key persisted at "
            "propose (the decision key is stable for the life of the decision)."
        )
    record.status = "cosigned"
    record.co_signed_at = datetime.datetime.now(datetime.timezone.utc)
    # Hoist the broker reference into its queryable column (Story 6.3) in addition
    # to the snapshot JSON below, so a later explicit reconcile (Story 6.7) can
    # find the order by ``broker_ref`` directly. NULL when the broker assigned
    # none (e.g. a no-order_id timeout surfaced as pending). Clamped to the column
    # width so an oversized ref can't DataError post-placement; the full ref is
    # preserved verbatim in the snapshot below.
    record.broker_ref = _fit_broker_ref(outcome.broker_ref)
    record.cosign_snapshot = {
        "order_intent": _order_intent_json(order_intent),
        "outcome": {
            "status": outcome.status.value,
            "filled_qty": _money(outcome.filled_qty),
            "avg_price": None if outcome.avg_price is None else _money(outcome.avg_price),
            "broker_ref": outcome.broker_ref,
            "account_ref": outcome.account_ref,
        },
    }


#: The terminal (settled) outcome statuses — a reconcile short-circuits on these
#: (nothing more to read; the broker's answer is final). As of Story 8.2
#: ``partial`` is NO LONGER terminal: a partially-filled order can still advance to
#: ``filled`` (the Epic 6 partial-fill decision), so a reconcile re-reads it —
#: ``record_reconciliation`` supplies the advance-only guard that keeps it from
#: regressing. ``timeout``/``pending``/``partial`` are all NON-terminal here.
_TERMINAL_STATUSES: frozenset[str] = frozenset({"filled", "rejected"})


def _is_terminal(status: str) -> bool:
    """True iff ``status`` is a settled/terminal outcome (filled/rejected).

    A terminal outcome is the broker's final answer — a reconcile returns it
    unchanged and never re-reads the broker. ``timeout``/``pending``/``partial`` are
    NON-terminal (still resolvable/advanceable by a later durable reconcile, Story
    8.2).
    """
    return status in _TERMINAL_STATUSES


def effective_outcome_status(record: DecisionRecord) -> str:
    """Return the newest-known outcome status for a decision (Story 6.7).

    Layers the durable reconciliation truth over the original co-sign: the
    ``reconciliation_snapshot.outcome.status`` when a reconcile has run (the newest
    broker truth), else the ``cosign_snapshot.outcome.status`` (the state first
    surfaced at co-sign). Returns ``""`` when neither snapshot carries an outcome
    (a not-yet-cosigned record). Read-only — never mutates the immutable snapshots,
    so verbatim replay of ``recommendation_snapshot``/``cosign_snapshot`` stays
    byte-identical; this is a projection for the history surface only.
    """
    reconciliation = record.reconciliation_snapshot or {}
    reconciled_outcome = reconciliation.get("outcome") or {}
    status = reconciled_outcome.get("status")
    if status:
        return status
    cosign = record.cosign_snapshot or {}
    outcome = cosign.get("outcome") or {}
    return outcome.get("status", "")


def _effective_filled_qty(record: DecisionRecord) -> Decimal:
    """The newest-known filled quantity for ``record`` (Story 8.2).

    Mirrors :func:`effective_outcome_status`'s snapshot precedence exactly — the
    reconciliation snapshot when it carries a status, else the cosign snapshot — so
    the qty read is drawn from the SAME outcome the status came from. ``filled_qty``
    is persisted as a fixed-point string; parse it back to ``Decimal`` (never binary
    float). Absent/unknown → ``Decimal("0")``.
    """
    reconciliation = record.reconciliation_snapshot or {}
    reconciled_outcome = reconciliation.get("outcome") or {}
    if reconciled_outcome.get("status"):
        raw = reconciled_outcome.get("filled_qty")
    else:
        cosign = record.cosign_snapshot or {}
        raw = (cosign.get("outcome") or {}).get("filled_qty")
    return Decimal(str(raw)) if raw is not None else Decimal("0")


def record_reconciliation(
    record: DecisionRecord,
    *,
    outcome: OrderOutcome,
    now: datetime.datetime | None = None,
) -> None:
    """Persist a durable reconciliation of a cosigned decision ADDITIVELY (6.7).

    Writes the reconciled :class:`OrderOutcome` into the ADDITIVE
    ``reconciliation_snapshot`` (money as fixed-point strings, the SAME shape as
    ``cosign_snapshot.outcome``) and stamps ``reconciled_at`` (tz-aware UTC now).
    Asserts ``record.status == "cosigned"`` — only a placed decision has an order
    to reconcile. It NEVER mutates ``recommendation_snapshot``, ``cosign_snapshot``,
    ``status``, or ``broker_ref`` (the immutable record of what was decided/first
    surfaced stays byte-identical for verbatim replay). Write-LATEST, not
    write-once: reconciliation may run repeatedly (latest-known-truth) as an order
    progresses ``pending → filled``, so these two fields are overwritten each time.
    The CALLER commits.

    Advance-only toward settlement (Story 8.2): a money truth is never walked
    backward to a less-settled state.

    - If the record's newest-known outcome is ALREADY fully terminal
      (``filled``/``rejected``), this is a no-op — the confirmed money truth stands.
    - If the newest-known outcome is ``partial`` (no longer terminal as of 8.2) and
      the incoming ``outcome.status`` is INDETERMINATE (``pending``/``timeout``),
      this is a no-op — a partial fill must NOT regress to a less-settled
      indeterminate state. A ``partial`` DOES accept an advance to ``filled`` or a
      larger-qty ``partial`` (both write below).
    - Otherwise (``pending``/``timeout`` → anything, ``partial`` → ``filled`` /
      larger ``partial``) the reconciliation snapshot is written.

    The sole caller only reconciles a non-fully-terminal record, so this is
    defense-in-depth at the writer (AD-6) against a stale/racing read regressing a
    confirmed outcome.
    """
    if record.status != "cosigned":
        raise ValueError(
            "Only a cosigned decision record can be reconciled (there is no "
            f"placed order to reconcile otherwise); record status is "
            f"{record.status!r}."
        )
    current = effective_outcome_status(record)
    if current in ("filled", "rejected"):
        # Fully terminal — never overwrite the confirmed money truth.
        return
    if current == "partial":
        # A partial fill advances only: to ``filled`` or an EQUAL/LARGER partial.
        if outcome.status.value in ("pending", "timeout"):
            # Indeterminate re-read (INDETERMINATE = {pending, timeout}) — never
            # regress a partial to a less-settled state.
            return
        if outcome.filled_qty < _effective_filled_qty(record):
            # Fewer filled shares than already confirmed — a stale/racing broker
            # read (e.g. a shrunken ``partial``, or a ``rejected``/``pending`` that
            # dropped the fill count) must NEVER overwrite the confirmed partial and
            # ERASE real filled shares. Share count is monotonic toward settlement.
            return
    record.reconciliation_snapshot = {
        "outcome": {
            "status": outcome.status.value,
            "filled_qty": _money(outcome.filled_qty),
            "avg_price": (
                None if outcome.avg_price is None else _money(outcome.avg_price)
            ),
            "broker_ref": outcome.broker_ref,
            # Shape parity with ``cosign_snapshot.outcome`` (Story 7.5): the
            # reconcile read now resolves the account hash onto ``account_ref``,
            # so persist it here too — the durable audit of which account the
            # reconciled order landed against, not just the cosign snapshot.
            "account_ref": outcome.account_ref,
        },
    }
    record.reconciled_at = now or datetime.datetime.now(datetime.timezone.utc)
