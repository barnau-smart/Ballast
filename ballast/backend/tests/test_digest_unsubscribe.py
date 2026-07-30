"""Story 5.1 — the unauthenticated one-click unsubscribe + email-only canary.

The unsubscribe link in every email must work WITHOUT a session (the clicker has
only their token), flip the opt-in off, and return the SAME calm confirmation
whether or not the token matched (no account enumeration). Plus a structural
canary: the ``digest`` package imports no push / SMS / telephony library — email
is the only channel (FR21).
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from api.app import create_app
from db.connection import get_connection
from fastapi.testclient import TestClient

PASSWORD = "supersecret123"


def _unique_email() -> str:
    return f"digest-unsub-{uuid.uuid4().hex}@example.com"


def _delete_user(email: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "user" WHERE email = %s', (email,))
        conn.commit()


def _pref_row(email: str):
    """Return (opted_in, unsubscribe_token) for a user's preference row."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT p.opted_in, p.unsubscribe_token "
                "FROM digest_preference p "
                'JOIN "user" u ON u.id = p.owner_id '
                "WHERE u.email = %s",
                (email,),
            )
            return cur.fetchone()


def _register(client: TestClient, email: str) -> None:
    resp = client.post(
        "/api/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert resp.status_code == 201, resp.text


def _login(client: TestClient, email: str) -> str:
    resp = client.post(
        "/api/auth/jwt/login",
        data={"username": email, "password": PASSWORD},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


def test_valid_token_unsubscribes(client):
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        client.put(
            "/api/digest/preference", headers=headers, json={"opted_in": True}
        )

        opted_in, token = _pref_row(email)
        assert opted_in is True
        assert token

        # One-click unsubscribe, NO auth header.
        r = client.get(f"/api/digest/unsubscribe?token={token}")
        assert r.status_code == 200
        assert "unsubscribed" in r.text.lower()

        opted_in_after, _ = _pref_row(email)
        assert opted_in_after is False

        # The user's own read now reflects the opt-out.
        r = client.get("/api/digest/preference", headers=headers)
        assert r.json() == {"opted_in": False}
    finally:
        _delete_user(email)


def test_post_token_unsubscribes(client):
    """The RFC 8058 one-click target is POST and performs the same opt-out.

    A mail client's native ``List-Unsubscribe-Post`` one-click (and any confirm
    button) POSTs here; a scanner that only pre-fetches GET links can't opt a
    user out through it.
    """
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        client.put(
            "/api/digest/preference", headers=headers, json={"opted_in": True}
        )
        _opted_in, token = _pref_row(email)

        r = client.post(f"/api/digest/unsubscribe?token={token}")
        assert r.status_code == 200
        assert "unsubscribed" in r.text.lower()

        opted_in_after, _ = _pref_row(email)
        assert opted_in_after is False
    finally:
        _delete_user(email)


def test_unknown_and_blank_token_return_calm_200(client):
    # Unknown token → calm 200, no enumeration.
    r = client.get(f"/api/digest/unsubscribe?token=nope-{uuid.uuid4().hex}")
    assert r.status_code == 200
    unknown_body = r.text

    # Missing token entirely → same calm 200.
    r = client.get("/api/digest/unsubscribe")
    assert r.status_code == 200
    # Identical confirmation, so nothing distinguishes a hit from a miss.
    assert r.text == unknown_body
    assert "unsubscribed" in r.text.lower()


@pytest.mark.asyncio
async def test_unsubscribe_by_token_swallows_commit_failure():
    """A DB failure on a VALID token must not surface a 500 / enumeration signal.

    The endpoint returns the SAME calm 200 for a True or False result, so if the
    commit failed and the exception bubbled, a valid-token error (500) would be
    distinguishable from a blank/unknown token (200) — leaking token validity.
    ``unsubscribe_by_token`` must roll back best-effort and report "not matched".
    """
    from digest.preferences import unsubscribe_by_token

    class _Pref:
        opted_in = True
        updated_at = None

    class _Result:
        def scalars(self):  # noqa: D401 — stub
            class _Scalars:
                def first(self_inner):
                    return _Pref()

            return _Scalars()

    class _FailingSession:
        rolled_back = False

        async def execute(self, _stmt):
            return _Result()

        async def commit(self):
            raise RuntimeError("db down")

        async def rollback(self):
            self.rolled_back = True

    session = _FailingSession()
    matched = await unsubscribe_by_token(session, "a-real-looking-token")
    assert matched is False  # never raises; identical to the no-match path
    assert session.rolled_back is True


def test_digest_module_imports_no_push_or_sms_library():
    """Structural canary: the digest package is EMAIL ONLY (FR21).

    Scans every digest/*.py file and asserts none imports a push-notification,
    SMS, or telephony library. ``smtplib`` (email) is the only sanctioned
    transport and is allowed.
    """
    digest_root = Path(__file__).resolve().parent.parent / "digest"
    forbidden = re.compile(
        r"^\s*(?:import|from)\s+"
        r"(twilio|nexmo|vonage|plivo|sinch|firebase_admin|pyfcm|fcm|apns|apns2|"
        r"onesignal|pusher|boto3)\b",
        re.MULTILINE,
    )
    offenders: list[str] = []
    for path in digest_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if forbidden.search(path.read_text(encoding="utf-8")):
            offenders.append(path.name)
    assert offenders == [], (
        f"digest is email-only; these files import a push/SMS library: {offenders}"
    )
