"""Integration tests for GET /api/health.

These hit a REAL database check (no mocks):
- test_health_db_ok requires the docker Postgres (`docker compose up -d db`)
  to be running and reachable at the default DATABASE_URL.
- test_health_db_down points DATABASE_URL at an unreachable port and asserts
  the endpoint degrades gracefully (still HTTP 200, db == "down").
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.app import create_app


def test_health_db_ok() -> None:
    """With Postgres up, health reports ok/ok. Real DB check, not mocked."""
    app = create_app()
    client = TestClient(app)

    resp = client.get("/api/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok", (
        "Expected a live Postgres connection. Is `docker compose up -d db` "
        "running and healthy?"
    )


def test_health_db_down(monkeypatch) -> None:
    """With an unreachable DB, health degrades gracefully (200, db=down)."""
    # Point at a port where nothing is listening; connection settings are read
    # fresh per request via get_settings(), so this override takes effect.
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://ballast:ballast@localhost:59999/ballast",
    )

    app = create_app()
    client = TestClient(app)

    resp = client.get("/api/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db"] == "down"
