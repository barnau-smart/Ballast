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

from api.brokerage import router as brokerage_router
from api.cash import router as cash_router
from api.coach import router as coach_router
from api.config import get_settings
from api.digest import router as digest_router
from api.portfolio import router as portfolio_router
from api.precedent import router as precedent_router
from api.logging_config import configure_logging
from api.schemas import UserCreate, UserRead
from api.users import auth_backend, fastapi_users
from coach.maintenance import MaintenanceScheduler
from marketdata.scheduler import MarketDataIngestScheduler
from db.connection import check_db
from db.migrations import run_startup_migrations
from db.models import User
from db.session import create_db_and_tables, engine

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
        # Idempotent startup migration (Story 7.1): create_all never ALTERs an
        # already-existing table, so patch every Epic 6 addition (columns +
        # indexes + NULL-key backfill) onto a carried-over DB. Runs strictly
        # AFTER create_all so any brand-new table already exists; a fresh or
        # already-migrated DB makes this a clean no-op.
        await run_startup_migrations(engine)
        # In-process decisions-maintenance scheduler (pre-unattended-prod
        # hardening, 2026-08-06): periodically reclaims crash-orphaned
        # ``cosigning`` rows (a possibly-live order that would otherwise strand
        # forever) and prunes stale ``proposed`` rows. ON by default; the whole
        # test suite runs with it OFF (exercised directly instead). Started after
        # the schema is provisioned; stopped cleanly on shutdown.
        # Background schedulers (started after schema is provisioned; each ON by
        # default, both forced OFF for the test suite). Collected so shutdown
        # stops every one that started.
        schedulers: list[object] = []
        if settings.DECISION_MAINTENANCE_ENABLED:
            maintenance = MaintenanceScheduler.from_settings(settings)
            maintenance.start()
            schedulers.append(maintenance)
            logger.info(
                "decisions_maintenance_scheduler_enabled interval_s=%d",
                settings.DECISION_MAINTENANCE_INTERVAL_SECONDS,
            )
        # In-process daily market-data ingest (2026-08-07): keeps market_daily
        # fresh (recent-window re-ingest via the configured adapter). Work-first,
        # so real data refreshes on boot too. Full backfill stays a manual CLI.
        if settings.MARKETDATA_INGEST_ENABLED:
            marketdata_ingest = MarketDataIngestScheduler.from_settings(settings)
            marketdata_ingest.start()
            schedulers.append(marketdata_ingest)
            logger.info(
                "marketdata_ingest_scheduler_enabled interval_s=%d adapter=%s",
                settings.MARKETDATA_INGEST_INTERVAL_SECONDS,
                settings.MARKETDATA_ADAPTER,
            )
        try:
            yield
        finally:
            for sched in schedulers:
                await sched.stop()

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
        # Log the type + full traceback SERVER-SIDE for debugging (never echoed to
        # the client — the response body below stays generic). exc_info=True is safe
        # here: it goes to the server log only.
        logger.error(
            "unhandled_exception error_type=%s", type(exc).__name__, exc_info=True
        )
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

    # Brokerage link flow (Story 2.1): authorize / callback / status. All
    # authenticated + scoped; tokens stored encrypted via the scoped repo.
    app.include_router(brokerage_router)

    # Portfolio read + refresh (Story 2.3): the AD-14 single-writer projection.
    # Read-only cache read + on-demand reconcile from the authoritative broker.
    app.include_router(portfolio_router)

    # Recovery-precedent read (Story 3.3, FR15): one auth-gated, read-only
    # surface over the Precedent Engine (AD-3). Returns the AD-12 evidence shape
    # verbatim; the Coach surface renders it.
    app.include_router(precedent_router)

    # Coach propose-and-approve (Story 4.6, FR8/FR9/FR10, AD-7/AD-11): the
    # coach's first HTTP surface. /recommend proposes (degraded-ok, never
    # executes); /approve executes through the Coach Engine on a live session.
    app.include_router(coach_router)

    # Weekly digest opt-in (Story 5.1, FR21): the Settings toggle's
    # authenticated GET/PUT preference endpoints plus the unauthenticated
    # one-click unsubscribe link. Email is the only channel; nothing sends here.
    app.include_router(digest_router)

    # Cash configuration (Story 9.1, Epic 9): the Settings "Cash setup" card's
    # authenticated GET/PUT config endpoints — user-declared reserve + parked
    # money-market symbols, funneled through the fail-closed scope (AD-10).
    app.include_router(cash_router)

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
