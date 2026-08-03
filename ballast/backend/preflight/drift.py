"""Per-seam drift comparison over redacted shape skeletons (Story 7.6).

Each of the five read-reachable money-path seams declares the EXACT field paths
our production mappers read (verified against the adapter source — see the
spec's Design Notes -> Field Map). :func:`compare` walks a captured shape
skeleton (produced by :func:`preflight.capture.to_shape`) against a seam's
declared fields and emits one :class:`FieldResult` per field:

- ``OK``               — the key is present at the expected path with the
                          expected type (a one-of field is OK if EITHER
                          alternative is present with the right type).
- ``MISSING``          — absent, with no matching sibling of the expected type.
- ``RENAMED-CANDIDATE``— absent, but a SIBLING key of the expected type exists at
                          the same nesting level (the candidate name is carried
                          on the result as a hint).
- ``TYPE-MISMATCH``    — present, but the wrong type.

The sixth seam (order-status / fill, ``_map_order``) needs a PLACED order and is
therefore unreachable by this read-only harness: :func:`order_status_out_of_scope_line`
returns the explicit "not confirmed here -> Story 7.7" line the report must
carry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- Seam identifiers (also the capture ``<seam>.json`` filenames) ------------

SEAM_TOKEN = "token"
SEAM_ACCOUNT = "account_numbers"
SEAM_PORTFOLIO = "portfolio"
SEAM_QUOTE = "quote"
SEAM_LLM_MESSAGE = "llm_message"
SEAM_LLM_OUTPUT = "llm_output"

#: The out-of-scope order-status/fill seam — declared, never confirmed here.
SEAM_ORDER_STATUS = "order_status"

# --- Verdicts -----------------------------------------------------------------

OK = "OK"
MISSING = "MISSING"
RENAMED_CANDIDATE = "RENAMED-CANDIDATE"
TYPE_MISMATCH = "TYPE-MISMATCH"

PASS = "PASS"
DRIFT = "DRIFT"
#: A declared seam that produced NO capture this run (never exercised, or its
#: drive raised). It is NOT a PASS — a real-money gate must never read a skipped
#: seam as confirmed.
INCOMPLETE = "INCOMPLETE"

#: In the shape skeleton every leaf is a type-name string. These are the type
#: names we treat as NUMERIC for a field declared ``number`` (JSON numbers can
#: arrive as int or float; a broker may send a numeric-looking string, which is
#: TYPE-MISMATCH — we do NOT accept ``str`` as numeric).
_NUMERIC_TYPE_NAMES = frozenset({"int", "float"})
#: The array marker to_shape emits for a list value.
_ARRAY_TYPE = "array"


@dataclass(frozen=True)
class ExpectedField:
    """One declared field: a dotted path + the expected type category.

    ``path`` is a tuple of keys from the skeleton root; ``"[]"`` denotes "descend
    into the array's ``item`` shape" (the per-element shape ``to_shape`` keeps).
    ``expected`` is one of ``"str"``, ``"number"``, ``"array"``. ``one_of`` lists
    ALTERNATIVE sibling keys any one of which (present with the expected type at
    the parent path) satisfies the field (e.g. the token expiry
    ``expires_at`` | ``expires_in``).
    """

    path: tuple[str, ...]
    expected: str
    one_of: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """Human-readable dotted path for the report/results."""
        if self.one_of:
            prefix = ".".join(self.path[:-1])
            alts = " | ".join(self.one_of)
            return f"{prefix}.{alts}" if prefix else alts
        return ".".join(self.path)


@dataclass(frozen=True)
class FieldResult:
    """The verdict for one declared field at one seam."""

    seam: str
    field_label: str
    verdict: str
    candidate: str | None = None


# --- Declared field maps (verified against adapter source) --------------------
#
# Seam 1 (token, ``_to_broker_tokens``): access_token/refresh_token (str),
#   one-of expires_at | expires_in (number).
# Seam 2 (account numbers, ``_account_hash``): array of {accountNumber, hashValue}.
# Seam 3 (portfolio, ``fetch_portfolio`` -> securitiesAccount): cashBalance
#   (number) + positions[].instrument.symbol (str), positions[].longQuantity
#   (number), positions[].marketValue (number).
# Seam 4 (quote, ``_quote_ask``): [symbol].quote.askPrice (number).
# Seam 5 (Anthropic): message stop_reason (str) + content[].type (str),
#   content[].text (str); parsed output requires action_label, reasoning (str),
#   evidence, uncertainties (array) per RECOMMENDATION_OUTPUT_SCHEMA.

#: Symbol placeholder key for the quote seam (drift resolves ``[symbol]`` against
#: the single top-level key present in the captured quote skeleton).
SYMBOL_PLACEHOLDER = "[symbol]"

_FIELD_MAPS: dict[str, tuple[ExpectedField, ...]] = {
    SEAM_TOKEN: (
        ExpectedField(("access_token",), "str"),
        ExpectedField(("refresh_token",), "str"),
        # One-of expiry: absolute ``expires_at`` OR relative ``expires_in``.
        ExpectedField(("expires_at",), "number", one_of=("expires_at", "expires_in")),
    ),
    SEAM_ACCOUNT: (
        ExpectedField(("[]", "accountNumber"), "str_or_number"),
        ExpectedField(("[]", "hashValue"), "str"),
    ),
    SEAM_PORTFOLIO: (
        ExpectedField(
            ("securitiesAccount", "currentBalances", "cashBalance"), "number"
        ),
        ExpectedField(
            ("securitiesAccount", "positions", "[]", "instrument", "symbol"), "str"
        ),
        ExpectedField(
            ("securitiesAccount", "positions", "[]", "longQuantity"), "number"
        ),
        ExpectedField(
            ("securitiesAccount", "positions", "[]", "marketValue"), "number"
        ),
    ),
    SEAM_QUOTE: (
        ExpectedField((SYMBOL_PLACEHOLDER, "quote", "askPrice"), "number"),
    ),
    SEAM_LLM_MESSAGE: (
        ExpectedField(("stop_reason",), "str"),
        ExpectedField(("content", "[]", "type"), "str"),
        ExpectedField(("content", "[]", "text"), "str"),
    ),
    SEAM_LLM_OUTPUT: (
        ExpectedField(("action_label",), "str"),
        ExpectedField(("reasoning",), "str"),
        ExpectedField(("evidence",), "array"),
        ExpectedField(("uncertainties",), "array"),
    ),
}


def declared_seams() -> tuple[str, ...]:
    """The seams drift knows how to compare (order = the harness run order)."""
    return (
        SEAM_TOKEN,
        SEAM_ACCOUNT,
        SEAM_PORTFOLIO,
        SEAM_QUOTE,
        SEAM_LLM_MESSAGE,
        SEAM_LLM_OUTPUT,
    )


def _type_matches(expected: str, actual_type_name: str) -> bool:
    """True if a leaf type-name string satisfies the expected type category."""
    if expected == "number":
        return actual_type_name in _NUMERIC_TYPE_NAMES
    if expected == "str_or_number":
        return actual_type_name == "str" or actual_type_name in _NUMERIC_TYPE_NAMES
    if expected == "array":
        return actual_type_name == _ARRAY_TYPE
    # A concrete type name (e.g. "str"): the skeleton must carry exactly it.
    return actual_type_name == expected


def _leaf_type_name(node) -> str | None:
    """The type-name string a skeleton node represents, or None if it's a subtree.

    A scalar leaf is a bare type-name string. An array node is the dict
    ``{"type": "array", ...}`` -> its type name is ``"array"``. A plain dict
    (a nested object) is a subtree, not a leaf -> ``None``.
    """
    if isinstance(node, str):
        return node
    if isinstance(node, dict) and node.get("type") == _ARRAY_TYPE:
        return _ARRAY_TYPE
    return None


def _descend(shape, path: tuple[str, ...]):
    """Walk ``shape`` down ``path`` (``"[]"`` -> array ``item``).

    Returns a tuple ``(parent, key, node)`` where ``node`` is the shape at the
    final path step (or ``None`` if absent), ``parent`` is the dict the final key
    lives in (for sibling inspection, or ``None`` if unreachable), and ``key`` is
    the final key. Intermediate ``"[]"`` steps descend into the array item shape;
    a missing intermediate node short-circuits to ``(None, key, None)``.
    """
    node = shape
    parent = None
    key = path[-1] if path else None
    for i, step in enumerate(path):
        is_last = i == len(path) - 1
        if step == "[]":
            # Descend into the per-element shape kept under ``item``.
            if not (isinstance(node, dict) and node.get("type") == _ARRAY_TYPE):
                return None, key, None
            node = node.get("item")
            if node is None:
                return None, key, None
            continue
        if not isinstance(node, dict):
            return None, key, None
        if is_last:
            parent = node
            return parent, key, node.get(step)
        node = node.get(step)
        if node is None:
            return None, key, None
    return parent, key, node


def _resolve_symbol_placeholder(shape, path: tuple[str, ...]) -> tuple[str, ...]:
    """Replace a leading ``[symbol]`` step with the sole top-level key present.

    The quote skeleton is ``{<SYMBOL>: {"quote": {...}}}`` — the symbol key is
    dynamic, so drift resolves it to whatever single top-level key the captured
    skeleton carries (if exactly one). Falls back to the literal placeholder
    (which will read as MISSING) when the shape is empty / ambiguous.
    """
    if not path or path[0] != SYMBOL_PLACEHOLDER:
        return path
    if isinstance(shape, dict):
        keys = list(shape.keys())
        if len(keys) == 1:
            return (keys[0],) + path[1:]
    return path


def _find_sibling_of_type(parent, expected: str, missing_key: str) -> str | None:
    """A sibling key (!= missing_key) whose leaf type matches ``expected``, or None."""
    if not isinstance(parent, dict):
        return None
    for name, node in parent.items():
        if name == missing_key:
            continue
        type_name = _leaf_type_name(node)
        if type_name is not None and _type_matches(expected, type_name):
            return name
    return None


def _compare_field(seam: str, shape, ef: ExpectedField) -> FieldResult:
    path = _resolve_symbol_placeholder(shape, ef.path)

    # One-of: any listed alternative present (with the expected type) at the
    # parent path satisfies the field.
    if ef.one_of:
        base = path[:-1]
        # Resolve the parent object the alternatives live under.
        parent_obj = _resolve_parent(shape, base)
        for alt in ef.one_of:
            if isinstance(parent_obj, dict) and alt in parent_obj:
                type_name = _leaf_type_name(parent_obj[alt])
                if type_name is not None and _type_matches(ef.expected, type_name):
                    return FieldResult(seam, ef.label, OK)
        # None of the alternatives present-and-right-typed -> MISSING (a one-of
        # with a wrong-typed member is still MISSING; the field is unsatisfied).
        candidate = None
        if isinstance(parent_obj, dict):
            for alt in ef.one_of:
                candidate = _find_sibling_of_type(parent_obj, ef.expected, alt)
                if candidate is not None:
                    break
        if candidate is not None:
            return FieldResult(seam, ef.label, RENAMED_CANDIDATE, candidate)
        return FieldResult(seam, ef.label, MISSING)

    parent, key, node = _descend(shape, path)
    if node is None:
        # Absent — look for a same-level sibling of the expected type (a rename).
        candidate = _find_sibling_of_type(parent, ef.expected, key)
        if candidate is not None:
            return FieldResult(seam, ef.label, RENAMED_CANDIDATE, candidate)
        return FieldResult(seam, ef.label, MISSING)
    type_name = _leaf_type_name(node)
    if type_name is None:
        # The node is a nested object where a leaf was expected — a shape/type
        # mismatch.
        return FieldResult(seam, ef.label, TYPE_MISMATCH)
    if _type_matches(ef.expected, type_name):
        return FieldResult(seam, ef.label, OK)
    return FieldResult(seam, ef.label, TYPE_MISMATCH)


def _resolve_parent(shape, base: tuple[str, ...]):
    """Return the dict object at ``base`` (the parent for one-of/sibling checks)."""
    if not base:
        return shape
    node = shape
    for step in base:
        if step == "[]":
            if not (isinstance(node, dict) and node.get("type") == _ARRAY_TYPE):
                return None
            node = node.get("item")
            continue
        if not isinstance(node, dict):
            return None
        node = node.get(step)
    return node


def compare(seam: str, shape) -> list[FieldResult]:
    """Compare a captured shape skeleton for ``seam`` against its declared fields."""
    fields = _FIELD_MAPS.get(seam)
    if fields is None:
        raise ValueError(f"Unknown seam: {seam!r}")
    return [_compare_field(seam, shape, ef) for ef in fields]


def overall_verdict(results: list[FieldResult]) -> str:
    """PASS iff EVERY field is OK (one-of counts as OK); else DRIFT."""
    return PASS if all(r.verdict == OK for r in results) else DRIFT


def order_status_out_of_scope_line() -> str:
    """The explicit out-of-scope line the report must carry for ``_map_order``.

    The order-status / fill seam (``_map_order``: ``status``, ``filledQuantity``,
    ``quantity``, ``avgFillPrice``) needs a PLACED order, so it is unreachable by
    this read-only harness.
    """
    return (
        "Seam 6 order-status/fill (_map_order: status, filledQuantity, "
        "quantity, avgFillPrice) is NOT confirmed by this read-only harness; "
        "deferred to Story 7.7."
    )


def token_reconstructed_caveat_line() -> str:
    """Honesty caveat for the token seam when the orchestrator drives it.

    The orchestrator drives the token seam via ``_to_broker_tokens`` on the
    Ballast-RECONSTRUCTED token dict (``factory._token_dict_from_broker_tokens``),
    not Schwab's raw OAuth token-endpoint payload — so a ``token`` PASS here
    confirms only that OUR reconstruction matches OUR mapper, not the live
    Schwab shape. The raw Schwab token shape is captured only at OAuth-LINK time
    (the callback taps ``_to_broker_tokens`` with schwab-py's raw token) — run the
    link before trusting the token seam. A true read-only token drift check at the
    OAuth-exchange boundary is deferred (see the deferred-work ledger).
    """
    return (
        "Seam 1 token: driven from the Ballast-reconstructed token dict, NOT "
        "Schwab's raw OAuth payload — a PASS confirms only our reconstruction "
        "matches our mapper. The raw Schwab token shape is captured at OAuth-link "
        "time; a read-only exchange-boundary token drift check is deferred."
    )
