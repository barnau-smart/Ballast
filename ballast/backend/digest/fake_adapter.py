"""FakeEmailAdapter — the credential-free implementation of :class:`EmailPort`.

This is the DEFAULT adapter (``EMAIL_ADAPTER=fake``). It makes the entire digest
path runnable and testable locally with ZERO credentials and ZERO network calls:
instead of talking to an SMTP server it appends each message to an in-memory
``sent`` list a test can inspect. It never imports ``smtplib`` (or any push/SMS
library) — the email-only, no-network invariant.
"""

from __future__ import annotations

from digest.email_port import EmailMessage, EmailPort


class FakeEmailAdapter(EmailPort):
    """An offline stand-in for a real email transport. Records, never sends.

    Each :meth:`send` appends to :attr:`sent`. Construct a fresh instance per run
    (the job / tests do) so the list is a clean record of that run's emails.
    """

    provider = "fake"

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> None:
        """Record the message in memory. No network, no credentials."""
        self.sent.append(message)
