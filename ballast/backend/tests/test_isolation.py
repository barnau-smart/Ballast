"""Story 1.4 isolation proofs — REAL DB, no mocks (AD-10 / NFR5).

These assert the security spine's core property directly: under a user scope,
the ``ScopedRepository`` is a cage — user A can never read or mutate user B's
rows — and there is no way to run unscoped (fail-closed). ``Scope.system()``
is the sole, explicit cross-user path.

A representative owned model (``_IsolationItem``) is defined IN THIS MODULE and
its table is created/dropped inside a fixture so the production schema stays
clean. It is bound to a private declarative base (NOT the app ``Base``) so it
never leaks into ``create_db_and_tables`` on app startup.

Requires the docker Postgres (`docker compose up -d db`) running at the default
DATABASE_URL. Each test creates real users via the register flow and cleans up
its own rows.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import Column, MetaData, String, Table
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID as PgUUID

from db.connection import get_connection
from db.models import OwnedEntityMixin
from db.repository import ScopedRepository
from db.scope import Scope
from db.session import async_session_maker, engine


# --- A test-only owned model on ITS OWN metadata (production schema stays clean)
#
# Using a private ``MetaData``/base guarantees the app's
# ``Base.metadata.create_all`` (run on every TestClient startup in the other
# suites) NEVER sees this model, so ``test_isolation_item`` cannot leak into
# the production schema. To make the mixin's ``ForeignKey("user.id")`` resolve
# within this private metadata, we register a minimal reference to the real
# ``user`` table (id column only — we never create/drop it).


class _IsolationBase(DeclarativeBase):
    metadata = MetaData()


# Minimal stand-in so ForeignKey("user.id") resolves in this private metadata.
# We only ever create/drop the test table, never this one.
Table(
    "user",
    _IsolationBase.metadata,
    Column("id", PgUUID(as_uuid=True), primary_key=True),
)


class _IsolationItem(OwnedEntityMixin, _IsolationBase):
    __tablename__ = "test_isolation_item"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4
    )
    label: Mapped[str] = mapped_column(String(length=64), nullable=False)


# --- Helpers to make real users (owner_id FKs to "user".id) ------------------


def _make_user() -> uuid.UUID:
    """Insert a real user row directly and return its UUID id."""
    user_id = uuid.uuid4()
    email = f"iso-test-{user_id.hex}@example.com"
    # A syntactically-valid but inert hash placeholder; login is not exercised.
    hashed = "x" * 60
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO "user" '
                "(id, email, hashed_password, is_active, is_superuser, is_verified) "
                "VALUES (%s, %s, %s, TRUE, FALSE, FALSE)",
                (str(user_id), email, hashed),
            )
        conn.commit()
    return user_id


def _delete_user(user_id: uuid.UUID) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "user" WHERE id = %s', (str(user_id),))
        conn.commit()


# --- Fixtures: create/drop the test table; provide two owners ----------------


@pytest_asyncio.fixture
async def isolation_table():
    """Create ONLY the test table before the test, drop it after.

    We operate on ``_IsolationItem.__table__`` specifically (not the whole
    ``Base.metadata``) so the production ``user`` table is untouched and the
    test table never lingers in the real schema.
    """
    table = _IsolationItem.__table__
    async with engine.begin() as conn:
        await conn.run_sync(table.create, checkfirst=True)
    try:
        yield
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(table.drop, checkfirst=True)


@pytest.fixture
def two_users():
    """Two real, distinct user ids (A, B). Cleaned up after the test."""
    a = _make_user()
    b = _make_user()
    try:
        yield a, b
    finally:
        _delete_user(a)
        _delete_user(b)


# --- Tests -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_scope_lists_only_own_rows(isolation_table, two_users):
    """AC3: user A's repo lists ONLY A's rows and never B's."""
    a, b = two_users
    async with async_session_maker() as session:
        repo_a = ScopedRepository(_IsolationItem, Scope.for_user(a), session)
        repo_b = ScopedRepository(_IsolationItem, Scope.for_user(b), session)

        await repo_a.add(label="a-one")
        await repo_a.add(label="a-two")
        await repo_b.add(label="b-one")
        await session.commit()

        a_rows = await repo_a.list()
        assert {r.label for r in a_rows} == {"a-one", "a-two"}
        assert all(r.owner_id == a for r in a_rows)
        # Hard negative: A sees ZERO of B's rows.
        assert all(r.label != "b-one" for r in a_rows)
        assert len(a_rows) == 2

        b_rows = await repo_b.list()
        assert {r.label for r in b_rows} == {"b-one"}


@pytest.mark.asyncio
async def test_user_scope_cannot_get_other_users_row(isolation_table, two_users):
    """AC3: a get() of a specific B-owned row under A's scope returns None."""
    a, b = two_users
    async with async_session_maker() as session:
        repo_b = ScopedRepository(_IsolationItem, Scope.for_user(b), session)
        b_item = await repo_b.add(label="b-secret")
        await session.commit()
        b_item_id = b_item.id

    async with async_session_maker() as session:
        repo_a = ScopedRepository(_IsolationItem, Scope.for_user(a), session)
        # B's row exists, but A must not be able to see it.
        assert await repo_a.get(b_item_id) is None
        # B, however, can fetch its own row.
        repo_b = ScopedRepository(_IsolationItem, Scope.for_user(b), session)
        got = await repo_b.get(b_item_id)
        assert got is not None and got.owner_id == b


@pytest.mark.asyncio
async def test_user_scope_cannot_mutate_other_users_row(isolation_table, two_users):
    """AC3: A cannot mutate a B-owned row — get() gives None, so no cross-write."""
    a, b = two_users
    async with async_session_maker() as session:
        repo_b = ScopedRepository(_IsolationItem, Scope.for_user(b), session)
        b_item = await repo_b.add(label="b-original")
        await session.commit()
        b_item_id = b_item.id

    async with async_session_maker() as session:
        repo_a = ScopedRepository(_IsolationItem, Scope.for_user(a), session)
        # The only handle the repo gives A is None — there is nothing to mutate.
        target = await repo_a.get(b_item_id)
        assert target is None

    # Confirm out-of-band that B's row is untouched.
    async with async_session_maker() as session:
        repo_b = ScopedRepository(_IsolationItem, Scope.for_user(b), session)
        still = await repo_b.get(b_item_id)
        assert still is not None and still.label == "b-original"


@pytest.mark.asyncio
async def test_add_stamps_owner_from_scope(isolation_table, two_users):
    """A's add() stamps owner_id == A; forging another owner is refused."""
    a, b = two_users
    async with async_session_maker() as session:
        repo_a = ScopedRepository(_IsolationItem, Scope.for_user(a), session)
        item = await repo_a.add(label="mine")
        assert item.owner_id == a

        # Attempting to forge B as the owner under A's scope raises.
        with pytest.raises(ValueError):
            await repo_a.add(label="forged", owner_id=b)

        # Passing the matching owner_id explicitly is allowed (still A).
        matching = await repo_a.add(label="explicit-self", owner_id=a)
        assert matching.owner_id == a
        await session.rollback()


@pytest.mark.asyncio
async def test_system_scope_sees_across_users(isolation_table, two_users):
    """AC2: a Scope.system() repo spans all owners — the only cross-user path."""
    a, b = two_users
    async with async_session_maker() as session:
        repo_a = ScopedRepository(_IsolationItem, Scope.for_user(a), session)
        repo_b = ScopedRepository(_IsolationItem, Scope.for_user(b), session)
        await repo_a.add(label="a-row")
        await repo_b.add(label="b-row")
        await session.commit()

        system_repo = ScopedRepository(_IsolationItem, Scope.system(), session)
        rows = await system_repo.list()
        owners = {r.owner_id for r in rows}
        # Sees BOTH users' rows — required the explicit system scope.
        assert a in owners and b in owners
        assert {"a-row", "b-row"}.issubset({r.label for r in rows})


@pytest.mark.asyncio
async def test_system_add_requires_explicit_owner(isolation_table, two_users):
    """A system repo will not silently create ownerless rows; owner_id required."""
    a, _ = two_users
    async with async_session_maker() as session:
        system_repo = ScopedRepository(_IsolationItem, Scope.system(), session)
        with pytest.raises(ValueError):
            await system_repo.add(label="no-owner")
        # With an explicit owner it works.
        item = await system_repo.add(label="owned", owner_id=a)
        assert item.owner_id == a
        await session.rollback()


@pytest.mark.asyncio
async def test_missing_scope_raises_fail_closed(isolation_table):
    """AC1: constructing a repo without a scope raises — never all-access."""
    async with async_session_maker() as session:
        with pytest.raises((ValueError, TypeError)):
            ScopedRepository(_IsolationItem, None, session)  # type: ignore[arg-type]


def test_scope_value_object_is_fail_closed():
    """The Scope object itself cannot be constructed into an all-access state."""
    # No user id -> raises (never all-access).
    with pytest.raises(ValueError):
        Scope.for_user(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Scope.for_user("not-a-uuid")  # type: ignore[arg-type]

    uid = uuid.uuid4()
    user_scope = Scope.for_user(uid)
    assert user_scope.is_system is False
    assert user_scope.user_id == uid

    system_scope = Scope.system()
    assert system_scope.is_system is True
    # user_id is undefined for a system scope — asking is a programming error.
    with pytest.raises(ValueError):
        _ = system_scope.user_id


def test_scope_cannot_be_constructed_directly():
    """The raw dataclass constructor must NOT be a back-door to an all-access
    scope — the only cross-user path stays the greppable ``Scope.system()``
    (AD-10 auditability guarantee)."""
    from db.scope import _ScopeKind  # private on purpose

    # A bare SYSTEM construction would yield a cross-user scope without typing
    # Scope.system(); it must raise.
    with pytest.raises(TypeError):
        Scope(_ScopeKind.SYSTEM, None)  # type: ignore[call-arg]
    # A bare USER construction (even well-formed) must also be refused.
    with pytest.raises(TypeError):
        Scope(_ScopeKind.USER, uuid.uuid4())  # type: ignore[call-arg]
