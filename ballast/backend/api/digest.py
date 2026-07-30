"""Weekly-digest preference + unsubscribe endpoints (Story 5.1, FR21).

Two authenticated, user-scoped endpoints back the Settings opt-in toggle
(``GET``/``PUT /api/digest/preference``) — both funnel through the fail-closed
scope (AD-10), so a user only ever reads or changes their OWN preference.

One UNAUTHENTICATED endpoint (``GET /api/digest/unsubscribe``) backs the
one-click link in every email: the recipient has no session, only their
unguessable token. It returns the SAME calm confirmation whether or not the token
matched, so it never reveals whether an account exists.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_scope
from db.scope import Scope
from db.session import get_async_session
from digest.preferences import (
    get_or_create_preference,
    set_opt_in,
    unsubscribe_by_token,
)

logger = logging.getLogger("ballast.api.digest")

router = APIRouter(prefix="/api/digest", tags=["digest"])


# --- Schemas -----------------------------------------------------------------


class DigestPreferenceOut(BaseModel):
    """The current user's digest opt-in state — plain, no secrets."""

    opted_in: bool


class DigestPreferenceIn(BaseModel):
    """The desired opt-in state from the Settings toggle."""

    opted_in: bool


# --- Authenticated preference endpoints --------------------------------------


@router.get("/preference", response_model=DigestPreferenceOut)
async def read_preference(
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> DigestPreferenceOut:
    """Return the current user's opt-in (defaults to OFF for a new user)."""
    pref = await get_or_create_preference(scope, session)
    return DigestPreferenceOut(opted_in=pref.opted_in)


@router.put("/preference", response_model=DigestPreferenceOut)
async def update_preference(
    body: DigestPreferenceIn,
    scope: Scope = Depends(get_scope),
    session: AsyncSession = Depends(get_async_session),
) -> DigestPreferenceOut:
    """Enable or disable the weekly digest for the current user (takes effect now)."""
    pref = await set_opt_in(scope, session, body.opted_in)
    return DigestPreferenceOut(opted_in=pref.opted_in)


# --- Unauthenticated one-click unsubscribe -----------------------------------

_UNSUBSCRIBE_HTML = (
    "<html><body style=\"font-family: sans-serif; line-height: 1.5; "
    "color: #1a1a1a; max-width: 32rem; margin: 3rem auto;\">"
    "<h1 style=\"font-size: 1.25rem;\">You're unsubscribed</h1>"
    "<p>You won't receive the weekly digest anymore. No hard feelings — "
    "Ballast is here whenever you want it, and you can turn the digest back "
    "on any time from Settings.</p>"
    "</body></html>"
)


async def _do_unsubscribe(token: str, session: AsyncSession) -> HTMLResponse:
    """Shared opt-out: flip OFF by token, return the SAME calm confirmation.

    Identical response whether or not the token matched, so nothing about account
    existence leaks (no enumeration). Always HTTP 200.
    """
    matched = await unsubscribe_by_token(session, token)
    # Log only whether a row matched — never the token value.
    logger.info("digest_unsubscribe matched=%s", matched)
    return HTMLResponse(content=_UNSUBSCRIBE_HTML, status_code=200)


@router.post("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_post(
    token: str = "",
    session: AsyncSession = Depends(get_async_session),
) -> HTMLResponse:
    """One-click unsubscribe by token — UNAUTHENTICATED, state-changing via POST.

    This is the RFC 8058 (``List-Unsubscribe-Post``) target and the safe path a
    mail client's native one-click uses: because it is POST, a link scanner that
    pre-fetches GET URLs can never silently opt a user out.
    """
    return await _do_unsubscribe(token, session)


@router.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe(
    token: str = "",
    session: AsyncSession = Depends(get_async_session),
) -> HTMLResponse:
    """One-click unsubscribe by token — UNAUTHENTICATED (direct human link click).

    Kept for the in-body link so a click always works. The preferred, scanner-safe
    path is the POST above (advertised via ``List-Unsubscribe-Post``); the token is
    unguessable and the opt-out is fully reversible from Settings.
    """
    return await _do_unsubscribe(token, session)
