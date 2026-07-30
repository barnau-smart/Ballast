"""Email sender factory (AD-8).

Selects the concrete :class:`~digest.email_port.EmailPort` implementation from
config (``EMAIL_ADAPTER``) and returns it typed as the port. Callers (the digest
job) depend on the port only; swapping fake <-> smtp is a config change, not a
code change.

- ``EMAIL_ADAPTER=fake`` (default): :class:`~digest.fake_adapter.FakeEmailAdapter`
  — no creds, no network, never imports ``smtplib``.
- ``EMAIL_ADAPTER=smtp``: :class:`~digest.smtp_adapter.SmtpEmailAdapter` — raises
  a clear :class:`~digest.email_port.EmailNotConfiguredError` if the SMTP config
  is absent.
"""

from __future__ import annotations

from api.config import get_settings
from digest.email_port import EmailPort
from digest.fake_adapter import FakeEmailAdapter


class UnknownEmailAdapterError(RuntimeError):
    """Raised when ``EMAIL_ADAPTER`` names an adapter that does not exist."""


def get_email_sender() -> EmailPort:
    """Return the configured email sender as an :class:`EmailPort`.

    The ``smtp`` adapter is imported lazily (only when selected), so the default
    fake path never touches the transport module.
    """
    adapter = (get_settings().EMAIL_ADAPTER or "fake").strip().lower()

    if adapter == "fake":
        return FakeEmailAdapter()

    if adapter == "smtp":
        # Import lazily so selecting fake never loads the smtp adapter module.
        from digest.smtp_adapter import SmtpEmailAdapter

        return SmtpEmailAdapter()

    raise UnknownEmailAdapterError(
        f"Unknown EMAIL_ADAPTER '{adapter}'. Expected 'fake' or 'smtp'."
    )
