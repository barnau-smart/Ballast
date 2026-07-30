"""Story 5.1 — the EmailPort factory + adapter gating (no DB, no network).

FakeEmailAdapter is the tested path (records in memory). The real SmtpEmailAdapter
is code-complete but credential-gated: constructing it without SMTP_HOST /
DIGEST_FROM_ADDRESS fails loud with EmailNotConfiguredError, and ``smtplib`` is
never imported at module import (only lazily inside ``send``). Mirrors the LLM /
market-data factory tests.
"""

from __future__ import annotations

import sys

import pytest

from digest.email_port import EmailMessage, EmailNotConfiguredError, EmailPort
from digest.fake_adapter import FakeEmailAdapter
from digest.factory import UnknownEmailAdapterError, get_email_sender


def test_factory_returns_fake_by_default(monkeypatch):
    monkeypatch.delenv("EMAIL_ADAPTER", raising=False)
    sender = get_email_sender()
    assert isinstance(sender, FakeEmailAdapter)
    assert isinstance(sender, EmailPort)


def test_factory_explicit_fake(monkeypatch):
    monkeypatch.setenv("EMAIL_ADAPTER", "fake")
    assert isinstance(get_email_sender(), FakeEmailAdapter)


def test_factory_unknown_adapter_raises(monkeypatch):
    monkeypatch.setenv("EMAIL_ADAPTER", "bogus")
    with pytest.raises(UnknownEmailAdapterError):
        get_email_sender()


def test_fake_adapter_records_sent_messages():
    sender = FakeEmailAdapter()
    assert sender.sent == []
    msg = EmailMessage(
        to="a@example.com", subject="hi", text_body="body", html_body="<p>body</p>"
    )
    sender.send(msg)
    assert sender.sent == [msg]


def test_smtp_adapter_no_host_raises_without_importing_smtplib(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("DIGEST_FROM_ADDRESS", "")  # force unconfigured

    # Importing the module must NOT import smtplib (lazy inside send()).
    from digest.smtp_adapter import SmtpEmailAdapter

    before = "smtplib" in sys.modules
    with pytest.raises(EmailNotConfiguredError):
        SmtpEmailAdapter()
    after = "smtplib" in sys.modules
    # Construction must not have newly imported the transport.
    assert after == before


def test_factory_smtp_no_host_raises(monkeypatch):
    monkeypatch.setenv("EMAIL_ADAPTER", "smtp")
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("DIGEST_FROM_ADDRESS", "")
    with pytest.raises(EmailNotConfiguredError):
        get_email_sender()
