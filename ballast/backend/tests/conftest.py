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

2. **Shared, session-scoped app + TestClient.** ``create_app()`` registers every
   router and runs the lifespan (``create_db_and_tables`` + startup migrations).
   Rebuilding it per test was pure overhead. The ``client`` fixture below builds
   it once per session. Because a shared app means per-test
   ``dependency_overrides`` (e.g. the ``get_broker`` spies) could leak, an
   autouse guard snapshots the baseline overrides and restores them after every
   test — preserving the isolation the per-test app used to give for free.
   Scope note: today only ``test_coach_api.py`` consumes this shared session app
   (it was the slow file this story targets). The other endpoint test files still
   define their own function-scoped ``client`` and rebuild ``create_app()`` per
   test; they benefit from the fast hasher (#1) but not this shared app. Migrating
   them onto this fixture is a safe future consolidation, deliberately out of
   Story 8.5's scope.

3. **One-time ``ensure_tables``.** The schema-reconciliation DDL (every
   ``CREATE/ALTER/INDEX ... IF NOT EXISTS`` statement is preserved verbatim from
   ``test_coach_api.py``) now runs once per session instead of before every
   test. It is idempotent, so once-per-session is equivalent.

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


@pytest_asyncio.fixture(scope="session", autouse=True)
async def ensure_tables():
    """Ensure owned tables + carried-over schema reconciliation exist ONCE.

    Every DDL statement is preserved verbatim from the original per-test
    ``ensure_tables`` in ``test_coach_api.py``; it is all ``IF NOT EXISTS`` /
    ``checkfirst`` and therefore idempotent, so running it once per session is
    equivalent to running it before each test — just ~100x cheaper.
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


# --- 2. shared session-scoped app + TestClient ------------------------------


@pytest.fixture(scope="session")
def client(ensure_tables):
    """A single app + TestClient shared across the whole session.

    ``TestClient`` as a context manager runs the app lifespan once
    (``create_db_and_tables`` + ``run_startup_migrations``), so that startup
    work is amortised over the whole suite. ``ensure_tables`` is depended on so
    the schema reconciliation runs before the first request.
    """
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(autouse=True)
def _isolate_dependency_overrides(request):
    """Restore ``dependency_overrides`` after every test that uses the shared app.

    A session-scoped app means a test that sets ``dependency_overrides[...]``
    (e.g. a per-test ``get_broker`` spy) and dies before popping would leak into
    the next test. Snapshot the baseline overrides for whatever app the ``client``
    fixture built and restore them after the test, keeping isolation identical to
    the old per-test-app behaviour. No-op for tests that don't use ``client``.
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
