"""Tests for the live-brokerage-link safety guard (2026-08-07 go-live follow-up).

The guard's abort wiring (``pytest.exit`` in a session-autouse fixture) can't be
exercised from inside a running session, so we test its decision logic directly:
the pre-existing-row count, the override switch, and the refusal message.
"""

from __future__ import annotations

import uuid

from db.connection import get_connection
from tests.brokerage_db_guard import (
    OVERRIDE_ENV,
    guard_message,
    override_enabled,
    preexisting_brokerage_token_count,
)


def _seed_user_with_token() -> uuid.UUID:
    """Insert a temp user + one brokerage_token row; return the user id."""
    uid = uuid.uuid4()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO "user" (id, email, hashed_password, is_active, '
                "is_superuser, is_verified) VALUES (%s, %s, %s, TRUE, FALSE, FALSE)",
                (str(uid), f"dbguard-{uid.hex}@example.com", "x" * 60),
            )
            cur.execute(
                "INSERT INTO brokerage_token "
                "(id, owner_id, provider, access_token, refresh_token, expires_at) "
                "VALUES (%s, %s, 'schwab', 'ct', 'ct', now())",
                (str(uuid.uuid4()), str(uid)),
            )
        conn.commit()
    return uid


def _delete_user(uid: uuid.UUID) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "user" WHERE id = %s', (str(uid),))  # cascades
        conn.commit()


def test_count_reflects_a_seeded_token_row():
    before = preexisting_brokerage_token_count()
    assert isinstance(before, int) and before >= 0
    uid = _seed_user_with_token()
    try:
        assert preexisting_brokerage_token_count() == before + 1
    finally:
        _delete_user(uid)
    assert preexisting_brokerage_token_count() == before


def test_guard_message_names_count_and_override():
    msg = guard_message(3)
    assert "REFUSING" in msg
    assert "3" in msg
    assert OVERRIDE_ENV in msg


def test_override_switch(monkeypatch):
    monkeypatch.delenv(OVERRIDE_ENV, raising=False)
    assert override_enabled() is False
    monkeypatch.setenv(OVERRIDE_ENV, "1")
    assert override_enabled() is True
    monkeypatch.setenv(OVERRIDE_ENV, "0")
    assert override_enabled() is False
