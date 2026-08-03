"""Payload shape-skeleton capture — redaction + drift substrate in one (Story 7.6).

``to_shape()`` reduces ANY payload to a SHAPE SKELETON:

- a ``dict`` becomes ``{key: to_shape(value)}`` (keys survive, values recurse),
- a ``list`` becomes ``{"type": "array", "len": N, "item": <shape of elem 0>}``
  (or ``item: None`` when empty),
- a scalar becomes its type NAME string (``"str"``, ``"int"``, ``"float"``,
  ``"bool"``, ``"NoneType"``, ...),
- an arbitrary/SDK object (e.g. the Anthropic ``Message``) is shaped via its
  PUBLIC attributes (``vars()``/``__dict__``), so its structure is preserved
  without any leaf value.

This is the redaction mechanism: because every leaf is replaced by its TYPE
name (and every array by its length), NO token / account number / hash / PII
leaf value can EVER appear in a captured skeleton.

The :class:`PayloadCapture` sink writes ``<seam>.json`` skeletons only when
``PREFLIGHT_CAPTURE_DIR`` is set; when it is empty the sink is a true no-op. The
module-level :func:`capture_enabled` / :func:`capture` helpers let the adapter
taps check the enabled-state and write WITHOUT doing any work when capture is
OFF (they early-return before reducing/serializing).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# The scalar/leaf types whose VALUE we must never let survive — each is reduced
# to its type name. Anything not a dict / list / tuple falls here too (its
# ``type(...).__name__`` is emitted, never the value).
_SCALAR_TYPES = (str, int, float, bool, bytes)


def to_shape(payload: Any) -> Any:
    """Reduce ``payload`` to a redacted SHAPE SKELETON (no leaf value survives).

    - ``dict`` -> ``{key: to_shape(value)}``
    - ``list``/``tuple`` -> ``{"type": "array", "len": N, "item": <shape of [0]>}``
      (``item`` is ``None`` for an empty sequence)
    - ``None`` -> ``"NoneType"``
    - a scalar -> its type name (``"str"``, ``"int"``, ...)
    - any other object (SDK/``Message``) -> ``to_shape`` of its public
      ``__dict__`` attributes (so SDK objects are shaped by their attributes,
      never by their values)
    """
    if payload is None:
        return "NoneType"
    if isinstance(payload, dict):
        return {str(key): to_shape(value) for key, value in payload.items()}
    if isinstance(payload, (list, tuple)):
        length = len(payload)
        return {
            "type": "array",
            "len": length,
            "item": to_shape(payload[0]) if length else None,
        }
    # ``bool`` is a subclass of ``int`` — the isinstance below catches it and
    # emits "bool" (its own type name), which is what drift expects.
    if isinstance(payload, _SCALAR_TYPES):
        return type(payload).__name__
    # An arbitrary / SDK object (e.g. the Anthropic ``Message``): shape it by its
    # PUBLIC attributes so structure survives but no leaf value does. Private
    # (``_``-prefixed) and callable attributes are skipped.
    attrs = getattr(payload, "__dict__", None)
    if attrs:
        return {
            str(name): to_shape(value)
            for name, value in vars(payload).items()
            if not name.startswith("_")
        }
    # No introspectable attributes — fall back to the bare type name (still no
    # value survives).
    return type(payload).__name__


class PayloadCapture:
    """A capture sink constructed from settings.

    When ``PREFLIGHT_CAPTURE_DIR`` is empty the sink is a NO-OP: :meth:`capture`
    writes nothing and does nothing observable. When it is set, :meth:`capture`
    ensures the directory exists and writes ``<seam>.json`` holding the
    :func:`to_shape` skeleton of the payload.
    """

    def __init__(self, capture_dir: str = "") -> None:
        # Empty string (the default) = OFF. Stored as ``str`` so ``enabled`` is a
        # cheap truthiness check with no work done at construction.
        self._capture_dir = capture_dir or ""

    @property
    def enabled(self) -> bool:
        """True only when a non-empty capture directory is configured."""
        return bool(self._capture_dir)

    @property
    def capture_dir(self) -> str:
        return self._capture_dir

    def capture(self, seam: str, payload: Any) -> Path | None:
        """Write the redacted skeleton for ``seam``; no-op (returns None) when OFF.

        Returns the written path when capture is enabled so the orchestrator can
        collect them; returns ``None`` (and does nothing) when OFF.
        """
        if not self.enabled:
            return None
        directory = Path(self._capture_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{seam}.json"
        skeleton = to_shape(payload)
        path.write_text(json.dumps(skeleton, indent=2, sort_keys=True))
        return path


def capture_enabled(settings: Any) -> bool:
    """Cheap enabled-state check for a tap: True iff ``PREFLIGHT_CAPTURE_DIR`` set.

    Does NO reduction and NO I/O — a tap calls this FIRST and only reduces +
    writes when it returns True, so a capture-OFF tap is a true no-op.
    """
    return bool(getattr(settings, "PREFLIGHT_CAPTURE_DIR", "") or "")


def capture(settings: Any, seam: str, payload: Any) -> Path | None:
    """Reduce + write ``payload`` for ``seam`` — early-returns when capture OFF.

    The helper the adapter taps call. It checks :func:`capture_enabled` FIRST and
    returns immediately (no ``to_shape``, no file) when OFF, so a tap adds zero
    overhead and leaves adapter behavior byte-for-byte unchanged in the default
    (OFF) configuration.
    """
    if not capture_enabled(settings):
        return None
    return PayloadCapture(settings.PREFLIGHT_CAPTURE_DIR).capture(seam, payload)
