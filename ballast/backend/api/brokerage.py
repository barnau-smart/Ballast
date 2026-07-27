"""Brokerage link endpoints (Story 2.1) — authorize / callback / status.

All three endpoints are authenticated (reuse 1.3 ``current_user`` via 1.4
``get_scope``) and never leak token values. Tokens are encrypted at the app
layer (``brokers.crypto``) and stored per-user ONLY through the fail-closed
``ScopedRepository`` (AD-10). The OAuth ``code`` and any token material are
NEVER logged.

CSRF state approach (documented choice)
---------------------------------------
The ``state`` is a STATELESS, HMAC-signed token — no server-side state table is
needed. On ``/authorize`` we mint ``base64(payload).hex(HMAC-SHA256(payload))``
where ``payload = "<user_id>:<nonce>:<issued_at>"``, signed with
``USER_MANAGER_SECRET`` (the key already used to sign JWTs; it lives outside the
DB). On ``/callback`` we recompute the HMAC (constant-time compare), confirm the
signature is valid, that the embedded user id matches the authenticated user
(so a state minted for user A cannot be replayed by user B), and that it has not
expired. This binds the callback to the same user who started the flow and
prevents CSRF, while keeping the flow stateless and greppable.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import logging
import secrets
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.deps import get_scope
from brokers.crypto import encrypt_token
from brokers.factory import get_broker
from brokers.port import BrokerPort
from brokers.portfolio import reconcile_portfolio
from brokers.session import get_brokerage_session
from db.models import BrokerageToken
from db.repository import ScopedRepository
from db.scope import Scope
from db.session import get_async_session

logger = logging.getLogger("ballast.brokerage")

router = APIRouter(prefix="/api/brokerage", tags=["brokerage"])

# How long a minted CSRF state remains valid (seconds). A user must complete the
# OAuth round-trip within this window.
_STATE_TTL_SECONDS = 15 * 60


# --- CSRF state (stateless, HMAC-signed) -------------------------------------


def _sign(payload: str) -> str:
    secret = get_settings().USER_MANAGER_SECRET.encode("utf-8")
    return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def mint_state(user_id: uuid.UUID) -> str:
    """Mint a signed CSRF state bound to ``user_id`` (stateless)."""
    nonce = secrets.token_urlsafe(16)
    issued_at = int(time.time())
    payload = f"{user_id}:{nonce}:{issued_at}"
    # Strip base64 '=' padding so the state survives URL-encoding unchanged
    # (kept greppable/comparable end-to-end); padding is restored on verify.
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{encoded}.{_sign(payload)}"


def verify_state(state: str, user_id: uuid.UUID) -> bool:
    """Return True iff ``state`` is a valid, unexpired state for ``user_id``."""
    if not state or "." not in state:
        return False
    encoded, provided_sig = state.rsplit(".", 1)
    try:
        # Restore stripped base64 padding before decoding.
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    # Constant-time signature check.
    if not hmac.compare_digest(_sign(payload), provided_sig):
        return False
    parts = payload.split(":")
    if len(parts) != 3:
        return False
    state_user_id, _nonce, issued_at_raw = parts
    # Must belong to the authenticated user (no cross-user replay).
    if state_user_id != str(user_id):
        return False
    try:
        issued_at = int(issued_at_raw)
    except ValueError:
        return False
    if time.time() - issued_at > _STATE_TTL_SECONDS:
        return False
    return True


# --- Resume-in-place: return_to allowlist (no open redirect) -----------------

# The exact set of in-app surfaces a user may be sent back to after re-auth.
# ``return_to`` is validated against THIS allowlist only — an external or
# absolute URL is rejected so re-auth can never be turned into an open redirect
# (Story 2.2 security requirement). Mirrors the frontend's known routes.
_ALLOWED_RETURN_TO: frozenset[str] = frozenset(
    {"/", "/dashboard", "/coach", "/decisions", "/settings", "/onboarding", "/auth"}
)


def sanitize_return_to(return_to: str | None) -> str | None:
    """Return ``return_to`` iff it is a known in-app path, else ``None``.

    Accepts ONLY an exact match against the in-app allowlist (a relative path
    like ``/coach``). Anything absolute or external — ``https://evil.com``,
    ``//evil.com``, a scheme-relative or backslash-obfuscated target, or an
    unknown path — is rejected (returns ``None``), so re-auth cannot be turned
    into an open redirect. Never raises; a bad value simply resolves to the
    default landing page on the frontend.
    """
    if not return_to:
        return None
    # Defensive: an allowlisted value is already relative + known, but reject
    # anything carrying a scheme/host or backslash outright before matching.
    if "://" in return_to or return_to.startswith("//") or "\\" in return_to:
        return None
    return return_to if return_to in _ALLOWED_RETURN_TO else None


# --- Schemas -----------------------------------------------------------------


class AuthorizeResponse(BaseModel):
    authorization_url: str
    state: str
    # Echoes back the validated in-app path to resume at after re-auth (or None
    # if none/invalid was supplied). NEVER an external URL (see sanitize).
    return_to: str | None = None


class CallbackRequest(BaseModel):
    code: str
    state: str


class CallbackResponse(BaseModel):
    linked: bool


class StatusResponse(BaseModel):
    """Plain brokerage session status — NEVER any token value.

    ``state`` is the Story 2.2 lifecycle: ``unlinked`` / ``live`` / ``expired``.
    ``linked`` is retained (``state != "unlinked"``) for backward compatibility
    with the Story 2.1 shape. ``expires_at`` is the (non-secret) token expiry,
    present only when a token exists.
    """

    state: str
    linked: bool
    provider: str | None = None
    expires_at: datetime.datetime | None = None


# --- Endpoints ---------------------------------------------------------------


@router.get("/authorize", response_model=AuthorizeResponse)
async def authorize(
    return_to: str | None = Query(
        default=None,
        description=(
            "Optional in-app path to resume at after re-auth (resume-in-place). "
            "Validated against an allowlist; external/absolute URLs are ignored."
        ),
    ),
    scope: Scope = Depends(get_scope),
    broker: BrokerPort = Depends(get_broker),
) -> AuthorizeResponse:
    """Return the brokerage authorization URL plus a signed CSRF ``state``.

    Also used for RE-AUTH (Story 2.2): the same link flow re-links an expired
    session (callback overwrites the old token → restores ``live``). The
    optional ``return_to`` supports resume-in-place — it round-trips back to the
    caller so the frontend can send the user to the surface they were on. It is
    validated against an in-app allowlist so it can never become an open
    redirect; an external/absolute URL resolves to ``None``.
    """
    state = mint_state(scope.user_id)
    url = broker.authorization_url(state)
    safe_return_to = sanitize_return_to(return_to)
    logger.info("brokerage_authorize_issued user_id=%s", scope.user_id)
    return AuthorizeResponse(
        authorization_url=url, state=state, return_to=safe_return_to
    )


@router.post("/callback", response_model=CallbackResponse)
async def callback(
    body: CallbackRequest,
    scope: Scope = Depends(get_scope),
    broker: BrokerPort = Depends(get_broker),
    session: AsyncSession = Depends(get_async_session),
) -> CallbackResponse:
    """Validate ``state``, exchange the ``code``, and store encrypted tokens."""
    if not verify_state(body.state, scope.user_id):
        # Never echo the code/state back.
        raise HTTPException(status_code=400, detail="Invalid or expired link request.")

    # Exchange the code for tokens (fake: local; schwab: network). Never log it.
    # Pass the validated state through: the real adapter needs it for authlib's
    # state check on the redirect (H1).
    tokens = broker.exchange_code(body.code, body.state)
    provider = getattr(broker, "provider", "unknown")

    # Encrypt at the app layer BEFORE anything touches the DB (AD-10 / NFR1).
    enc_access = encrypt_token(tokens.access_token)
    enc_refresh = encrypt_token(tokens.refresh_token)

    repo = ScopedRepository(BrokerageToken, scope, session)
    # Replace any existing token rows for this user (re-link overwrites).
    existing = await repo.list()
    for row in existing:
        await session.delete(row)
    await repo.add(
        provider=provider,
        access_token=enc_access,
        refresh_token=enc_refresh,
        expires_at=tokens.expires_at,
    )
    await session.commit()

    logger.info("brokerage_linked user_id=%s provider=%s", scope.user_id, provider)

    # Import-on-connect (Story 2.3, AD-14): pull the user's holdings in as soon
    # as the account is linked, via the single-writer projection. Resilient — a
    # fetch failure must NOT break the link (the account is linked; the user can
    # retry the import / it reconciles later). Never surface broker internals.
    try:
        await reconcile_portfolio(scope, session, broker)
    except Exception as exc:  # noqa: BLE001 — link must survive any import failure
        logger.warning(
            "portfolio_import_on_connect_failed user_id=%s error_type=%s",
            scope.user_id,
            type(exc).__name__,
        )

    return CallbackResponse(linked=True)


@router.get("/status", response_model=StatusResponse)
async def status(
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> StatusResponse:
    """Report the current user's brokerage session state. No token values.

    Story 2.2: computes ``unlinked`` / ``live`` / ``expired`` from the stored
    ``expires_at`` (no network call), read through the fail-closed scoped repo
    so it only ever reflects THIS user. The frontend drives the reauth-banner
    (shown on ``expired``) and degraded-mode UX off this.
    """
    broker_session = await get_brokerage_session(scope, session)
    return StatusResponse(
        state=broker_session.state,
        linked=broker_session.state != "unlinked",
        provider=broker_session.provider,
        expires_at=broker_session.expires_at,
    )
