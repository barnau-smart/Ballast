"""Test-suite safety guard: never let the suite wipe a live brokerage link.

Several tests DELETE ``brokerage_token`` rows for isolation (e.g. the
``bind_operator_token`` tests need a clean global row set because that function
reads across all owners). On a DEDICATED test DB that is harmless — it starts
empty and each test creates + cleans its own rows within the session. But when
the suite is pointed at a SHARED dev DB that holds a real Schwab OAuth link,
those deletes WIPE the live link. That bit us repeatedly during the 2026-08-07
go-live: every full-suite run destroyed the linked token and forced a re-link.

This module provides a session-start check (wired as an autouse fixture in
``conftest.py``): if ``brokerage_token`` already has any row at session start,
the DB holds a pre-existing link the suite would destroy, so the run is refused.
Override with ``BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1`` for a knowingly-disposable
DB (CI, a scratch database, or after you've cleared the link on purpose).
"""

from __future__ import annotations

import os

from db.connection import get_connection

OVERRIDE_ENV = "BALLAST_ALLOW_DIRTY_BROKERAGE_DB"


def preexisting_brokerage_token_count() -> int:
    """Rows in ``brokerage_token`` right now (0 if the table doesn't exist yet).

    Called at SESSION START (before any test creates fixtures), so a non-zero
    result means the target DB holds a pre-existing brokerage link — on a proper
    test DB this is always 0. Returns 0 (not an error) if the table hasn't been
    created yet, so a brand-new/empty DB is never falsely flagged.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('brokerage_token')")
            if cur.fetchone()[0] is None:
                return 0
            cur.execute("SELECT count(*) FROM brokerage_token")
            return int(cur.fetchone()[0])


def override_enabled() -> bool:
    """True when the operator has explicitly marked this DB as disposable."""
    return os.environ.get(OVERRIDE_ENV) == "1"


def guard_message(count: int) -> str:
    """The refusal message shown when a pre-existing link would be wiped."""
    return (
        f"REFUSING TO RUN THE SUITE: brokerage_token already has {count} row(s). "
        "The suite's per-test isolation DELETEs brokerage_token and would WIPE "
        "them — possibly a LIVE Schwab link (this happened on the 2026-08-07 "
        "go-live). Point the tests at a dedicated/empty test DB, or clear the "
        f"link first, or set {OVERRIDE_ENV}=1 if this DB is disposable."
    )
