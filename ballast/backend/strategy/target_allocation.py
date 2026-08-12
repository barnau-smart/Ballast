"""Model-portfolio target-allocation reference (Story 10.1, Epic 10).

The **target** a user's portfolio is measured against: a small set of named
model portfolios (Conservative / Balanced / Growth), each a fixed mix across
three broad **asset classes** — US equity, international equity, bonds — mapped
to canonical broad index funds. This is the "where your money SHOULD be" that
Story 10-2's gap-to-target engine compares holdings + investable cash against.

Twin of :mod:`strategy.index_core`: curated, fixed, deterministic reference data
(a per-user *editable* allocation is a later concern — v1 is named presets, NOT a
questionnaire, NOT age-based, NOT user-editable weights). Weights are ``Decimal``
(never binary float) and every model's class weights sum to exactly
``Decimal("1.00")``.

Diversification is defined by **asset class** on purpose: two flavours of large-US
(e.g. ``SCHB`` and an S&P-500 fund) are the SAME asset class, so they can never
count as diversifying each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# --- Asset-class taxonomy ----------------------------------------------------

#: The three broad asset classes the target is expressed in. Kept deliberately
#: small (extend as a strategy decision, like ``index_core``). Values are stable
#: string keys — JSON/wire-friendly and used as dict keys throughout.
US_EQUITY = "us_equity"
INTL_EQUITY = "intl_equity"
BONDS = "bonds"

ASSET_CLASSES: tuple[str, ...] = (US_EQUITY, INTL_EQUITY, BONDS)

#: Plain-English, jargon-light labels for the classes (surfaced to the user).
ASSET_CLASS_LABEL: dict[str, str] = {
    US_EQUITY: "US stocks",
    INTL_EQUITY: "International stocks",
    BONDS: "Bonds",
}

#: Map each curated index-core symbol (see :data:`strategy.index_core.INDEX_CORE_SYMBOLS`)
#: to its asset class. Case-insensitive lookup via :func:`asset_class_for`.
#:
#: ``VT`` (Vanguard Total World) is a whole-world fund that SPANS US + international
#: and is deliberately OMITTED here — it is a spans-classes special case resolved
#: in Story 10-2 (classifying holdings), not a single-class fund.
SYMBOL_ASSET_CLASS: dict[str, str] = {
    # US equity
    "VTI": US_EQUITY,
    "ITOT": US_EQUITY,
    "SCHB": US_EQUITY,
    "VOO": US_EQUITY,
    "IVV": US_EQUITY,
    "SPY": US_EQUITY,
    "SWPPX": US_EQUITY,
    # International equity
    "VXUS": INTL_EQUITY,
    "IXUS": INTL_EQUITY,
    "VEU": INTL_EQUITY,
    # Bonds
    "BND": BONDS,
    "AGG": BONDS,
    "BNDX": BONDS,
    "SCHZ": BONDS,
}

#: The canonical broad fund to BUY for each asset class (what the deploy-cash
#: engine, Story 10-2, will pre-fill). One well-known, low-cost total-market fund
#: per class.
CANONICAL_FUND: dict[str, str] = {
    US_EQUITY: "VTI",  # Vanguard Total U.S. Stock Market
    INTL_EQUITY: "VXUS",  # Vanguard Total International Stock
    BONDS: "BND",  # Vanguard Total Bond Market
}


# --- Model portfolios --------------------------------------------------------


@dataclass(frozen=True)
class ModelPortfolio:
    """One named target mix. ``weights`` maps every asset class → a ``Decimal``
    weight, and the weights sum to exactly ``Decimal("1.00")`` (enforced by a
    test). Frozen + pure — same key always yields the same target."""

    key: str
    name: str
    description: str
    weights: dict[str, Decimal]


#: The v1 model portfolios (LOCKED weights — extend/tune deliberately, like
#: ``INDEX_CORE_SYMBOLS``). Stock/bond split reads as 40/60, 65/35, 90/10; the
#: international share of equity sits in the standard ~25-33% band.
MODEL_PORTFOLIOS: dict[str, ModelPortfolio] = {
    "conservative": ModelPortfolio(
        key="conservative",
        name="Conservative",
        description=(
            "Mostly bonds for a steadier ride — smaller ups and downs. "
            "Roughly 40% stocks, 60% bonds."
        ),
        weights={
            US_EQUITY: Decimal("0.30"),
            INTL_EQUITY: Decimal("0.10"),
            BONDS: Decimal("0.60"),
        },
    ),
    "balanced": ModelPortfolio(
        key="balanced",
        name="Balanced",
        description=(
            "A middle path: a solid stock base with a real bond cushion. "
            "Roughly 65% stocks, 35% bonds."
        ),
        weights={
            US_EQUITY: Decimal("0.45"),
            INTL_EQUITY: Decimal("0.20"),
            BONDS: Decimal("0.35"),
        },
    ),
    "growth": ModelPortfolio(
        key="growth",
        name="Growth",
        description=(
            "Mostly stocks for long-term growth — expect bigger swings. "
            "Roughly 90% stocks, 10% bonds."
        ),
        weights={
            US_EQUITY: Decimal("0.60"),
            INTL_EQUITY: Decimal("0.30"),
            BONDS: Decimal("0.10"),
        },
    ),
}

#: The valid model keys, in presentation order (also the enum the config validates
#: a stored/selected key against).
MODEL_KEYS: tuple[str, ...] = ("conservative", "balanced", "growth")

#: Plain-English explainer of what a target mix is (NFR6, no unexplained jargon).
TARGET_MIX_RATIONALE = (
    "Your target mix is the balance of broad, low-cost funds you're aiming for — "
    "how much sits in US stocks, international stocks, and bonds. Ballast uses it "
    "to suggest what to buy so your money moves toward that balance."
)


# --- Pure helpers ------------------------------------------------------------


def list_models() -> list[ModelPortfolio]:
    """Return the model portfolios in presentation order (pure)."""
    return [MODEL_PORTFOLIOS[key] for key in MODEL_KEYS]


def get_model(key: str | None) -> ModelPortfolio | None:
    """Return the model for ``key`` (case-insensitive), or ``None`` if unknown/blank."""
    if not key:
        return None
    return MODEL_PORTFOLIOS.get(key.strip().lower())


def is_valid_model(key: str | None) -> bool:
    """True iff ``key`` names a known model portfolio (case-insensitive)."""
    return get_model(key) is not None


def asset_class_for(symbol: str | None) -> str | None:
    """Return the asset class for an index-core ``symbol`` (case-insensitive), or
    ``None`` for a blank/unknown/spans-classes symbol (e.g. ``VT``)."""
    if not symbol:
        return None
    return SYMBOL_ASSET_CLASS.get(symbol.strip().upper())


def resolve_target(key: str | None) -> dict | None:
    """Resolve a model key to the concrete target (Story 10-2's sole contract).

    Returns ``{"model": key, "weights": {class: Decimal}, "funds": {class: symbol}}``
    for a known model, or ``None`` when the key is unknown/blank (an undecided
    user resolves to ``None`` — never a fabricated target).
    """
    model = get_model(key)
    if model is None:
        return None
    return {
        "model": model.key,
        "weights": dict(model.weights),
        "funds": dict(CANONICAL_FUND),
    }
