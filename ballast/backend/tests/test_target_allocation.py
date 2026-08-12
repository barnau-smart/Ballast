"""Story 10.1 — the target-allocation reference data + selection endpoints.

Reference-data tests are pure (no DB). The endpoint tests match the convention
(TestClient + register + JWT login, per-test cleanup): default undecided; GET is
read-only (creates no row); PUT sets a model; an unknown model → calm 422; weights
render as fixed-point strings; and per-user isolation (AD-10). Requires the docker
Postgres.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from api.app import create_app
from db.connection import get_connection
from fastapi.testclient import TestClient
import re

from strategy.target_allocation import (
    ASSET_CLASSES,
    CANONICAL_FUND,
    MODEL_KEYS,
    MODEL_PORTFOLIOS,
    TARGET_MIX_RATIONALE,
    asset_class_for,
    get_model,
    list_models,
    resolve_target,
)
from strategy.index_core import INDEX_CORE_SYMBOLS

PASSWORD = "supersecret123"


# --- Reference data (pure, no DB) --------------------------------------------


def test_every_model_weights_sum_to_exactly_one():
    for key in MODEL_KEYS:
        model = MODEL_PORTFOLIOS[key]
        total = sum(model.weights.values(), Decimal("0"))
        assert total == Decimal("1.00"), (key, total)
        # Every asset class is present with a Decimal weight.
        assert set(model.weights) == set(ASSET_CLASSES)
        assert all(isinstance(w, Decimal) for w in model.weights.values())


def test_symbol_asset_class_map_covers_index_core_except_whole_world():
    # Every curated index-core symbol classifies EXCEPT VT (whole-world, a
    # deliberate spans-classes special case for Story 10-2).
    for symbol in INDEX_CORE_SYMBOLS:
        cls = asset_class_for(symbol)
        if symbol == "VT":
            assert cls is None
        else:
            assert cls in ASSET_CLASSES, symbol
    # Case-insensitive; unknown/blank → None.
    assert asset_class_for("vti") == "us_equity"
    assert asset_class_for("  BND ") == "bonds"
    assert asset_class_for("TSLA") is None
    assert asset_class_for(None) is None


# The digest calm-voice bar (mirrors test_digest_compose.FORBIDDEN) — AC7.
_FORBIDDEN = [
    "urgent", "hurry", "act now", "act fast", "don't miss", "dont miss",
    "missing out", "miss out", "last chance", "limited time", "warning",
    "alarm", "panic", "crash", "plunge", "fear", "red", "alert", "immediately",
]


def test_reference_copy_is_calm_no_fomo():
    """All new reference-data copy (rationale + model names + descriptions) uses
    the calm, non-alarmist voice — no FOMO / urgency / "red" (AC7)."""
    blobs = [TARGET_MIX_RATIONALE]
    for m in list_models():
        blobs += [m.name, m.description]
    blob = " ".join(blobs).lower()
    for word in _FORBIDDEN:
        pattern = r"\b" + re.escape(word) + r"\b"
        assert not re.search(pattern, blob), f"target-mix copy should never say {word!r}"


def test_helpers_are_deterministic_and_case_insensitive():
    assert [m.key for m in list_models()] == list(MODEL_KEYS)
    assert get_model("BALANCED").key == "balanced"
    assert get_model(" growth ").key == "growth"
    assert get_model("nope") is None
    assert get_model(None) is None
    # resolve_target is a pure function of the key.
    assert resolve_target("balanced") == resolve_target("BALANCED")
    r = resolve_target("growth")
    assert r["funds"] == CANONICAL_FUND
    assert r["weights"]["bonds"] == Decimal("0.10")
    assert resolve_target("nope") is None
    assert resolve_target(None) is None


# --- Endpoint tests (REAL DB) ------------------------------------------------


def _unique_email() -> str:
    return f"target-alloc-{uuid.uuid4().hex}@example.com"


def _delete_user(email: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "user" WHERE email = %s', (email,))
        conn.commit()


def _user_id_for(email: str) -> uuid.UUID:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id FROM "user" WHERE email = %s', (email,))
            (uid,) = cur.fetchone()
    return uuid.UUID(str(uid))


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


def test_default_is_undecided_and_offers_choices(client):
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        r = client.get("/api/target-allocation", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["model"] is None  # undecided, never a silent default
        assert body["resolved"] is None
        # The three choices are offered, weights as fixed-point strings.
        assert [c["key"] for c in body["choices"]] == list(MODEL_KEYS)
        bal = next(c for c in body["choices"] if c["key"] == "balanced")
        assert bal["weights"]["us_equity"] == "0.45"
        assert Decimal(str(bal["weights"]["bonds"])) == Decimal("0.35")
    finally:
        _delete_user(email)


def test_get_is_read_only_creates_no_row(client):
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}
        uid = _user_id_for(email)

        r = client.get("/api/target-allocation", headers=headers)
        assert r.status_code == 200
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM target_allocation_config WHERE owner_id = %s",
                    (str(uid),),
                )
                (count,) = cur.fetchone()
        assert count == 0  # a read must never write a row
    finally:
        _delete_user(email)


def test_set_model_persists_and_resolves(client):
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        r = client.put(
            "/api/target-allocation", headers=headers, json={"model": "growth"}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["model"] == "growth"
        assert body["resolved"]["model"] == "growth"
        assert body["resolved"]["funds"]["us_equity"] == "VTI"
        assert body["resolved"]["weights"]["us_equity"] == "0.60"

        # A fresh read reflects it (case-insensitive input normalizes to the key).
        r = client.put(
            "/api/target-allocation", headers=headers, json={"model": "Balanced"}
        )
        assert r.json()["model"] == "balanced"
        r = client.get("/api/target-allocation", headers=headers)
        assert r.json()["model"] == "balanced"
    finally:
        _delete_user(email)


def test_unknown_model_is_calm_422(client):
    email = _unique_email()
    try:
        _register(client, email)
        headers = {"Authorization": f"Bearer {_login(client, email)}"}

        r = client.put(
            "/api/target-allocation", headers=headers, json={"model": "yolo"}
        )
        assert r.status_code == 422, r.text
        # Nothing persisted — still undecided.
        assert client.get("/api/target-allocation", headers=headers).json()["model"] is None
    finally:
        _delete_user(email)


def test_requires_authentication(client):
    assert client.get("/api/target-allocation").status_code == 401


def test_per_user_isolation(client):
    email_a = _unique_email()
    email_b = _unique_email()
    try:
        _register(client, email_a)
        _register(client, email_b)
        headers_a = {"Authorization": f"Bearer {_login(client, email_a)}"}
        headers_b = {"Authorization": f"Bearer {_login(client, email_b)}"}

        client.put("/api/target-allocation", headers=headers_a, json={"model": "growth"})

        # B is unaffected — still undecided (fail-closed per-user, AD-10).
        assert client.get("/api/target-allocation", headers=headers_b).json()["model"] is None
        # A still sees its own choice.
        assert client.get("/api/target-allocation", headers=headers_a).json()["model"] == "growth"
    finally:
        _delete_user(email_a)
        _delete_user(email_b)
