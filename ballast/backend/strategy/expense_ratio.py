"""The fund expense-ratio reference table (Story 10.4 — cost/fees bucket).

The fee-fact SOURCE for the cost/fees analysis bucket. A small, curated table
keyed by upper-case symbol; each entry carries the fund's REAL published net
expense ratio (as a ``Decimal`` percentage-point figure, e.g. ``Decimal("0.03")``
== 0.03% == 3 basis points) plus its asset class. This is the ONLY place a fee
number is stated, so the never-invent-a-fact gate can admit it into the allow-set:
a wrong value here would poison that gate, so every ratio is a real, stable, public
figure (never invented).

Twin of :mod:`strategy.index_core` / :mod:`strategy.target_allocation`: curated,
fixed, deterministic reference data (a live-feed of prospectus fees is a later
concern). Two kinds of entry live here:

- **The index-core / canonical funds** (the cheap SWITCH targets) — their real,
  very low ratios, so the engine can compare a held fund against its same-class
  canonical index fund's true fee.
- **A handful of genuinely well-known HIGHER-fee funds beginners commonly hold**
  (actively-managed / legacy funds) with their real published net expense ratios,
  each mapped to an asset class so the switch bucket can offer the cheaper
  same-class canonical fund.

Classification is by symbol only, case-insensitive, and CONSERVATIVE: a symbol
with no entry has no known fee → NO cost finding (never invent a ratio).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from strategy.target_allocation import BONDS, INTL_EQUITY, US_EQUITY


@dataclass(frozen=True)
class FundCost:
    """One fund's fee fact: its net expense ratio + its asset class (pure).

    ``expense_ratio`` is the REAL published net expense ratio expressed as a
    percentage-point ``Decimal`` (``Decimal("0.03")`` == 0.03%, ``Decimal("1.00")``
    == 1.00%), never binary float. ``asset_class`` is one of the three broad classes
    (:data:`~strategy.target_allocation.ASSET_CLASSES`), so the cost bucket can look
    up the cheaper same-class canonical index fund. Frozen reference data — same
    symbol always yields the same fact.
    """

    expense_ratio: Decimal
    asset_class: str


#: The material fee gap (in percentage points) at/above which the cost bucket fires
#: a switch. 0.20pp is a beginner-meaningful gap that compounds over decades — a
#: broad index fund near 0.03-0.10% versus an active fund near 0.50-1.00% clears it
#: easily, while two low-cost index funds a few basis points apart never do. Tune
#: deliberately (a strategy decision, like the concentration ceiling).
EXPENSE_RATIO_MATERIAL_DELTA: Decimal = Decimal("0.20")


#: The curated expense-ratio table — REAL published net expense ratios only
#: (percentage-point ``Decimal``s). Keyed by UPPER-CASE symbol; matching is
#: case-insensitive via :func:`fund_cost`. Extend deliberately: each addition is a
#: fee-fact commitment the never-invent gate depends on being accurate.
FUND_EXPENSE_RATIOS: dict[str, FundCost] = {
    # --- Index-core / canonical funds: the cheap SWITCH targets ---------------
    # Broad U.S. equity (real net ERs).
    "VTI": FundCost(Decimal("0.03"), US_EQUITY),  # Vanguard Total U.S. Stock Market
    "ITOT": FundCost(Decimal("0.03"), US_EQUITY),  # iShares Core S&P Total U.S.
    "SCHB": FundCost(Decimal("0.03"), US_EQUITY),  # Schwab U.S. Broad Market
    "VOO": FundCost(Decimal("0.03"), US_EQUITY),  # Vanguard S&P 500
    "IVV": FundCost(Decimal("0.03"), US_EQUITY),  # iShares Core S&P 500
    "SPY": FundCost(Decimal("0.0945"), US_EQUITY),  # SPDR S&P 500 (higher of the trio)
    "SWPPX": FundCost(Decimal("0.02"), US_EQUITY),  # Schwab S&P 500 Index (mutual fund)
    # Broad international equity.
    "VXUS": FundCost(Decimal("0.05"), INTL_EQUITY),  # Vanguard Total International
    "IXUS": FundCost(Decimal("0.07"), INTL_EQUITY),  # iShares Core MSCI Total Intl
    "VEU": FundCost(Decimal("0.04"), INTL_EQUITY),  # Vanguard FTSE All-World ex-US
    # Broad bonds.
    "BND": FundCost(Decimal("0.03"), BONDS),  # Vanguard Total Bond Market
    "AGG": FundCost(Decimal("0.03"), BONDS),  # iShares Core U.S. Aggregate Bond
    "BNDX": FundCost(Decimal("0.07"), BONDS),  # Vanguard Total International Bond
    "SCHZ": FundCost(Decimal("0.03"), BONDS),  # Schwab U.S. Aggregate Bond
    # --- Well-known HIGHER-fee funds beginners commonly hold ------------------
    # Real published net expense ratios (actively-managed / legacy funds). Each
    # maps to a broad asset class so the switch bucket offers the cheaper canonical.
    "AGTHX": FundCost(Decimal("0.61"), US_EQUITY),  # American Funds Growth Fund of America
    "ANCFX": FundCost(Decimal("0.59"), US_EQUITY),  # American Funds Fundamental Investors
    "FCNTX": FundCost(Decimal("0.39"), US_EQUITY),  # Fidelity Contrafund
    "FMAGX": FundCost(Decimal("0.47"), US_EQUITY),  # Fidelity Magellan
    "DODGX": FundCost(Decimal("0.51"), US_EQUITY),  # Dodge & Cox Stock
    "VFINX": FundCost(Decimal("0.14"), US_EQUITY),  # Vanguard 500 Index (legacy Investor)
    "AIVSX": FundCost(Decimal("0.58"), US_EQUITY),  # American Funds Investment Co. of America
    "AEPGX": FundCost(Decimal("0.82"), INTL_EQUITY),  # American Funds EuroPacific Growth
    "DODFX": FundCost(Decimal("0.62"), INTL_EQUITY),  # Dodge & Cox International Stock
    "PTTAX": FundCost(Decimal("0.80"), BONDS),  # PIMCO Total Return (Class A)
    "ABNDX": FundCost(Decimal("0.60"), BONDS),  # American Funds Bond Fund of America
}


def fund_cost(symbol: str | None) -> FundCost | None:
    """Return the :class:`FundCost` for ``symbol`` (case-insensitive), or ``None``.

    ``None``/blank and any symbol not in :data:`FUND_EXPENSE_RATIOS` have no known
    fee (conservative — never invent a ratio). Symbols are compared upper-cased and
    stripped, mirroring :func:`strategy.index_core.is_index_core`.
    """
    if not symbol:
        return None
    return FUND_EXPENSE_RATIOS.get(symbol.strip().upper())


def fund_expense_ratio(symbol: str | None) -> Decimal | None:
    """Return the real net expense ratio (percentage-point ``Decimal``) for
    ``symbol`` (case-insensitive), or ``None`` when the symbol is unknown/blank
    (never invent a ratio)."""
    cost = fund_cost(symbol)
    return cost.expense_ratio if cost is not None else None
