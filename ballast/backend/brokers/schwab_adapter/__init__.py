"""Schwab adapter package (AD-8).

This is the ONLY place in the codebase that imports ``schwab-py``. The import is
lazy (inside methods) so that importing this package NEVER crashes when the SDK
is present but credentials are absent, and so callers depend only on
:class:`~brokers.port.BrokerPort`.
"""

from __future__ import annotations

from brokers.schwab_adapter.adapter import (
    SchwabAdapter,
    SchwabNotConfiguredError,
    SchwabReadError,
)

__all__ = ["SchwabAdapter", "SchwabNotConfiguredError", "SchwabReadError"]
