"""FastAPI application factory.

Establishes the cross-cutting concerns every future story reuses:
- CORS for the configured frontend origins
- structured logging
- a consistent JSON error envelope: {"error": {"type": ..., "message": ...}}
- the /api/health endpoint with a live Postgres check
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.config import get_settings
from api.logging_config import configure_logging
from api.schemas import UserCreate, UserRead
from api.users import auth_backend, fastapi_users
from db.connection import check_db
from db.models import User
from db.session import create_db_and_tables

logger = logging.getLogger("ballast.api")

# Maps FastAPI-Users' machine error codes (returned as HTTPException detail
# strings) to warm, plain-language, jargon-free copy for users (NFR6/NFR8).
_AUTH_ERROR_MESSAGES: dict[str, str] = {
    "REGISTER_USER_ALREADY_EXISTS": (
        "An account with that email already exists. Try logging in instead."
    ),
    "REGISTER_INVALID_PASSWORD": (
        "That password will not work. Please choose a longer, stronger one."
    ),
    # Story 1.3: a single generic message for BOTH a wrong password and an
    # unknown email. FastAPI-Users returns the same LOGIN_BAD_CREDENTIALS code
    # for both cases, so mapping it here guarantees no user enumeration on login.
    "LOGIN_BAD_CREDENTIALS": (
        "That email or password doesn't match. Please try again."
    ),
}


def _error_response(status_code: int, error_type: str, message: str) -> JSONResponse:
    """Build the canonical error envelope response."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"type": error_type, "message": message}},
    )


def create_app() -> FastAPI:
    """Construct and configure the Ballast FastAPI application."""
    configure_logging()
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        # Lightweight create-all for local/dev (single-instance assumption).
        # Creates the `user` table if it does not yet exist. Alembic can
        # replace this later.
        await create_db_and_tables()
        yield

    app = FastAPI(title="Ballast API", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    # --- Consistent JSON error envelope --------------------------------------

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # FastAPI-Users surfaces machine codes in exc.detail. For registration
        # failures the detail is either a bare code string
        # (REGISTER_USER_ALREADY_EXISTS) or a dict {"code", "reason"} for
        # password validation. Map both to warm, plain-language copy and never
        # leak internal codes/reasons to the client.
        code: str | None = None
        reason: str | None = None
        if isinstance(exc.detail, str):
            code = exc.detail
        elif isinstance(exc.detail, dict):
            raw_code = exc.detail.get("code")
            if isinstance(raw_code, str):
                code = raw_code
            raw_reason = exc.detail.get("reason")
            if isinstance(raw_reason, str):
                reason = raw_reason

        if code in _AUTH_ERROR_MESSAGES:
            # For password validation, the reason is our own plain-language copy
            # (e.g. "Password must be at least 8 characters.") — surface it so the
            # user knows the actual requirement. Never surface internal codes.
            message = (
                reason
                if code == "REGISTER_INVALID_PASSWORD" and reason
                else _AUTH_ERROR_MESSAGES[code]
            )
            return _error_response(exc.status_code, "auth_error", message)

        detail = exc.detail if isinstance(exc.detail, str) else "HTTP error"
        return _error_response(exc.status_code, "http_error", detail)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(422, "validation_error", "Request validation failed")

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        # Log the type only; never echo internals or secrets to the client.
        logger.error("unhandled_exception error_type=%s", type(exc).__name__)
        return _error_response(
            500, "internal_error", "An unexpected error occurred."
        )

    # --- Auth routes ---------------------------------------------------------

    # Registration (Story 1.2).
    app.include_router(
        fastapi_users.get_register_router(UserRead, UserCreate),
        prefix="/api/auth",
        tags=["auth"],
    )

    # Login / logout (Story 1.3). Reuses the existing auth_backend + JWT
    # strategy from api/users.py — nothing recreated. Gives:
    #   POST /api/auth/jwt/login  (OAuth2 form: username=email, password)
    #   POST /api/auth/jwt/logout (best-effort; JWT is stateless — see below)
    app.include_router(
        fastapi_users.get_auth_router(auth_backend),
        prefix="/api/auth/jwt",
        tags=["auth"],
    )

    # --- Routes --------------------------------------------------------------

    # A single protected route to prove authed access (Story 1.3). Returns the
    # current active user via UserRead (id + email + flags only — never the
    # password hash). Unauthenticated requests get 401 from the dependency.
    current_active_user = fastapi_users.current_user(active=True)

    @app.get("/api/users/me", response_model=UserRead, tags=["users"])
    async def read_current_user(
        user: User = Depends(current_active_user),
    ) -> User:
        return user

    @app.get("/api/health")
    async def health() -> JSONResponse:
        """Liveness + dependency check. Reflects a real Postgres probe."""
        db_ok = check_db()
        if db_ok:
            body = {"status": "ok", "db": "ok"}
        else:
            body = {"status": "degraded", "db": "down"}
        # Always HTTP 200 — degradation is reported in the body.
        return JSONResponse(status_code=200, content=body)

    logger.info("app_created cors_origins=%s", settings.cors_origins)
    return app
