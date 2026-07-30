"""SmtpEmailAdapter — the real :class:`EmailPort` implementation via SMTP.

Code-shaped but CREDENTIAL-GATED (the fake-first strategy this codebase uses for
every external edge), mirroring :class:`~llm.anthropic_adapter.AnthropicGateway`:

- Importing this module NEVER imports ``smtplib`` and NEVER crashes, even with no
  credentials. ``smtplib``/``email.message`` are imported LAZILY inside
  :meth:`send`.
- Constructing/using the adapter without ``SMTP_HOST`` or ``DIGEST_FROM_ADDRESS``
  raises a clear :class:`~digest.email_port.EmailNotConfiguredError` — a
  configuration error, not an import crash and not a network error. The config is
  checked BEFORE ``smtplib`` is imported.
- Real network calls happen ONLY when the adapter is properly configured (i.e.
  never in tests / the default fake path).
- The SMTP password and the message body are NEVER logged.

Uses only the Python standard library — no heavy email dependency is added.
"""

from __future__ import annotations

import logging

from api.config import get_settings
from digest.email_port import EmailMessage, EmailNotConfiguredError, EmailPort

logger = logging.getLogger("ballast.digest.smtp")


class SmtpEmailAdapter(EmailPort):
    """Real email sender backed by an SMTP server. Gated on credentials."""

    provider = "smtp"

    def __init__(self) -> None:
        # Read config via Settings (pydantic-settings, .env-aware) — the same
        # source the rest of the app uses. Fail loudly at construction if the
        # required values are missing, so the factory's gating is unambiguous.
        # ``smtplib`` is NOT imported here.
        settings = get_settings()
        self._host = (settings.SMTP_HOST or "").strip()
        self._port = settings.SMTP_PORT
        self._username = settings.SMTP_USERNAME or ""
        self._password = settings.SMTP_PASSWORD or ""
        self._from_address = (settings.DIGEST_FROM_ADDRESS or "").strip()
        self._require_configured()

    def _require_configured(self) -> None:
        if not self._host or not self._from_address:
            raise EmailNotConfiguredError(
                "SmtpEmailAdapter requires SMTP_HOST and DIGEST_FROM_ADDRESS; "
                "set them (and SMTP credentials if your server needs auth), or "
                "use EMAIL_ADAPTER=fake for local/dev."
            )

    def send(self, message: EmailMessage) -> None:
        """Deliver one email via SMTP. Imports ``smtplib`` lazily.

        Raises on transport failure so the batch job can isolate this recipient
        and continue. Never logs the password or the message body.
        """
        self._require_configured()

        # Imported here so importing this module (e.g. for the factory's typing)
        # never pulls in the transport, matching the codebase's lazy-import gating.
        import smtplib
        from email.message import EmailMessage as MIMEEmailMessage

        mime = MIMEEmailMessage()
        mime["From"] = self._from_address
        mime["To"] = message.to
        mime["Subject"] = message.subject
        # RFC 8058 one-click unsubscribe: let compliant clients (Gmail/Yahoo/
        # Apple) render a native unsubscribe that POSTs, so a link scanner that
        # pre-fetches GET URLs can never silently opt a user out.
        if message.list_unsubscribe_url:
            mime["List-Unsubscribe"] = f"<{message.list_unsubscribe_url}>"
            mime["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        mime.set_content(message.text_body)
        mime.add_alternative(message.html_body, subtype="html")

        # Bound the connect/handshake so one unresponsive server can't hang the
        # whole weekly run. Port 465 is implicit TLS (SMTP_SSL); other ports use
        # STARTTLS when the server offers it (best-effort, not all relays do).
        if self._port == 465:
            with smtplib.SMTP_SSL(self._host, self._port, timeout=30) as smtp:
                if self._username:
                    smtp.login(self._username, self._password)
                smtp.send_message(mime)
        else:
            with smtplib.SMTP(self._host, self._port, timeout=30) as smtp:
                smtp.ehlo()
                if smtp.has_extn("starttls"):
                    smtp.starttls()
                    smtp.ehlo()  # re-identify over the now-encrypted channel
                if self._username:
                    smtp.login(self._username, self._password)
                smtp.send_message(mime)

        # Log the fact of a send (recipient + subject only) — never the body.
        logger.info(
            "digest_email_sent to=%s subject=%s provider=%s",
            message.to,
            message.subject,
            self.provider,
        )
