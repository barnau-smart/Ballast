"""The Email Port — the sole outbound boundary for the weekly digest (AD-8).

Story 5.1 (FR21): the digest is EMAIL ONLY — never push, never SMS. Every send
in the ``digest`` module flows through this one port, so the channel is a single,
auditable seam. The concrete transport (a credential-free fake for dev/test vs a
real SMTP sender) is chosen by config (``EMAIL_ADAPTER``) and swapped without
touching a single caller, mirroring :mod:`llm` and :mod:`marketdata`.

This module deliberately imports NOTHING that could send anywhere: no ``smtplib``
here (the real adapter imports it lazily), and no push/SMS/telephony library
anywhere in ``digest`` — a structural test enforces the email-only invariant.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class EmailNotConfiguredError(RuntimeError):
    """Raised when the real email adapter is used without required credentials.

    A configuration error (fail-loud), deliberately distinct from an import
    failure or a network error. The fake adapter (the default, tested path)
    never raises this.
    """


@dataclass(frozen=True)
class EmailMessage:
    """One outbound email — a plain value, no transport concern.

    ``to`` is the recipient address, ``subject`` the line, and the body is
    carried in BOTH ``text_body`` (always present — the calm plain-text digest)
    and ``html_body`` (a minimal styled rendering). Frozen so a composed message
    is an immutable value a test can assert on byte-for-byte.

    ``list_unsubscribe_url`` (optional) is the one-click opt-out endpoint. When
    set, a real transport SHOULD emit the RFC 8058 ``List-Unsubscribe`` /
    ``List-Unsubscribe-Post`` headers so compliant mail clients render a native
    one-click unsubscribe that POSTs (avoiding accidental opt-outs from link
    scanners that pre-fetch GET links) and so bulk-sender rules are satisfied.
    """

    to: str
    subject: str
    text_body: str
    html_body: str
    list_unsubscribe_url: str | None = None


class EmailPort(ABC):
    """The abstract email boundary — the only sender type callers depend on.

    Implementations: :class:`~digest.fake_adapter.FakeEmailAdapter` (dev / test,
    records messages in memory, zero credentials & zero network — the DEFAULT and
    tested path) and :class:`~digest.smtp_adapter.SmtpEmailAdapter` (real SMTP,
    credential-gated). Swapping is a config change (``EMAIL_ADAPTER``), not a code
    change — callers touch only this port.
    """

    #: Identifies the concrete adapter; set on each subclass.
    provider: str

    @abstractmethod
    def send(self, message: EmailMessage) -> None:
        """Deliver one :class:`EmailMessage`. Synchronous.

        Implementations MUST NOT log credentials or the message body. A transport
        failure should raise so the batch job can isolate that recipient and
        continue with the rest (it will retry next run).
        """
        raise NotImplementedError
