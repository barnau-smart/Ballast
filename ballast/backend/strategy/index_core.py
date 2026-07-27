"""The index-core strategy reference (Story 2.5 — FR6 / FR10).

The "index core" is a user's stable base: broad, low-cost index funds/ETFs
(total-market, broad large-cap-index, and broad bond funds). Everything else a
user happens to hold — individual stocks, sector/thematic funds, crypto — is
"the rest": not *bad*, just outside the stable base.

FR10 constrains v1 order scope to this broad core (the core may diversify beyond
the S&P at user choice, but never by *avoiding* the index). This module is the
single definition of what's core; the portfolio view (Epic 2) and the Coach
Engine (Epic 4, which recommends investing "into your index core") both consume
it — which is why it lives in ``strategy`` rather than ``coach`` or ``brokers``.

Classification is by symbol only, case-insensitive, and CONSERVATIVE: only a
known broad index fund is core; anything unknown is non-core. The curated set is
fixed in v1 (a per-user editable core universe is a later concern).
"""

from __future__ import annotations

# Curated broad, low-cost index funds/ETFs that make up the v1 "index core".
# Kept small and well-known; extend deliberately (each addition is a strategy
# decision, not a convenience). Symbols are upper-case; matching is case-insensitive.
INDEX_CORE_SYMBOLS: frozenset[str] = frozenset(
    {
        # Broad U.S. equity
        "VTI",  # Vanguard Total U.S. Stock Market
        "ITOT",  # iShares Core S&P Total U.S. Stock Market
        "SCHB",  # Schwab U.S. Broad Market
        "VOO",  # Vanguard S&P 500
        "IVV",  # iShares Core S&P 500
        "SPY",  # SPDR S&P 500
        "SWPPX",  # Schwab S&P 500 Index (mutual fund)
        # Broad international equity
        "VXUS",  # Vanguard Total International Stock
        "IXUS",  # iShares Core MSCI Total International Stock
        "VEU",  # Vanguard FTSE All-World ex-US
        # Whole-world equity
        "VT",  # Vanguard Total World Stock
        # Broad bonds
        "BND",  # Vanguard Total Bond Market
        "AGG",  # iShares Core U.S. Aggregate Bond
        "BNDX",  # Vanguard Total International Bond
        "SCHZ",  # Schwab U.S. Aggregate Bond
    }
)

# A short, plain-English description of what the core is — surfaced to the user
# and reused by the coach. No unexplained jargon.
INDEX_CORE_RATIONALE = (
    "Your index core is the steady base of your portfolio: broad, low-cost funds "
    "that hold a little piece of the whole market at once, instead of betting on "
    "any single company."
)


def is_index_core(symbol: str | None) -> bool:
    """Return True iff ``symbol`` is part of the broad index core (FR6/FR10).

    Case-insensitive; ``None``/blank and any symbol not in the curated set are
    non-core (conservative — only a known broad index fund counts as core).
    """
    if not symbol:
        return False
    return symbol.strip().upper() in INDEX_CORE_SYMBOLS
