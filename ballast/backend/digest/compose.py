"""Compose the calm weekly digest email (Story 5.1, FR21 / NFR8).

The digest is intentionally STATIC copy — no LLM. Tone is a testable acceptance
criterion (patient, warm, honest, plain — never alarmist, urgent, or FOMO), and
static templating keeps it deterministic and unit-testable. The content is only
"plan status + on-track reinforcement": it summarises the user's cached portfolio
projection and reassures them they are on track. It computes NO market statistics
of its own (the digest never shows a scary number) and reads plan status only
from the portfolio projection it is handed, mapping core holdings via
``strategy.index_core.is_index_core``.

Every email carries a one-click unsubscribe link — the digest is the product's
one proactive touch, so opting out must be trivial.
"""

from __future__ import annotations

import html

from brokers.portfolio import PortfolioView
from digest.email_port import EmailMessage
from strategy.index_core import is_index_core

_SUBJECT = "Your steady week with Ballast"


def _plan_status_line(view: PortfolioView) -> str:
    """One calm, honest sentence about where the user's plan stands.

    Never alarmist, never a market figure — just a plain count of holdings and a
    nod to the index-core base. Handles the never-imported / empty account with a
    gentle set-up-and-steady variant.
    """
    if view.is_empty:
        return (
            "You're all set up and steady. There's nothing to summarise yet — "
            "once you connect and import your holdings, this note will reflect "
            "your plan. Nothing needs your attention."
        )

    total = len(view.holdings)
    core = sum(1 for h in view.holdings if is_index_core(h.symbol))
    holdings_word = "holding" if total == 1 else "holdings"
    if core == total:
        base = (
            f"You're holding {total} {holdings_word}, all part of your "
            "long-term index core."
        )
    elif core > 0:
        base = (
            f"You're holding {total} {holdings_word}, {core} of them in your "
            "long-term index core."
        )
    else:
        base = f"You're holding {total} {holdings_word}."
    return f"{base} You're staying the course — nothing here needs your attention."


def _text_body(status_line: str, unsubscribe_url: str) -> str:
    """The plain-text digest — the calm coach voice, plain and short."""
    return (
        "Hi — a quick, calm check-in from Ballast.\n\n"
        f"{status_line}\n\n"
        "This note is just here so you can feel on-track between visits. "
        "It arrives once a week, only because you asked for it.\n\n"
        f"Not useful? Unsubscribe anytime: {unsubscribe_url}\n\n"
        "— Ballast"
    )


def _html_body(status_line: str, unsubscribe_url: str) -> str:
    """A minimal, styled rendering of the same calm copy.

    Text is escaped; the unsubscribe URL is a real one-click link. No colours
    that read as alarm (no red) — a market up/down figure is never shown here, so
    the green▲ / sky-blue▼ rule has nothing to render.
    """
    safe_status = html.escape(status_line)
    safe_url = html.escape(unsubscribe_url, quote=True)
    return (
        "<html><body style=\"font-family: sans-serif; line-height: 1.5; "
        "color: #1a1a1a;\">"
        "<p>Hi — a quick, calm check-in from Ballast.</p>"
        f"<p>{safe_status}</p>"
        "<p>This note is just here so you can feel on-track between visits. "
        "It arrives once a week, only because you asked for it.</p>"
        f"<p style=\"font-size: 0.9em; color: #555;\">Not useful? "
        f"<a href=\"{safe_url}\">Unsubscribe anytime</a>.</p>"
        "<p>— Ballast</p>"
        "</body></html>"
    )


def compose_digest(
    view: PortfolioView,
    *,
    unsubscribe_url: str,
    recipient_email: str,
) -> EmailMessage:
    """Compose the calm weekly digest for one user.

    Pure function of its inputs (deterministic — no wall-clock, no network, no
    LLM). Produces both a plain-text and a minimal HTML body, each carrying the
    plan-status reassurance and the one-click unsubscribe link.
    """
    status_line = _plan_status_line(view)
    return EmailMessage(
        to=recipient_email,
        subject=_SUBJECT,
        text_body=_text_body(status_line, unsubscribe_url),
        html_body=_html_body(status_line, unsubscribe_url),
        # Surface the same one-click opt-out as a List-Unsubscribe target so a
        # real transport can offer a POST-based one-click (scanner-safe) as well
        # as the in-body link.
        list_unsubscribe_url=unsubscribe_url,
    )
