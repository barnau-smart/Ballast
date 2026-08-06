"""Pre-unattended-prod hardening (go-live sweep 2026-08-06) — broker_ref width.

``decision_record.broker_ref`` is ``VARCHAR(64)``. It is written to the queryable
column at placement (``persist_broker_ref``) and again at ``cosign`` — both AFTER
a live order already exists. Without a guard, an over-length ref would raise a DB
``DataError`` at commit, converting a successful placement into an uncaught 500
with the row stranded in ``cosigning`` and NO durable ref (the exact zombie Story
7.2 eliminates). ``_fit_broker_ref`` clamps to the column width so that write can
never raise; the FULL ref is still preserved verbatim in the cosign snapshot /
``OrderOutcome``. Real Schwab order ids are short integers, far under the limit,
so truncation never triggers in practice — this is a defensive backstop.

Pure unit tests: no DB, no network, no credentials.
"""

from __future__ import annotations

import logging

from coach.decision_record import BROKER_REF_MAX_LEN, _fit_broker_ref


def test_short_ref_passes_through_unchanged():
    # A realistic Schwab order id (a short integer string) is returned verbatim.
    assert _fit_broker_ref("1003842571") == "1003842571"


def test_none_passes_through():
    # A no-order_id timeout surfaces broker_ref=None; must stay None (never "").
    assert _fit_broker_ref(None) is None


def test_ref_exactly_at_limit_is_unchanged():
    ref = "x" * BROKER_REF_MAX_LEN
    assert _fit_broker_ref(ref) == ref


def test_over_length_ref_is_truncated_to_column_width(caplog):
    over = "y" * (BROKER_REF_MAX_LEN + 50)
    with caplog.at_level(logging.WARNING, logger="ballast.coach.decision_record"):
        fitted = _fit_broker_ref(over)
    # Truncated to exactly the column width — the write can never DataError.
    assert fitted == "y" * BROKER_REF_MAX_LEN
    assert len(fitted) == BROKER_REF_MAX_LEN
    # And the truncation is surfaced, not silent.
    assert any("exceeds column width" in r.message for r in caplog.records)
