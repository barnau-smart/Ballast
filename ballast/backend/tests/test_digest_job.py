"""Story 5.1 — the SYSTEM-scope weekly digest send job (REAL DB).

Mirrors the market-ingest test style (async + async_session_maker, real DB, no
mocks; the FakeEmailAdapter is the tested transport). Verifies: only opted-in
users are emailed; the job is idempotent within an ISO week (a re-run sends none);
and one recipient's send failing does not abort the run and leaves that user
UNMARKED (retried next run). Requires the docker Postgres.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from db.connection import get_connection
from db.models import DigestPreference, PortfolioCache, User
from db.scope import Scope
from db.session import async_session_maker, engine
from digest.email_port import EmailMessage, EmailPort
from digest.fake_adapter import FakeEmailAdapter
from digest.job import iso_week_key, send_weekly_digests
from digest.preferences import set_opt_in

# A fixed instant so the ISO-week marker is deterministic across the run.
NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
WEEK = iso_week_key(NOW)
BASE_URL = "http://test.local"


@pytest_asyncio.fixture(autouse=True)
async def ensure_tables():
    """Ensure the tables these tests touch exist (real-DB tests)."""
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create, checkfirst=True)
        await conn.run_sync(PortfolioCache.__table__.create, checkfirst=True)
        await conn.run_sync(DigestPreference.__table__.create, checkfirst=True)
    yield


def _unique_email(tag: str) -> str:
    return f"digest-job-{tag}-{uuid.uuid4().hex}@example.com"


def _insert_user(email: str) -> uuid.UUID:
    uid = uuid.uuid4()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO "user" '
                "(id, email, hashed_password, is_active, is_superuser, is_verified) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (str(uid), email, "x-not-a-real-hash", True, False, False),
            )
        conn.commit()
    return uid


def _delete_user(email: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "user" WHERE email = %s', (email,))
        conn.commit()


def _last_sent_week(owner_id: uuid.UUID) -> str | None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_sent_week FROM digest_preference WHERE owner_id = %s",
                (str(owner_id),),
            )
            row = cur.fetchone()
    return row[0] if row else None


async def _opt_in(uid: uuid.UUID) -> None:
    async with async_session_maker() as session:
        await set_opt_in(Scope.for_user(uid), session, True)


@pytest.mark.asyncio
async def test_only_opted_in_users_receive():
    email_in = _unique_email("in")
    email_out = _unique_email("out")
    uid_in = _insert_user(email_in)
    _insert_user(email_out)  # never opts in → no preference row
    try:
        await _opt_in(uid_in)

        sender = FakeEmailAdapter()
        async with async_session_maker() as session:
            result = await send_weekly_digests(
                session, sender, unsubscribe_base_url=BASE_URL, now=NOW
            )

        assert result.sent == [email_in]
        assert len(sender.sent) == 1
        assert sender.sent[0].to == email_in
        # The unsubscribe link is built on the base URL + the user's token.
        assert f"{BASE_URL}/api/digest/unsubscribe?token=" in sender.sent[0].text_body
        # The opted-out user was never even loaded (not in results at all).
        assert email_out not in result.sent
        assert _last_sent_week(uid_in) == WEEK
    finally:
        _delete_user(email_in)
        _delete_user(email_out)


@pytest.mark.asyncio
async def test_idempotent_within_week():
    email = _unique_email("idem")
    uid = _insert_user(email)
    try:
        await _opt_in(uid)

        first = FakeEmailAdapter()
        async with async_session_maker() as session:
            r1 = await send_weekly_digests(
                session, first, unsubscribe_base_url=BASE_URL, now=NOW
            )
        assert r1.sent == [email]
        assert len(first.sent) == 1

        # Re-run in the SAME ISO week → nothing sent, the user is skipped.
        second = FakeEmailAdapter()
        async with async_session_maker() as session:
            r2 = await send_weekly_digests(
                session, second, unsubscribe_base_url=BASE_URL, now=NOW
            )
        assert r2.sent == []
        assert str(uid) in r2.skipped
        assert second.sent == []
    finally:
        _delete_user(email)


def _deactivate_user(email: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'UPDATE "user" SET is_active = FALSE WHERE email = %s', (email,)
            )
        conn.commit()


@pytest.mark.asyncio
async def test_deactivated_user_is_skipped():
    """A deactivated account never receives the proactive email (skipped, not sent)."""
    email = _unique_email("inactive")
    uid = _insert_user(email)
    try:
        await _opt_in(uid)
        _deactivate_user(email)

        sender = FakeEmailAdapter()
        async with async_session_maker() as session:
            result = await send_weekly_digests(
                session, sender, unsubscribe_base_url=BASE_URL, now=NOW
            )

        assert sender.sent == []
        assert result.sent == []
        assert email in result.skipped
        # Left unmarked, so a reactivated user is picked up on a later run.
        assert _last_sent_week(uid) is None
    finally:
        _delete_user(email)


class _FailForRecipient(EmailPort):
    """Records sends, but raises for one recipient (a per-user hiccup)."""

    provider = "fake"

    def __init__(self, failing_email: str):
        self._failing = failing_email
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        if message.to == self._failing:
            raise RuntimeError("simulated transport hiccup")
        self.sent.append(message)


@pytest.mark.asyncio
async def test_one_send_failure_does_not_abort_run():
    # Several good users surround the failing one so the failure is exercised
    # regardless of list_opted_in's (unordered) DB order — a per-user rollback
    # must not poison the shared session for the users processed afterwards.
    email_bad = _unique_email("bad")
    good_emails = [_unique_email(f"ok{i}") for i in range(4)]
    uid_bad = _insert_user(email_bad)
    good_uids = [_insert_user(e) for e in good_emails]
    try:
        await _opt_in(uid_bad)
        for uid in good_uids:
            await _opt_in(uid)

        sender = _FailForRecipient(email_bad)
        async with async_session_maker() as session:
            result = await send_weekly_digests(
                session, sender, unsubscribe_base_url=BASE_URL, now=NOW
            )

        # EVERY good user was still sent; the bad one is reported, run continued.
        for email in good_emails:
            assert email in result.sent
        assert email_bad in result.failed
        assert {m.to for m in sender.sent} == set(good_emails)

        # Each successful user is marked; the failed user is left UNMARKED so the
        # next run retries them.
        for uid in good_uids:
            assert _last_sent_week(uid) == WEEK
        assert _last_sent_week(uid_bad) is None
    finally:
        _delete_user(email_bad)
        for email in good_emails:
            _delete_user(email)
