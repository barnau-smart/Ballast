"""Application-layer token encryption (AD-10 / NFR1).

Brokerage OAuth tokens are the crown jewels. They are encrypted HERE, in the
application layer, with a symmetric key (Fernet / AES-128-CBC + HMAC) held in
the environment (``TOKEN_ENCRYPTION_KEY``) — the key NEVER lives in the
database. The database therefore only ever holds ciphertext; a database dump
alone cannot reveal a token.

Fail-loud: a missing or malformed key raises ``TokenEncryptionError`` at first
use (not at import), so a misconfiguration surfaces immediately rather than
silently degrading. Plaintext tokens and the key itself are NEVER logged.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from api.config import get_settings


class TokenEncryptionError(RuntimeError):
    """Raised when token encryption/decryption cannot be performed.

    Covers a missing/invalid ``TOKEN_ENCRYPTION_KEY`` and any ciphertext that
    cannot be decrypted (tampered/wrong key). The message deliberately never
    includes the key or any plaintext.
    """


def _get_fernet() -> Fernet:
    """Build a Fernet from the env key, failing loudly if it is missing/invalid."""
    key = get_settings().TOKEN_ENCRYPTION_KEY
    if not key:
        raise TokenEncryptionError(
            "TOKEN_ENCRYPTION_KEY is not set; refusing to store brokerage "
            "tokens without an encryption key (fail-closed)."
        )
    try:
        return Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        # Do NOT include the key value in the error.
        raise TokenEncryptionError(
            "TOKEN_ENCRYPTION_KEY is invalid; it must be a 32-byte url-safe "
            "base64-encoded Fernet key."
        ) from exc


def encrypt_token(plaintext: str) -> str:
    """Encrypt a token string, returning url-safe base64 ciphertext.

    Raises :class:`TokenEncryptionError` if the key is missing/invalid.
    """
    if plaintext is None:
        raise TokenEncryptionError("Cannot encrypt a None token.")
    fernet = _get_fernet()
    return fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: str) -> str:
    """Decrypt ciphertext produced by :func:`encrypt_token` back to plaintext.

    Raises :class:`TokenEncryptionError` if the key is missing/invalid or the
    ciphertext cannot be decrypted (tampered or encrypted under another key).
    """
    if ciphertext is None:
        raise TokenEncryptionError("Cannot decrypt a None value.")
    fernet = _get_fernet()
    try:
        return fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise TokenEncryptionError(
            "Could not decrypt the stored token (wrong key or tampered data)."
        ) from exc
