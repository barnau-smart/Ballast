"""Story 7.2 tests — recoverable placement, reconcile/balance concurrency, and
bounded reclamation of orphaned ``cosigning`` rows.

Covers the I/O & Edge-Case Matrix (a–g), OFFLINE against a real Postgres
(``docker compose up -d db``), zero network / zero credentials (fake adapters):

  (a) A cosign/commit failure AFTER a successful placement leaves the row
      ``status='cosigning'`` WITH the durably-persisted ``broker_ref`` (recoverable)
      and no double-place — because ``broker_ref`` is persisted in its OWN commit
      BEFORE the cosign, and the claim is NEVER released post-placement.
  (b) Two concurrent same-decision reconciles never regress a terminal outcome
      (a ``filled`` is not overwritten by a ``timeout``) — the lock + monotonic
      guard hold across sessions.
  (c) Two concurrent balance reconciles → the newest ``as_of`` wins, holdings +
      balance land as ONE atomic unit (no interleave), a stale snapshot writes
      nothing.
  (d) The reclaimer forward-recovers a ``broker_ref``-present orphan to a
      re-reconcilable cosigned record (indeterminate outcome carrying the ref).
  (e) The reclaimer surfaces a ``broker_ref``-NULL orphan as cosigned +
      needs-reconfirmation, NEVER re-placing and NEVER releasing to ``proposed``.
  (f) A within-window ``cosigning`` row (a legitimately in-flight approve) is
      untouched.
  (g) A reclaim re-run is a full no-op (idempotent).

Each test uses a fresh user and cleans up its own rows.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text

from brokers.fake_adapter import FakeBrokerAdapter
from brokers.port import Holding, OrderOutcome, OrderStatus, PortfolioSnapshot
from brokers.portfolio import get_portfolio, reconcile_portfolio
from coach.decision_record import (
    claim_for_cosign,
    cosign,
    persist_broker_ref,
    reclaim_orphaned_cosigning,
    record_reconciliation,
)
from coach.execution import execute_approved_order
from coach.recommendation import OrderIntent, OrderSide
from db.connection import get_connection
from db.models import (
    BrokerageToken,
    DecisionRecord,
    PortfolioBalance,
    PortfolioCache,
)
from db.scope import Scope
from db.session import async_session_maker, engine
from brokers.session import BrokerageSession


@pytest_asyncio.fixture(autouse=True)
async def ensure_tables():
    async with engine.begin() as conn:
        await conn.run_sync(BrokerageToken.__table__.create, checkfirst=True)
        await conn.run_sync(PortfolioCache.__table__.create, checkfirst=True)
        await conn.run_sync(PortfolioBalance.__table__.create, checkfirst=True)
        await conn.run_sync(DecisionRecord.__table__.create, checkfirst=True)
        await conn.execute(
            text(
                "ALTER TABLE decision_record "
                "ADD COLUMN IF NOT EXISTS broker_ref VARCHAR(64)"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE decision_record "
                "ADD COLUMN IF NOT EXISTS reconciliation_snapshot JSON"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE decision_record "
                "ADD COLUMN IF NOT EXISTS reconciled_at TIMESTAMPTZ"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE decision_record "
                "ADD COLUMN IF NOT EXISTS cosigning_at TIMESTAMPTZ"
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_portfolio_balance_owner "
                "ON portfolio_balance (owner_id)"
            )
        )
    yield


# --- helpers -----------------------------------------------------------------


def _make_user() -> uuid.UUID:
    user_id = uuid.uuid4()
    email = f"recover-test-{user_id.hex}@example.com"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO "user" '
                "(id, email, hashed_password, is_active, is_superuser, is_verified) "
                "VALUES (%s, %s, %s, TRUE, FALSE, FALSE)",
                (str(user_id), email, "x" * 60),
            )
        conn.commit()
    return user_id


def _delete_user(user_id: uuid.UUID) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "user" WHERE id = %s', (str(user_id),))
        conn.commit()


def _live_session(provider: str = "fake") -> BrokerageSession:
    return BrokerageSession(
        state="live",
        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
        provider=provider,
    )


_ORDER_INTENT_JSON = {"symbol": "VTI", "side": "buy", "amount": "500"}


def _insert_proposed(owner: uuid.UUID, *, idempotency_key: str) -> uuid.UUID:
    """Insert a real ``proposed`` decision_record row (raw SQL) with an order_intent."""
    decision_id = uuid.uuid4()
    snapshot = {
        "action_label": "Buy VTI",
        "reasoning": "x",
        "order_intent": _ORDER_INTENT_JSON,
        "evidence": [],
        "uncertainties": [],
    }
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO decision_record "
                "(id, owner_id, schema_version, recommendation_snapshot, status, "
                " created_at, idempotency_key) "
                "VALUES (%s, %s, 1, %s, 'proposed', %s, %s)",
                (
                    str(decision_id),
                    str(owner),
                    json.dumps(snapshot),
                    datetime.now(timezone.utc),
                    idempotency_key,
                ),
            )
        conn.commit()
    return decision_id


def _insert_cosigning(
    owner: uuid.UUID,
    *,
    cosigning_at: datetime | None,
    broker_ref: str | None,
) -> uuid.UUID:
    """Insert a ``cosigning`` orphan row (raw SQL) modelling a crash mid-claim."""
    decision_id = uuid.uuid4()
    snapshot = {
        "action_label": "Buy VTI",
        "reasoning": "x",
        "order_intent": _ORDER_INTENT_JSON,
        "evidence": [],
        "uncertainties": [],
    }
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO decision_record "
                "(id, owner_id, schema_version, recommendation_snapshot, status, "
                " created_at, idempotency_key, cosigning_at, broker_ref) "
                "VALUES (%s, %s, 1, %s, 'cosigning', %s, %s, %s, %s)",
                (
                    str(decision_id),
                    str(owner),
                    json.dumps(snapshot),
                    datetime.now(timezone.utc),
                    f"key-{decision_id}",
                    cosigning_at,
                    broker_ref,
                ),
            )
        conn.commit()
    return decision_id


def _row(decision_id: uuid.UUID) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, broker_ref, co_signed_at, cosign_snapshot, "
                "cosigning_at FROM decision_record WHERE id = %s",
                (str(decision_id),),
            )
            cols = [c.name for c in cur.description]
            (row,) = cur.fetchall()
    return dict(zip(cols, row))


def _snapshot(
    *, as_of: datetime, cash: Decimal, symbols: list[str]
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        as_of=as_of,
        cash=cash,
        holdings=[
            Holding(
                symbol=s,
                quantity=Decimal("1"),
                market_value=Decimal("100.00"),
                cost_basis=Decimal("90.00"),
            )
            for s in symbols
        ],
    )


# =============================================================================
# (a) Placement recoverability — cosign/commit fails after placement
# =============================================================================


@pytest.mark.asyncio
async def test_placement_then_cosign_failure_leaves_cosigning_with_broker_ref():
    """A cosign/commit failure after placement leaves ``cosigning`` + ``broker_ref``.

    Reproduces the ``/approve`` ordering (claim → place → persist_broker_ref(own
    commit) → cosign+commit) but INTERRUPTS before the final cosign/commit,
    modelling a cosign/commit failure. Asserts the durable ``broker_ref`` is
    already persisted, the row stays ``cosigning`` (NOT released to ``proposed``),
    and ``place_order`` ran exactly once — so the order is recoverable and no
    double-place is possible.
    """
    owner = _make_user()
    try:
        decision_id = _insert_proposed(owner, idempotency_key="k-a")
        scope = Scope.for_user(owner)
        broker = FakeBrokerAdapter()
        intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))

        async with async_session_maker() as session:
            won = await claim_for_cosign(decision_id, scope=scope, session=session)
            assert won is True
            outcome = await execute_approved_order(
                intent,
                broker=broker,
                broker_session=_live_session(),
                idempotency_key="k-a",
            )
            assert outcome.broker_ref is not None
            # Durable ref persisted in ITS OWN commit, BEFORE cosign.
            persisted = await persist_broker_ref(
                decision_id, outcome.broker_ref, scope=scope, session=session
            )
            assert persisted is True
            # ...now the cosign/commit "fails" — we simply never reach it. No
            # release is issued (never-release-post-placement).

        # The order was placed exactly once.
        assert len(broker._orders_by_ref) == 1

        # The record is recoverable: still cosigning, WITH the broker_ref.
        row = _row(decision_id)
        assert row["status"] == "cosigning"
        assert row["broker_ref"] == outcome.broker_ref
        assert row["co_signed_at"] is None
    finally:
        _delete_user(owner)


# =============================================================================
# (b) Two concurrent same-decision reconciles never regress a terminal outcome
# =============================================================================


@pytest.mark.asyncio
async def test_concurrent_reconcile_never_regresses_terminal_outcome():
    """A committed ``filled`` is never regressed to ``timeout`` by a racing persist.

    Cosigns a record with an indeterminate (pending) outcome, then serially applies
    two reconciles UNDER THE LOCK (the endpoint's pattern): first a terminal
    ``filled`` (persisted), then a ``timeout`` on a freshly-locked instance. The
    monotonic terminal guard — made effective across sessions by the row lock —
    no-ops the second, so the persisted outcome stays ``filled``.
    """
    from coach.decision_record import lock_decision, effective_outcome_status

    owner = _make_user()
    try:
        decision_id = _insert_proposed(owner, idempotency_key="k-b")
        scope = Scope.for_user(owner)
        intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))

        # Claim + cosign with an indeterminate (pending) outcome carrying a ref.
        async with async_session_maker() as session:
            await claim_for_cosign(decision_id, scope=scope, session=session)
            record = await lock_decision(
                decision_id, scope=scope, session=session
            )
            cosign(
                record,
                order_intent=intent,
                outcome=OrderOutcome(
                    status=OrderStatus.PENDING,
                    filled_qty=Decimal("0"),
                    avg_price=None,
                    broker_ref="ref-b",
                ),
                idempotency_key="k-b",
            )
            await session.commit()

        filled = OrderOutcome(
            status=OrderStatus.FILLED,
            filled_qty=Decimal("1"),
            avg_price=Decimal("500.00"),
            broker_ref="ref-b",
        )
        timeout = OrderOutcome(
            status=OrderStatus.TIMEOUT,
            filled_qty=Decimal("0"),
            avg_price=None,
            broker_ref="ref-b",
        )

        # Reconcile A persists terminal filled (locked).
        async with async_session_maker() as session:
            locked = await lock_decision(decision_id, scope=scope, session=session)
            record_reconciliation(locked, outcome=filled)
            await session.commit()

        # Reconcile B (serialized second, freshly locked) reads the committed
        # terminal state → monotonic guard no-ops; filled is NOT regressed.
        async with async_session_maker() as session:
            locked = await lock_decision(decision_id, scope=scope, session=session)
            record_reconciliation(locked, outcome=timeout)
            await session.commit()

        async with async_session_maker() as session:
            locked = await lock_decision(decision_id, scope=scope, session=session)
            assert effective_outcome_status(locked) == "filled"
            await session.rollback()
    finally:
        _delete_user(owner)


@pytest.mark.asyncio
async def test_partial_advances_to_filled_but_never_regresses():
    """Story 8.2: an effective ``partial`` advances to ``filled`` but never regresses.

    Closes the Epic 6 partial-fill action item: ``partial`` is no longer terminal,
    so a re-reconcile can advance it to ``filled`` (or a larger partial), but the
    advance-only guard in ``record_reconciliation`` must IGNORE an incoming
    ``pending``/``timeout`` re-read so a partial can never regress.
    """
    from coach.decision_record import lock_decision, effective_outcome_status

    owner = _make_user()
    try:
        decision_id = _insert_proposed(owner, idempotency_key="k-p")
        scope = Scope.for_user(owner)
        intent = OrderIntent(symbol="VTI", side=OrderSide.BUY, amount=Decimal("500"))

        # Cosign with an indeterminate (pending) outcome carrying a ref.
        async with async_session_maker() as session:
            await claim_for_cosign(decision_id, scope=scope, session=session)
            record = await lock_decision(
                decision_id, scope=scope, session=session
            )
            cosign(
                record,
                order_intent=intent,
                outcome=OrderOutcome(
                    status=OrderStatus.PENDING,
                    filled_qty=Decimal("0"),
                    avg_price=None,
                    broker_ref="ref-p",
                ),
                idempotency_key="k-p",
            )
            await session.commit()

        partial = OrderOutcome(
            status=OrderStatus.PARTIAL,
            filled_qty=Decimal("1"),
            avg_price=Decimal("500.00"),
            broker_ref="ref-p",
        )
        pending_reread = OrderOutcome(
            status=OrderStatus.PENDING,
            filled_qty=Decimal("0"),
            avg_price=None,
            broker_ref="ref-p",
        )
        timeout_reread = OrderOutcome(
            status=OrderStatus.TIMEOUT,
            filled_qty=Decimal("0"),
            avg_price=None,
            broker_ref="ref-p",
        )
        filled = OrderOutcome(
            status=OrderStatus.FILLED,
            filled_qty=Decimal("2"),
            avg_price=Decimal("500.00"),
            broker_ref="ref-p",
        )

        # 1) pending → partial (written).
        async with async_session_maker() as session:
            locked = await lock_decision(decision_id, scope=scope, session=session)
            record_reconciliation(locked, outcome=partial)
            await session.commit()
        async with async_session_maker() as session:
            locked = await lock_decision(decision_id, scope=scope, session=session)
            assert effective_outcome_status(locked) == "partial"
            await session.rollback()

        smaller_partial_reread = OrderOutcome(
            status=OrderStatus.PARTIAL,
            filled_qty=Decimal("0"),  # fewer filled shares than the confirmed 1
            avg_price=Decimal("500.00"),
            broker_ref="ref-p",
        )

        # 2) partial → pending re-read: IGNORED (no regression).
        async with async_session_maker() as session:
            locked = await lock_decision(decision_id, scope=scope, session=session)
            record_reconciliation(locked, outcome=pending_reread)
            await session.commit()
        # 3) partial → timeout re-read: IGNORED (no regression).
        async with async_session_maker() as session:
            locked = await lock_decision(decision_id, scope=scope, session=session)
            record_reconciliation(locked, outcome=timeout_reread)
            await session.commit()
        # 3b) partial → SMALLER partial re-read: IGNORED — a stale/racing read
        # reporting fewer filled shares must never overwrite the confirmed partial
        # and erase real filled shares (share count is monotonic toward settlement).
        async with async_session_maker() as session:
            locked = await lock_decision(decision_id, scope=scope, session=session)
            record_reconciliation(locked, outcome=smaller_partial_reread)
            await session.commit()
        async with async_session_maker() as session:
            locked = await lock_decision(decision_id, scope=scope, session=session)
            assert effective_outcome_status(locked) == "partial"  # still partial
            snap = locked.reconciliation_snapshot["outcome"]
            assert snap["filled_qty"] == "1"  # the confirmed fill is preserved
            await session.rollback()

        # 4) partial → filled: ADVANCES (written).
        async with async_session_maker() as session:
            locked = await lock_decision(decision_id, scope=scope, session=session)
            record_reconciliation(locked, outcome=filled)
            await session.commit()
        async with async_session_maker() as session:
            locked = await lock_decision(decision_id, scope=scope, session=session)
            assert effective_outcome_status(locked) == "filled"
            snap = locked.reconciliation_snapshot["outcome"]
            assert snap["filled_qty"] == "2"
            await session.rollback()
    finally:
        _delete_user(owner)


# =============================================================================
# (c) Two concurrent balance reconciles — newest wins, atomic, stale writes none
# =============================================================================


@pytest.mark.asyncio
async def test_concurrent_balance_reconcile_newest_wins_atomic():
    """Newest ``as_of`` wins; stale writes nothing; the two-table unit is atomic.

    First establishes a T1 balance + holdings. Then applies a T2 (newer) reconcile
    and a T1-equal (stale) reconcile. The newer T2 wins (balance advances, holdings
    replaced together); the stale one is a no-op (rowcount 0 on the ``as_of <
    :incoming`` conditional claim) leaving both tables at T2.
    """
    owner = _make_user()
    try:
        scope = Scope.for_user(owner)
        t1 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        t2 = t1 + timedelta(hours=1)

        # T1: initial reconcile (first-ever insert path).
        async with async_session_maker() as session:
            await reconcile_portfolio(
                scope,
                session,
                FakeBrokerAdapter(),
                snapshot=_snapshot(as_of=t1, cash=Decimal("100.00"), symbols=["VTI"]),
            )

        # T2 (newer) reconcile → wins: balance advances, holdings become BND.
        async with async_session_maker() as session:
            view = await reconcile_portfolio(
                scope,
                session,
                FakeBrokerAdapter(),
                snapshot=_snapshot(as_of=t2, cash=Decimal("250.00"), symbols=["BND"]),
            )
            assert view.cash == Decimal("250.00")
            assert view.as_of == t2
            assert {h.symbol for h in view.holdings} == {"BND"}

        # A stale (T1-equal) reconcile → loses: nothing changes, still T2/BND.
        async with async_session_maker() as session:
            view = await reconcile_portfolio(
                scope,
                session,
                FakeBrokerAdapter(),
                snapshot=_snapshot(as_of=t1, cash=Decimal("999.00"), symbols=["SPY"]),
            )
            assert view.cash == Decimal("250.00")  # stale did NOT clobber newer
            assert view.as_of == t2
            assert {h.symbol for h in view.holdings} == {"BND"}

        # Atomicity: exactly ONE balance row, holdings match the winner (BND only).
        async with async_session_maker() as session:
            final = await get_portfolio(scope, session)
            assert final.cash == Decimal("250.00")
            assert {h.symbol for h in final.holdings} == {"BND"}
    finally:
        _delete_user(owner)


@pytest.mark.asyncio
async def test_concurrent_balance_reconcile_no_orphaned_holdings():
    """TRULY concurrent reconciles must not orphan a racing winner's holdings.

    Regression for the pre-claim-read interleave (Story 7.2 review): establish a T1
    balance, then run a T2 and a T3 (both strictly newer) reconcile CONCURRENTLY via
    ``asyncio.gather``. Because the winning balance ``UPDATE`` holds the
    ``portfolio_balance`` row lock until commit and the holdings are read AFTER the
    claim, the two-table replace can never interleave: the newest snapshot (T3) is
    the final committed state, with EXACTLY its holdings — never a stale winner's
    rows left orphaned alongside. (Under the buggy pre-claim read the loser deleted a
    stale snapshot and both symbol sets survived → duplicated/orphaned holdings.)
    """
    owner = _make_user()
    try:
        scope = Scope.for_user(owner)
        t1 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        t2 = t1 + timedelta(hours=1)
        t3 = t1 + timedelta(hours=2)

        # T1: establish the initial balance + holdings.
        async with async_session_maker() as session:
            await reconcile_portfolio(
                scope,
                session,
                FakeBrokerAdapter(),
                snapshot=_snapshot(as_of=t1, cash=Decimal("100.00"), symbols=["VTI"]),
            )

        async def _reconcile(as_of, cash, symbol):
            async with async_session_maker() as session:
                return await reconcile_portfolio(
                    scope,
                    session,
                    FakeBrokerAdapter(),
                    snapshot=_snapshot(
                        as_of=as_of, cash=Decimal(cash), symbols=[symbol]
                    ),
                )

        # T2 and T3 race; both are newer than T1 so the row lock serializes them.
        results = await asyncio.gather(
            _reconcile(t2, "250.00", "BND"),
            _reconcile(t3, "375.00", "SPY"),
            return_exceptions=True,
        )
        for r in results:
            assert not isinstance(r, Exception), r

        # Final state is the NEWEST snapshot (T3) with EXACTLY its holdings — no
        # orphaned BND row from the racing T2 winner.
        async with async_session_maker() as session:
            final = await get_portfolio(scope, session)
            assert final.cash == Decimal("375.00")
            assert final.as_of == t3
            assert {h.symbol for h in final.holdings} == {"SPY"}

        # Exactly ONE balance row and one holdings row (no interleave duplicates).
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM portfolio_balance WHERE owner_id = %s",
                    (str(owner),),
                )
                (bal_count,) = cur.fetchone()
                cur.execute(
                    "SELECT count(*) FROM portfolio_cache WHERE owner_id = %s",
                    (str(owner),),
                )
                (holdings_count,) = cur.fetchone()
        assert bal_count == 1
        assert holdings_count == 1
    finally:
        _delete_user(owner)


@pytest.mark.asyncio
async def test_concurrent_first_balance_insert_race_is_lost_race_not_crash():
    """Two concurrent first-ever reconciles → one inserts, the other loses cleanly.

    Runs two ``reconcile_portfolio`` first-inserts CONCURRENTLY on the same fresh
    user (no balance row yet). ``uq_portfolio_balance_owner`` lets exactly one
    insert win; the other's ``IntegrityError`` is caught as a lost race (rollback →
    re-read → current view), never a crash or a duplicate row.
    """
    owner = _make_user()
    try:
        scope = Scope.for_user(owner)
        t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

        async def _reconcile(cash: str, symbol: str):
            async with async_session_maker() as session:
                return await reconcile_portfolio(
                    scope,
                    session,
                    FakeBrokerAdapter(),
                    snapshot=_snapshot(
                        as_of=t0, cash=Decimal(cash), symbols=[symbol]
                    ),
                )

        # Both race the first insert; gather must not raise (no unhandled
        # IntegrityError leaks past the lost-race handling).
        results = await asyncio.gather(
            _reconcile("100.00", "VTI"),
            _reconcile("200.00", "BND"),
            return_exceptions=True,
        )
        for r in results:
            assert not isinstance(r, Exception), r

        # Exactly ONE balance row exists (no duplicate).
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM portfolio_balance WHERE owner_id = %s",
                    (str(owner),),
                )
                (count,) = cur.fetchone()
        assert count == 1
    finally:
        _delete_user(owner)


# =============================================================================
# (d) Reclaimer forward-recovers a broker_ref-present orphan
# =============================================================================


@pytest.mark.asyncio
async def test_reclaim_forward_recovers_broker_ref_present_orphan():
    """A ``broker_ref``-present orphan older than the window → re-reconcilable cosigned.

    The reclaimer forward-recovers it to ``cosigned`` with an INDETERMINATE
    (pending) outcome carrying the persisted ``broker_ref`` — so the 6.7 reconcile
    can resolve it. It NEVER re-places and NEVER releases to ``proposed``.
    """
    owner = _make_user()
    try:
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        decision_id = _insert_cosigning(
            owner, cosigning_at=old, broker_ref="fake-order-xyz"
        )

        async with async_session_maker() as session:
            reclaimed = await reclaim_orphaned_cosigning(
                session=session, older_than=timedelta(hours=1)
            )
        assert reclaimed == 1

        row = _row(decision_id)
        assert row["status"] == "cosigned"
        assert row["co_signed_at"] is not None
        assert row["broker_ref"] == "fake-order-xyz"  # preserved
        snapshot = row["cosign_snapshot"]
        # Indeterminate (non-terminal) outcome carrying the ref → re-reconcilable.
        assert snapshot["outcome"]["status"] in {"pending", "timeout"}
        assert snapshot["outcome"]["broker_ref"] == "fake-order-xyz"
        assert snapshot["outcome"]["filled_qty"] == "0"
        assert snapshot["outcome"]["avg_price"] is None
        # Executed intent taken verbatim from the proposed snapshot.
        assert snapshot["order_intent"] == _ORDER_INTENT_JSON
    finally:
        _delete_user(owner)


# =============================================================================
# (e) Reclaimer surfaces a broker_ref-NULL orphan as needs-reconfirmation
# =============================================================================


@pytest.mark.asyncio
async def test_reclaim_surfaces_broker_ref_null_orphan_needs_reconfirmation():
    """A ``broker_ref``-NULL orphan → cosigned + indeterminate; NEVER re-placed/released.

    An ambiguous placement (no confirmed order id) is completed to ``cosigned``
    with an indeterminate outcome and a NULL ``broker_ref`` — surfaced for explicit
    human re-confirmation. It is NEVER re-placed and NEVER released to ``proposed``.
    """
    owner = _make_user()
    try:
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        decision_id = _insert_cosigning(owner, cosigning_at=old, broker_ref=None)

        async with async_session_maker() as session:
            reclaimed = await reclaim_orphaned_cosigning(
                session=session, older_than=timedelta(hours=1)
            )
        assert reclaimed == 1

        row = _row(decision_id)
        assert row["status"] == "cosigned"  # NOT released to proposed
        assert row["broker_ref"] is None
        snapshot = row["cosign_snapshot"]
        assert snapshot["outcome"]["status"] in {"pending", "timeout"}  # indeterminate
        assert snapshot["outcome"]["broker_ref"] is None
    finally:
        _delete_user(owner)


# =============================================================================
# (f) Within-window cosigning row is untouched
# =============================================================================


@pytest.mark.asyncio
async def test_reclaim_leaves_within_window_row_untouched():
    """A ``cosigning`` row within the window (in-flight approve) is NEVER reclaimed."""
    owner = _make_user()
    try:
        recent = datetime.now(timezone.utc) - timedelta(seconds=5)
        decision_id = _insert_cosigning(
            owner, cosigning_at=recent, broker_ref="fake-order-live"
        )

        async with async_session_maker() as session:
            reclaimed = await reclaim_orphaned_cosigning(
                session=session, older_than=timedelta(hours=1)
            )
        assert reclaimed == 0

        row = _row(decision_id)
        assert row["status"] == "cosigning"  # untouched
        assert row["co_signed_at"] is None
        assert row["cosign_snapshot"] is None
    finally:
        _delete_user(owner)


@pytest.mark.asyncio
async def test_reclaim_leaves_null_cosigning_at_untouched():
    """A ``cosigning`` row with NULL ``cosigning_at`` (unknown claim time) is never
    reclaimed — the reclaimer treats an unknown age conservatively."""
    owner = _make_user()
    try:
        decision_id = _insert_cosigning(
            owner, cosigning_at=None, broker_ref="fake-order-null-at"
        )
        async with async_session_maker() as session:
            reclaimed = await reclaim_orphaned_cosigning(
                session=session, older_than=timedelta(hours=1)
            )
        assert reclaimed == 0
        assert _row(decision_id)["status"] == "cosigning"
    finally:
        _delete_user(owner)


# =============================================================================
# (g) Reclaim re-run is a no-op
# =============================================================================


@pytest.mark.asyncio
async def test_reclaim_rerun_is_noop():
    """After a reclaim moves rows to ``cosigned``, a re-run matches nothing → 0."""
    owner = _make_user()
    try:
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        _insert_cosigning(owner, cosigning_at=old, broker_ref="fake-order-rerun")

        async with async_session_maker() as session:
            first = await reclaim_orphaned_cosigning(
                session=session, older_than=timedelta(hours=1)
            )
        assert first == 1

        async with async_session_maker() as session:
            second = await reclaim_orphaned_cosigning(
                session=session, older_than=timedelta(hours=1)
            )
        assert second == 0  # idempotent no-op
    finally:
        _delete_user(owner)


@pytest.mark.asyncio
async def test_reclaim_rejects_negative_window():
    """A negative ``older_than`` is refused (would reclaim recent in-flight rows)."""
    async with async_session_maker() as session:
        with pytest.raises(ValueError):
            await reclaim_orphaned_cosigning(
                session=session, older_than=timedelta(seconds=-1)
            )


@pytest.mark.asyncio
async def test_reclaim_rowcount_gated_racing_cosign_wins():
    """A racing live cosign (cosigning→cosigned) makes the reclaimer a no-op.

    Models the reclaimer losing the race: the row is moved to ``cosigned`` by a
    legitimate cosign between the reclaimer's candidate SELECT and its gated
    UPDATE. The rowcount-gated ``WHERE status='cosigning'`` then matches nothing,
    so the reclaimer no-ops (returns 0) and never double-completes.
    """
    owner = _make_user()
    try:
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        decision_id = _insert_cosigning(
            owner, cosigning_at=old, broker_ref="fake-order-race"
        )
        # Simulate the racing live cosign committing first.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE decision_record SET status='cosigned', "
                    "co_signed_at=%s WHERE id=%s",
                    (datetime.now(timezone.utc), str(decision_id)),
                )
            conn.commit()

        async with async_session_maker() as session:
            reclaimed = await reclaim_orphaned_cosigning(
                session=session, older_than=timedelta(hours=1)
            )
        # No cosigning row matched at UPDATE time → no-op.
        assert reclaimed == 0
    finally:
        _delete_user(owner)
