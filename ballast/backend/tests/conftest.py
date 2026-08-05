"""Shared, test-only pytest fixtures (Story 8.5 — test-suite performance).

This module makes the backend integration suite fast WITHOUT changing any
product behaviour. It does three things, all strictly test-scoped:

1. **Test-only fast password hasher.** Production auth stays pwdlib/Argon2
   (``api/users.py`` is untouched). Here — and ONLY here, from ``tests/`` — we
   patch ``api.users.UserManager.__init__`` so that, when no ``password_helper``
   is supplied (which is exactly how production constructs it), the manager gets
   a cheap-but-consistent SHA-256 helper instead of Argon2. Argon2 costs ~22 ms
   per hash/verify; every one of the ~100 register+login round-trips in
   ``test_coach_api.py`` (and ~32 other test files) paid that toll twice. The
   fast helper hashes AND verifies correctly, so register/login still work end
   to end — it is simply not slow and not memory-hard. It is NEVER reachable
   from a production code path: production never imports ``tests`` and the patch
   is installed by an autouse fixture that only runs under pytest.

2. **Shared app + TestClient fixture (function-scoped).** ``create_app()``
   registers every router and runs the lifespan (``create_db_and_tables`` +
   startup migrations). The ``client`` fixture below provides it via conftest so
   ``test_coach_api.py`` no longer defines its own. It is FUNCTION-scoped (pytest's
   default), NOT session-scoped.

   Why not session-scoped? asyncpg connections are bound to the event loop that
   created them, and ``db.session.engine`` is built with ``NullPool`` (see
   ``db/session.py``) so it caches no connections across calls. A session-scoped
   async fixture opens its connection on whatever loop is live at first use, but
   pytest-asyncio gives each test function its OWN event loop; a later test that
   touches the module-global engine then awaits a result future on a loop other
   than the one that created the connection, so the await never resolves — a HANG
   (postgres sits ``idle in transaction`` on a ``SELECT`` at ~0% CPU). The first
   8.5 pass made ``client`` + ``ensure_tables`` ``scope="session"``; independent
   runs of ``tests/test_coach_api.py`` never completed. Function scope keeps every
   engine interaction on the test's own loop, so ``NullPool`` opens and closes the
   connection within that single loop and nothing crosses loops. The dominant,
   safe speedup is the fast hasher (#1), which is unaffected by scope and is the
   whole win.

3. **Function-scoped ``ensure_tables``.** The schema-reconciliation DDL (every
   ``CREATE/ALTER/INDEX ... IF NOT EXISTS`` statement is preserved verbatim from
   ``test_coach_api.py``) runs before each test. It is idempotent and cheap
   (``IF NOT EXISTS`` / ``checkfirst``), so per-test execution is correct and not
   a meaningful cost. It is function-scoped for the same cross-event-loop reason
   as ``client`` above — a session-scoped async fixture reusing the shared engine
   stalls across per-function event loops.

Per-test isolation is otherwise unchanged: tests still use unique-email users
(``_unique_email``) and clean up their own rows against the real docker Postgres.
"""

from __future__ import annotations

import hashlib

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from api import users as _users_module
from api.app import create_app
from db.session import engine

# --- 1. test-only fast password hasher --------------------------------------


class _FastTestHasher:
    """A cheap, deterministic pwdlib hasher used ONLY in the test suite.

    Not memory-hard, not salted, NOT suitable for production — that's the point:
    it makes register/login effectively free while still hashing+verifying
    consistently so the auth flow behaves identically. Production keeps Argon2.
    """

    _PREFIX = "fasttest$"

    @classmethod
    def identify(cls, hash: str) -> bool:
        return hash.startswith(cls._PREFIX)

    def hash(self, password, *, salt=None) -> str:  # noqa: A002 - pwdlib API
        if isinstance(password, bytes):
            password = password.decode()
        return self._PREFIX + hashlib.sha256(password.encode()).hexdigest()

    def verify(self, password, hash) -> bool:  # noqa: A002 - pwdlib API
        if isinstance(password, bytes):
            password = password.decode()
        return hash == self._PREFIX + hashlib.sha256(password.encode()).hexdigest()

    def check_needs_rehash(self, hash) -> bool:  # noqa: A002 - pwdlib API
        return False


def _fast_password_helper():
    from fastapi_users.password import PasswordHelper

    # Single-hasher tuple on purpose: every test registers AND logs in the same
    # unique-email user within one test under one hasher, so a hash is only ever
    # verified by the hasher that produced it. INVARIANT for future tests: do not
    # split a user's register (one hasher) and login (the other) across the
    # ``real_hasher`` boundary, and do not reuse a fixed email across markers —
    # ``PasswordHash`` here only ``identify``s the ``fasttest$`` prefix, so
    # verifying a leftover Argon2 row under the fast hasher would raise
    # ``UnknownHashError`` (a confusing 500) instead of a clean auth-fail. Unique
    # uuid4 emails (``_unique_email``) keep that from happening today.
    return PasswordHelper(PasswordHash((_FastTestHasher(),)))


@pytest.fixture(autouse=True)
def _fast_password_hasher(request):
    """Patch ``UserManager.__init__`` to inject the fast helper in tests only.

    Production constructs ``UserManager(user_db)`` with no ``password_helper``;
    ``get_user_manager`` builds a fresh ``UserManager`` per request, so patching
    the ``None`` default flips every test app (every ``create_app()`` in every
    test file) onto the cheap hasher without any production edit and without
    per-app dependency overrides. It is applied around each test (cheap) and
    removed afterwards.

    Opt-out: a test/module marked ``@pytest.mark.real_hasher`` keeps production
    Argon2. Used by ``test_register.py``, which deliberately asserts the persisted
    password is a valid pwdlib/Argon2 hash (a real security property) — the fast
    hasher would defeat that assertion, so those tests must run the real thing.
    """
    if request.node.get_closest_marker("real_hasher") is not None:
        yield
        return

    original_init = _users_module.UserManager.__init__

    def _patched_init(self, user_db, password_helper=None):
        if password_helper is None:
            password_helper = _fast_password_helper()
        original_init(self, user_db, password_helper=password_helper)

    _users_module.UserManager.__init__ = _patched_init
    try:
        yield
    finally:
        _users_module.UserManager.__init__ = original_init


# --- 3. one-time schema reconciliation (moved from test_coach_api.py) --------


@pytest_asyncio.fixture(autouse=True)
async def ensure_tables():
    """Ensure owned tables + carried-over schema reconciliation exist.

    Every DDL statement is preserved verbatim from the original per-test
    ``ensure_tables`` in ``test_coach_api.py``; it is all ``IF NOT EXISTS`` /
    ``checkfirst`` and therefore idempotent. FUNCTION-scoped (the default): a
    session-scoped async fixture reusing the module-global ``engine`` across
    pytest-asyncio's per-function event loops stalls (asyncpg connections are
    loop-bound and the engine uses ``NullPool``, so a connection created on one
    test's loop is awaited on another's and never resolves), which hung the suite.
    Function scope keeps each engine interaction on the test's own event loop; the
    DDL is idempotent and cheap so running it per test is correct and negligible.
    """
    from sqlalchemy import text

    from db.models import BrokerageToken, DecisionRecord, MarketDaily, PortfolioCache

    async with engine.begin() as conn:
        await conn.run_sync(BrokerageToken.__table__.create, checkfirst=True)
        await conn.run_sync(PortfolioCache.__table__.create, checkfirst=True)
        await conn.run_sync(MarketDaily.__table__.create, checkfirst=True)
        await conn.run_sync(DecisionRecord.__table__.create, checkfirst=True)
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_decision_record_idempotency_key "
                "ON decision_record (idempotency_key)"
            )
        )
        # Story 6.3 hoists broker_ref into a queryable column; reconcile a
        # carried-over test DB the same way (create_all won't ALTER an existing
        # table). Harmless/no-op once the column exists.
        await conn.execute(
            text(
                "ALTER TABLE decision_record "
                "ADD COLUMN IF NOT EXISTS broker_ref VARCHAR(64)"
            )
        )
        # Story 6.6 adds the (owner_id, co_signed_at) composite index backing the
        # paginated history read; reconcile a carried-over DB the same way (a
        # fresh create_all would build it). Harmless/no-op once it exists.
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS "
                "ix_decision_record_owner_co_signed_at "
                "ON decision_record (owner_id, co_signed_at)"
            )
        )
        # Story 6.7 adds the additive durable-reconciliation columns; reconcile a
        # carried-over DB the same way (create_all won't ALTER an existing table).
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
        # Story 7.2 adds the reclaimer's bounded-age key ``cosigning_at``;
        # reconcile a carried-over DB the same way (create_all won't ALTER an
        # existing table). Harmless/no-op once the column exists.
        await conn.execute(
            text(
                "ALTER TABLE decision_record "
                "ADD COLUMN IF NOT EXISTS cosigning_at TIMESTAMPTZ"
            )
        )
    yield


# --- 2. shared app + TestClient (function-scoped) ---------------------------


@pytest.fixture
def client(ensure_tables):
    """A fresh app + TestClient per test.

    ``TestClient`` as a context manager runs the app lifespan
    (``create_db_and_tables`` + ``run_startup_migrations``). FUNCTION-scoped (the
    default): a session-scoped fixture reusing the module-global asyncpg engine
    across pytest-asyncio's per-function event loops caused a cross-event-loop
    stall (asyncpg connections are loop-bound; the engine uses ``NullPool``), so
    the suite hung at ~0% CPU. A fresh app per test keeps each engine interaction
    on the test's own loop. ``ensure_tables`` is depended on so the schema
    reconciliation runs before the first request.
    """
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(autouse=True)
def _isolate_dependency_overrides(request):
    """Restore ``dependency_overrides`` after every test that uses ``client``.

    With the function-scoped ``client`` fixture each test gets a fresh app, so
    per-test ``dependency_overrides`` (e.g. a ``get_broker`` spy) cannot leak into
    the next test — this guard is effectively a harmless no-op today. It is kept
    as-is (cheap, still correct) so it stays a safety net if ``client`` is ever
    shared more broadly. No-op for tests that don't use ``client``.
    """
    if "client" not in request.fixturenames:
        yield
        return
    app = request.getfixturevalue("client").app
    baseline = dict(app.dependency_overrides)
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(baseline)
