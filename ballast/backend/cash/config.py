"""Read/write helpers for the per-user cash configuration (Story 9.1, AD-10).

The request-path helpers (:func:`get_or_create_config`, :func:`set_reserve`,
:func:`set_parked_symbols`) go through the fail-closed
:class:`~db.repository.ScopedRepository` under the caller's user scope — a user
can only ever read or change their OWN config. Money is ``Decimal`` end-to-end
(``Numeric(20, 2)`` in the DB), never binary float.

The reserve is honest-by-construction (AC2): two columns disambiguate three
states — never-decided / declined / set — and :func:`resolve_reserve` is the ONE
place the "resolved" reserve is derived (the amount if set, ``0`` if declined,
``None`` if never-decided) so display and every later calc agree.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import CashConfig
from db.repository import ScopedRepository
from db.scope import Scope

logger = logging.getLogger("ballast.cash.config")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_symbols(symbols: list[str]) -> list[str]:
    """Trim, upper-case, and de-duplicate a list of symbols (order-stable).

    Blank/whitespace-only entries are dropped. De-duplication keeps first
    occurrence so the stored order is stable and predictable (deterministic
    round-trips, AD-1-friendly). Non-string entries are coerced via ``str``
    defensively (the API layer types them as ``list[str]``).
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in symbols or []:
        sym = str(raw).strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def parked_market_value(holdings, config: CashConfig | None) -> Decimal:
    """Return Σ ``market_value`` of the holdings the user has parked (money-market).

    The SINGLE source of the parked-sum rule so the read-path can't drift between
    endpoints (``GET /api/portfolio`` and the missed-growth read both call this).
    A holding is parked when its normalized symbol is in ``config.parked_symbols``
    (reuse :func:`normalize_symbols` so the compare rule matches how symbols were
    stored). ``config is None`` (a user who never set one — the calm never-decided
    default) or no parked symbols → ``Decimal("0")``. Never writes; derives purely
    from the passed-in holdings + config (AD-14: parked is derived at read time,
    never stored on the pure broker projection).
    """
    if config is None:
        return Decimal("0")
    parked_set = set(normalize_symbols(config.parked_symbols))
    if not parked_set:
        return Decimal("0")
    return sum(
        (
            h.market_value
            for h in holdings
            if h.symbol
            and any(sym in parked_set for sym in normalize_symbols([h.symbol]))
            # Skip an unpriced / degenerate holding (mirrors the liquidation filter
            # ``_largest_parked_holding``): a ``None``/non-finite/≤0 ``market_value``
            # can't be sensibly summed — without this guard a single ``None``-valued
            # parked holding raises a ``TypeError`` inside ``sum`` and 500s every
            # caller (``GET /api/portfolio``, missed-growth, the deploy engine).
            and h.market_value is not None
            and h.market_value.is_finite()
            and h.market_value > 0
        ),
        Decimal("0"),
    )


def resolve_reserve(config: CashConfig) -> Decimal | None:
    """Return the RESOLVED reserve for calculation/display (the honesty crux).

    - never-decided (``reserve_decided is False``) → ``None`` (absent; NEVER 0).
    - declined (decided, ``reserve_amount is None``) → ``Decimal("0")``.
    - set (decided, amount present) → that amount (``>= 0``).
    """
    if not config.reserve_decided:
        return None
    if config.reserve_amount is None:
        return Decimal("0")
    return config.reserve_amount


# Largest magnitude representable by ``Numeric(20, 2)`` — 18 integer digits + 2
# fractional. A value at/above this overflows the column and would surface as a
# raw 500 at commit; we reject it up front as a calm 422 instead.
_RESERVE_MAX = Decimal("10") ** 18


def _validate_reserve(amount: Decimal | None) -> Decimal | None:
    """Return a valid reserve amount or raise ``ValueError`` (→ calm 422).

    Rejects the inputs that would otherwise corrupt the wire contract or blow up
    at commit: non-finite (``NaN``/``Infinity`` — which slip past a naive ``< 0``
    check), negative, out of ``Numeric(20, 2)`` range, or with more than two
    decimal places (silent rounding is dishonest for a money field). ``None``
    (declined / never-decided) passes through.
    """
    if amount is None:
        return None
    if not amount.is_finite():
        raise ValueError("Reserve amount must be a finite number.")
    if amount < 0:
        raise ValueError("Reserve amount cannot be negative.")
    if amount >= _RESERVE_MAX:
        raise ValueError("Reserve amount is too large.")
    if amount.as_tuple().exponent < -2:
        raise ValueError("Reserve amount cannot have more than two decimal places.")
    # Canonicalize to the DB scale (2 dp) so the stored + serialized value is
    # deterministic — e.g. "0" → "0.00", "1500" → "1500.00" — independent of when
    # SQLAlchemy refreshes the attribute after commit. Precision is only PADDED
    # here (over-precision was rejected above), never rounded away.
    return amount.quantize(Decimal("0.01"))


async def get_config(scope: Scope, session: AsyncSession) -> CashConfig | None:
    """Return the caller's cash config if it exists, else ``None`` — READ-ONLY.

    Unlike :func:`get_or_create_config`, this NEVER writes: the read-class
    ``GET /api/portfolio`` uses it so a plain read can't INSERT a row (or race on
    ``uq_cash_config_owner``), and treats an absent config as the calm
    never-decided default. Fail-closed per-user via the scoped repo.
    """
    repo = ScopedRepository(CashConfig, scope, session)
    rows = await repo.list()
    return rows[0] if rows else None


async def get_or_create_config(
    scope: Scope, session: AsyncSession
) -> CashConfig:
    """Return the caller's cash config, creating a calm default one if absent.

    Fail-closed per-user: reads/writes only THIS user's row via the scoped repo.
    A freshly created row reads as the calm default — reserve *never-decided*
    (``reserve_decided=False``, ``reserve_amount=None``) and no parked symbols.
    Commits when it creates a row so the default persists. Handles the
    first-request race (SPA firing GET on mount + PUT on save) the same way the
    digest preference does: on ``IntegrityError`` roll back and re-read.
    """
    repo = ScopedRepository(CashConfig, scope, session)
    rows = await repo.list()
    if rows:
        return rows[0]

    now = _now()
    try:
        config = await repo.add(
            reserve_amount=None,
            reserve_decided=False,
            parked_symbols=[],
            created_at=now,
            updated_at=now,
        )
        await session.commit()
        return config
    except IntegrityError:
        # A concurrent first-time request lost the race on
        # UniqueConstraint(owner_id). Not an error for the caller — the row now
        # exists; roll back and read it.
        await session.rollback()
        rows = await repo.list()
        if rows:
            return rows[0]
        raise


async def set_config(
    scope: Scope,
    session: AsyncSession,
    *,
    amount: Decimal | None,
    decided: bool,
    symbols: list[str],
) -> CashConfig:
    """Set the caller's reserve + parked symbols in ONE atomic commit — fail-closed.

    The whole PUT is a single write (the reviewers flagged a two-commit split that
    could half-apply): validate, load-or-create once, set all three fields, commit
    once.

    Reserve semantics: ``amount=None`` with ``decided=True`` is a decline
    (resolves to 0); ``amount>=0`` is an explicit set (``0`` is legitimate).
    **Coherence guard:** an explicit amount is ALWAYS an explicit decision, so
    ``reserve_decided`` is forced ``True`` whenever ``amount`` is provided — a
    stored amount can never masquerade as "never-decided" (which
    :func:`resolve_reserve` would otherwise report as ``None``). A parked-only
    write (``amount=None``, ``decided=False``) legitimately leaves the reserve
    never-decided. Invalid amounts raise ``ValueError`` (the API → calm 422).
    Symbols are normalized (trim / upper-case / de-dupe); an unheld symbol is
    stored but simply matches nothing at read time.
    """
    amount = _validate_reserve(amount)

    config = await get_or_create_config(scope, session)
    config.reserve_amount = amount
    config.reserve_decided = True if amount is not None else bool(decided)
    config.parked_symbols = normalize_symbols(symbols)
    config.updated_at = _now()
    await session.commit()
    return config
