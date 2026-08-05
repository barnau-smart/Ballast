"""Shared, test-only pytest fixtures (Story 8.5 — test-suite performance).

Makes the backend integration suite fast WITHOUT changing product behaviour.
Root-caused hands-on 2026-08-05 after two autonomous passes mis-diagnosed it —
the costs, in order of impact:

0. **Real Anthropic API calls in tests (THE dominant cost).** The local ``.env``
   runs the demo/live config ``LLM_ADAPTER=anthropic`` with a real key, and the
   suite never forced it back to fake, so every test hitting ``/recommend`` made
   a real ~10 s network call at ~1% CPU — this alone made ``test_coach_api.py``
   take ~13 min (and made ``test_recommend_surfaces_fr11_warning`` flaky on a
   live model reply). Forcing ``LLM_ADAPTER=fake`` at module import (top of file,
   before any settings load) restores the deterministic in-process gateway the
   tests were always designed for → ~13 min ⇒ ~20 s, and the flaky test goes
   green. This is the fix that matters.

1. **Test-only fast password hasher.** Production auth stays pwdlib/Argon2
   (``api/users.py`` untouched). We patch ``UserManager.__init__`` so that, when
   no ``password_helper`` is supplied (exactly how production builds it), tests
   get a cheap SHA-256 helper instead of Argon2 (~22 ms/op × hundreds of
   register+login round-trips across ~32 files). Never reachable from production
   (which never imports ``tests``); installed by an autouse fixture.

2. **Schema + migrations run ONCE (``_schema_once``), per-test lifespan neutered.**
   The app lifespan runs ``run_startup_migrations`` — which takes a
   ``pg_advisory_xact_lock`` — on EVERY test; run 108× that lock serialises and
   also caused the earlier session-scoped attempt to HANG forever (a cross-loop
   stuck connection held the lock). ``create_db_and_tables`` +
   ``run_startup_migrations`` are idempotent and superset the old per-test
   ``ensure_tables`` DDL, so we run them once per session (on a dedicated
   ``asyncio.run`` loop, fully decoupled from pytest-asyncio's per-test loops)
   and no-op the per-test lifespan. ``client`` stays FUNCTION-scoped so every
   engine interaction (``NullPool``, asyncpg connections are loop-bound) stays on
   its own event loop — no session-scope hang, just a cheap per-test app.

Per-test isolation is unchanged: unique-email users (``_unique_email``) + row
self-cleanup against the real docker Postgres.
"""

from __future__ import annotations

import hashlib
import os

# --- 0. isolate the whole suite from the developer's local ``.env`` ----------
#
# THE root cause of both the old ~13-min slowness AND four "fake-by-default"
# factory-test failures (found 2026-08-05): the local ``.env`` runs the demo/live
# config (``LLM_ADAPTER=anthropic`` + a real key, ``MARKETDATA_ADAPTER=tiingo``),
# and pydantic-settings reads that file on EVERY ``Settings()`` — even when a test
# ``monkeypatch.delenv(...)``s a var, ``.env`` puts it right back. So tests hit
# the real Anthropic API (slow + non-deterministic), and tests asserting the
# code-default adapter ("returns fake by default", "no key raises") failed because
# ``.env`` supplied a real value the test couldn't clear.
#
# Fix: neutralize the env file for tests, so the suite sees ONLY explicit env vars
# + the code defaults in ``api/config.Settings`` — which are exactly the intended
# offline baseline (``*_ADAPTER=fake``, empty keys, and a ``DATABASE_URL`` default
# identical to the docker URL, so the DB is unaffected). This must run BEFORE
# anything instantiates ``Settings`` — ``db.session`` builds the engine at import
# via ``get_settings()`` — so patch ``model_config`` first, before those imports.
from api.config import Settings as _Settings

_Settings.model_config["env_file"] = None
# Belt-and-suspenders: also pin fake LLM in the process env, so even a real
# ``LLM_ADAPTER`` exported in the shell (not just via .env) can't reach a test.
os.environ["LLM_ADAPTER"] = "fake"

import pytest
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


# --- 3. one-time schema + migrations; neutered per-test lifespan ------------
#
# THE REAL COST (root-caused 2026-08-05): the app lifespan runs
# ``run_startup_migrations`` on EVERY test, and that migration takes a
# ``pg_advisory_xact_lock`` (see db/migrations.py). Run 108 times, each
# acquisition serialises behind the previous test's not-yet-released connection,
# so every test waited ~7s on the lock at ~0% CPU → ~13 min total. (The same
# advisory lock, held by a cross-loop-stuck connection, is what made the earlier
# session-scoped attempt HANG forever.) ``create_db_and_tables`` +
# ``run_startup_migrations`` are idempotent and together superset every DDL the
# old per-test ``ensure_tables`` ran, so the correct design is: run them ONCE per
# session, then make each per-test app lifespan a no-op. This removes the
# repeated advisory-lock contention entirely, with NO event-loop-scope games —
# ``client`` stays function-scoped so every engine interaction stays on its own
# loop, and the fast hasher (#1) remains the CPU-side win.


@pytest.fixture(scope="session", autouse=True)
def _schema_once():
    """Run create_all + startup migrations exactly ONCE for the whole session.

    Executed on its own dedicated event loop via ``asyncio.run`` (created and
    closed here), fully decoupled from pytest-asyncio's per-test loops: the
    ``NullPool`` engine opens a connection on this loop, the migration commits
    (releasing its ``pg_advisory_xact_lock``), and the loop closes — so nothing
    lingers to be awaited on another loop. Idempotent, so a carried-over test DB
    is reconciled exactly as a fresh one.
    """
    import asyncio

    from db.migrations import run_startup_migrations as _real_migrations
    from db.session import create_db_and_tables as _real_create

    async def _setup() -> None:
        await _real_create()
        await _real_migrations(engine)

    asyncio.run(_setup())
    yield


@pytest.fixture(autouse=True)
def _neuter_lifespan_migrations(monkeypatch):
    """Make each per-test app lifespan cheap: no re-run of the migration.

    ``_schema_once`` already built the schema for the whole session, so the
    per-test ``create_app()`` lifespan must NOT re-take the advisory lock. Patch
    the names as looked up inside ``api.app`` (``create_db_and_tables`` +
    ``run_startup_migrations``) to no-ops. Production is untouched — this only
    affects apps constructed under pytest.
    """
    import api.app as _app_module

    async def _noop_create() -> None:
        return None

    async def _noop_migrations(_engine) -> None:
        return None

    monkeypatch.setattr(_app_module, "create_db_and_tables", _noop_create)
    monkeypatch.setattr(_app_module, "run_startup_migrations", _noop_migrations)
    yield


# --- 2. app + TestClient (function-scoped; lifespan now no-op & cheap) -------


@pytest.fixture
def client():
    """A fresh app + TestClient per test.

    Function-scoped so every engine interaction stays on the test's own event
    loop (asyncpg connections are loop-bound; the engine uses ``NullPool``). This
    used to be the slow part because the lifespan re-ran the advisory-locked
    migration each time — but ``_neuter_lifespan_migrations`` (autouse) makes the
    lifespan a no-op and ``_schema_once`` (session) did the real setup once, so a
    fresh app per test is now cheap AND cannot hang.
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
