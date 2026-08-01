"""The single source of truth for serializing money/``Decimal`` onto the wire.

Every monetary/``Decimal`` value that crosses an HTTP boundary MUST render as
fixed-point via :func:`format_money` — never binary float and never exponent
notation (``"1E+27"`` / ``"1E-8"``). ``format(Decimal, "f")`` renders in plain
fixed-point, so an extreme amount serializes as e.g.
``"1000000000000000000000000000"`` (not the lossy-looking ``str(Decimal)`` form
``"1E+27"``) and a tiny fraction as ``"0.00000001"`` (not ``"1E-8"``). Decimal
scale is preserved (``Decimal("100.00")`` → ``"100.00"``), and every emitted
string round-trips cleanly through the documented consumer:
``Decimal(format_money(x)) == x`` for every finite ``x`` (money is always
finite; a non-finite ``Decimal`` renders ``"NaN"``/``"Infinity"`` verbatim and
must be rejected/sanitized upstream — Pydantic ``WireMoney`` fields reject it at
validation, and non-finite is not reachable at the engine-computed call sites).

This module is stdlib + Pydantic only (no project imports) so it can be the
shared home without introducing import cycles. The pre-existing ad-hoc helpers
(``coach.decision_record._money``, ``api.coach._money_str``) delegate here so the
persisted co-sign snapshot and the live ``/approve`` outcome stay byte-identical.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import PlainSerializer


def format_money(value: Decimal) -> str:
    """Return a fixed-point decimal STRING — never binary float, never ``E+``/``E-``.

    ``format(Decimal, "f")`` renders in plain fixed-point notation, preserving
    scale, so ``Decimal("1E29")/Decimal("100")`` → ``"1000000000000000000000000000"``
    and ``Decimal("1E-8")`` → ``"0.00000001"``. The result round-trips cleanly:
    ``Decimal(format_money(x)) == x`` for every finite ``x`` (see module docstring
    on non-finite handling).
    """
    return format(value, "f")


#: Reusable Pydantic annotated type for ``Decimal``-typed response fields. It
#: serializes a ``Decimal`` as fixed-point via :func:`format_money` on JSON output
#: (leaving the in-model value a ``Decimal`` for internal use) and passes ``None``
#: through unchanged, so ``Decimal | None`` optional money fields stay ``null`` on
#: the wire. Apply as ``cash: WireMoney`` / ``cost_basis: WireMoney | None = None``.
WireMoney = Annotated[
    Decimal,
    PlainSerializer(
        lambda v: None if v is None else format_money(v),
        return_type=str,
        when_used="json",
    ),
]
