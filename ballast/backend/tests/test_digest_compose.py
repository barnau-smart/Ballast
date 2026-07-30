"""Story 5.1 — unit tests for the calm digest composition (no DB, no network).

Walks the compose-relevant I/O & Edge-Case Matrix rows: a populated portfolio
yields plan-status + core-plan reinforcement; an empty portfolio yields the
gentle set-up-and-steady variant; both carry the one-click unsubscribe link and
NEVER use alarmist / urgency / FOMO wording (tone is a testable AC, NFR8). The
composer is a pure function — the same inputs yield a byte-identical message.
"""

from __future__ import annotations

import re
from decimal import Decimal
from types import SimpleNamespace

from brokers.portfolio import PortfolioView
from digest.compose import compose_digest

UNSUB = "http://localhost:8000/api/digest/unsubscribe?token=abc123"
EMAIL = "calm@example.com"

# Words that would betray the calm-coach voice — alarm, urgency, FOMO, or a "red"
# framing. The digest copy must contain none of these (case-insensitive).
FORBIDDEN = [
    "urgent", "hurry", "act now", "act fast", "don't miss", "dont miss",
    "missing out", "miss out", "last chance", "limited time", "warning",
    "alarm", "panic", "crash", "plunge", "fear", "red", "alert", "immediately",
]


def _holding(symbol: str):
    """A minimal stand-in carrying just what compose reads (``.symbol``)."""
    return SimpleNamespace(symbol=symbol)


def _assert_calm(message):
    blob = f"{message.subject}\n{message.text_body}\n{message.html_body}".lower()
    for word in FORBIDDEN:
        # Match on word boundaries so a forbidden term like "red" flags "red"
        # but not innocuous substrings ("covered", "required", "hundred").
        pattern = r"\b" + re.escape(word) + r"\b"
        assert not re.search(pattern, blob), (
            f"digest copy should never say {word!r}"
        )


def test_populated_portfolio_has_plan_status_and_core_reinforcement():
    view = PortfolioView(
        holdings=[_holding("VTI"), _holding("VXUS"), _holding("BND")],
        cash=Decimal("750.00"),
        as_of=None,
    )
    msg = compose_digest(view, unsubscribe_url=UNSUB, recipient_email=EMAIL)

    assert msg.to == EMAIL
    assert "3 holdings" in msg.text_body
    # All three are index-core → the copy reinforces the long-term core plan.
    assert "index core" in msg.text_body.lower()
    assert UNSUB in msg.text_body
    assert UNSUB in msg.html_body
    # The one-click List-Unsubscribe target is surfaced for a scanner-safe POST.
    assert msg.list_unsubscribe_url == UNSUB
    _assert_calm(msg)


def test_partial_core_mix_counts_core_holdings():
    view = PortfolioView(
        holdings=[_holding("VTI"), _holding("TSLA")],  # one core, one not
        cash=Decimal("0"),
        as_of=None,
    )
    msg = compose_digest(view, unsubscribe_url=UNSUB, recipient_email=EMAIL)
    assert "2 holdings" in msg.text_body
    assert "1 of them in your long-term index core" in msg.text_body
    _assert_calm(msg)


def test_empty_portfolio_uses_gentle_setup_variant():
    view = PortfolioView(holdings=[], cash=Decimal("0"), as_of=None)
    msg = compose_digest(view, unsubscribe_url=UNSUB, recipient_email=EMAIL)

    assert view.is_empty
    assert "nothing to summarise yet" in msg.text_body.lower()
    assert UNSUB in msg.text_body
    _assert_calm(msg)


def test_compose_is_deterministic():
    view = PortfolioView(
        holdings=[_holding("VOO")], cash=Decimal("10.00"), as_of=None
    )
    a = compose_digest(view, unsubscribe_url=UNSUB, recipient_email=EMAIL)
    b = compose_digest(view, unsubscribe_url=UNSUB, recipient_email=EMAIL)
    assert a == b


def test_single_holding_uses_singular_wording():
    view = PortfolioView(
        holdings=[_holding("VTI")], cash=Decimal("0"), as_of=None
    )
    msg = compose_digest(view, unsubscribe_url=UNSUB, recipient_email=EMAIL)
    assert "1 holding," in msg.text_body
    assert "1 holdings" not in msg.text_body
