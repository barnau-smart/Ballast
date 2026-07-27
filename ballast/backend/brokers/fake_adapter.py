"""FakeBrokerAdapter — the credential-free implementation of :class:`BrokerPort`.

This is the DEFAULT adapter (``BROKER_ADAPTER=fake``). It makes the entire
OAuth link flow — authorize -> callback -> token storage -> status — fully
runnable and testable locally with ZERO credentials and ZERO network calls.

Everything is deterministic so tests can assert exact values. When the user's
real Schwab developer app is approved, flipping ``BROKER_ADAPTER=schwab`` swaps
in the real adapter with no caller changes (AD-8).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote, urlencode

from brokers.port import BrokerPort, BrokerTokens, Holding, PortfolioSnapshot

# A recognisable, obviously-fake authorization host so it can never be mistaken
# for a real Schwab URL in logs or the UI.
_FAKE_AUTH_BASE = "https://fake-broker.ballast.local/oauth/authorize"

# Deterministic, obviously-fake token material. Tests assert the stored DB value
# differs from these plaintext strings (proving encryption at rest).
FAKE_ACCESS_TOKEN = "fake-access-token-ballast-local"
FAKE_REFRESH_TOKEN = "fake-refresh-token-ballast-local"

# A small, obviously-fake but realistic holdings set + cash. Deterministic so
# tests can assert exact values. Money is Decimal (never float). These broad
# index funds mirror the v1 "stable core" universe (Story 2.5 maps them).
FAKE_HOLDINGS: tuple[Holding, ...] = (
    Holding(
        symbol="VTI",
        quantity=Decimal("10"),
        market_value=Decimal("2500.00"),
        cost_basis=Decimal("2000.00"),
    ),
    Holding(
        symbol="VXUS",
        quantity=Decimal("20"),
        market_value=Decimal("1200.00"),
        cost_basis=Decimal("1100.00"),
    ),
    Holding(
        symbol="BND",
        quantity=Decimal("15"),
        market_value=Decimal("1050.00"),
        cost_basis=Decimal("1080.00"),
    ),
)
FAKE_CASH = Decimal("750.25")

# A fixed base ``as_of`` so reconcile-wins tests can drive older/newer snapshots
# deterministically. Callers/tests advance it via ``as_of_offset``; the default
# is a stable timestamp (no wall-clock, so assertions never flake).
FAKE_AS_OF_BASE = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


class FakeBrokerAdapter(BrokerPort):
    """A deterministic, offline stand-in for a real brokerage.

    ``as_of_offset`` shifts the snapshot's ``as_of`` from :data:`FAKE_AS_OF_BASE`
    so tests can construct older/newer snapshots on demand to exercise the
    single-writer reconcile-wins rule (AD-14). It never uses wall-clock time, so
    every fetched snapshot is fully deterministic.
    """

    provider = "fake"

    def __init__(self, *, as_of_offset: timedelta | None = None) -> None:
        self._as_of_offset = as_of_offset or timedelta(0)

    def authorization_url(self, state: str) -> str:
        """Return a deterministic fake authorization URL embedding ``state``."""
        query = urlencode(
            {
                "response_type": "code",
                "client_id": "fake-client-id",
                "state": state,
            },
            quote_via=quote,
        )
        return f"{_FAKE_AUTH_BASE}?{query}"

    def exchange_code(self, code: str, state: str) -> BrokerTokens:
        """Return deterministic fake tokens for any ``code`` (no network).

        ``state`` is accepted to match :class:`BrokerPort` (the real adapter
        needs it for authlib's state check); the fake ignores it.
        """
        # Include a fixed suffix so the value is stable across runs; the code is
        # NOT echoed into the token (and is never logged).
        return BrokerTokens(
            access_token=FAKE_ACCESS_TOKEN,
            refresh_token=FAKE_REFRESH_TOKEN,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    def fetch_portfolio(self) -> PortfolioSnapshot:
        """Return the deterministic fake holdings/cash snapshot (no network).

        ``as_of`` is :data:`FAKE_AS_OF_BASE` shifted by this adapter's
        ``as_of_offset`` — deterministic so the reconcile-wins tests can build a
        newer/older snapshot without touching the wall clock.
        """
        return PortfolioSnapshot(
            as_of=FAKE_AS_OF_BASE + self._as_of_offset,
            cash=FAKE_CASH,
            holdings=list(FAKE_HOLDINGS),
        )
