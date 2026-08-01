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
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from brokers.port import OrderOutcome
from coach.execution import mint_idempotency_key
from coach.recommendation import OrderIntent
from coach.validation import BlessedRecommendation
from db.models import DecisionRecord
from db.repository import ScopedRepository
from db.scope import Scope
from money import format_money

#: The snapshot-shape version stamped onto every proposed record (AD-5). Bump
#: this when the persisted snapshot shape changes so replay can adapt.
DECISION_RECORD_SCHEMA_VERSION = 1


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
    """Serialize an :class:`OrderIntent` for a JSON snapshot (money fixed-point)."""
    return {
        "symbol": order_intent.symbol,
        "side": order_intent.side.value,
        "amount": _money(order_intent.amount),
    }


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
    """
    if scope.is_system:
        raise ValueError(
            "claim_for_cosign requires a USER scope; the approve seam is never "
            "system-scoped (fail-closed)."
        )
    result = await session.execute(
        update(DecisionRecord)
        .where(
            DecisionRecord.id == decision_id,
            DecisionRecord.owner_id == scope.user_id,
            DecisionRecord.status == "proposed",
        )
        .values(status="cosigning")
    )
    await session.commit()
    return result.rowcount == 1


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
    """
    if scope.is_system:
        raise ValueError(
            "release_claim requires a USER scope; the approve seam is never "
            "system-scoped (fail-closed)."
        )
    await session.execute(
        update(DecisionRecord)
        .where(
            DecisionRecord.id == decision_id,
            DecisionRecord.owner_id == scope.user_id,
            DecisionRecord.status == "cosigning",
        )
        .values(status="proposed")
    )
    await session.commit()


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
    # none (e.g. a no-order_id timeout surfaced as pending).
    record.broker_ref = outcome.broker_ref
    record.cosign_snapshot = {
        "order_intent": _order_intent_json(order_intent),
        "outcome": {
            "status": outcome.status.value,
            "filled_qty": _money(outcome.filled_qty),
            "avg_price": None if outcome.avg_price is None else _money(outcome.avg_price),
            "broker_ref": outcome.broker_ref,
        },
    }


#: The terminal (settled) outcome statuses — a reconcile short-circuits on these
#: (nothing more to read; the broker's answer is final). ``timeout``/``pending``
#: are NON-terminal (indeterminate): the true state is not yet known, so a durable
#: reconcile may still resolve them (mirrors ``coach.execution.INDETERMINATE``).
_TERMINAL_STATUSES: frozenset[str] = frozenset({"filled", "partial", "rejected"})


def _is_terminal(status: str) -> bool:
    """True iff ``status`` is a settled/terminal outcome (filled/partial/rejected).

    A terminal outcome is the broker's final answer — a reconcile returns it
    unchanged and never re-reads the broker. ``timeout``/``pending`` are NON-terminal
    (still resolvable by a later durable reconcile).
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

    Monotonic toward settlement: if the record's newest-known outcome is ALREADY
    terminal (``filled``/``rejected``), this is a no-op — a settled money truth is
    never walked backward to a less-settled state. The sole caller only reconciles
    a non-terminal record, so this is defense-in-depth at the writer (AD-6) against
    a stale/racing read regressing a confirmed outcome.
    """
    if record.status != "cosigned":
        raise ValueError(
            "Only a cosigned decision record can be reconciled (there is no "
            f"placed order to reconcile otherwise); record status is "
            f"{record.status!r}."
        )
    if _is_terminal(effective_outcome_status(record)):
        # Already settled — never regress a terminal outcome (monotonic toward
        # settlement); the confirmed money truth stands.
        return
    record.reconciliation_snapshot = {
        "outcome": {
            "status": outcome.status.value,
            "filled_qty": _money(outcome.filled_qty),
            "avg_price": (
                None if outcome.avg_price is None else _money(outcome.avg_price)
            ),
            "broker_ref": outcome.broker_ref,
        },
    }
    record.reconciled_at = now or datetime.datetime.now(datetime.timezone.utc)
