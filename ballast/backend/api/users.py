"""FastAPI-Users wiring: user manager, auth backend, and shared instance.

Builds the ``FastAPIUsers`` instance, the ``UserManager``, and the JWT
``auth_backend`` (bearer transport + stateless JWT strategy). Routers are
mounted in ``api/app.py``:
- register router (Story 1.2) at ``/api/auth``
- JWT login/logout router (Story 1.3) at ``/api/auth/jwt``, reusing the
  ``auth_backend`` defined here.

Password hashing is FastAPI-Users' built-in (pwdlib / Argon2) — never
hand-rolled. Passwords, tokens, and secrets are never logged.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator

from fastapi import Depends
from fastapi_users import (
    BaseUserManager,
    FastAPIUsers,
    InvalidPasswordException,
    UUIDIDMixin,
)
from fastapi_users.authentication import (
    AuthenticationBackend,
    BearerTransport,
    JWTStrategy,
)
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase

from api.config import get_settings
from db.models import User
from db.session import get_user_db

logger = logging.getLogger("ballast.auth")


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    """Manages user lifecycle. Secrets come from settings, never hardcoded."""

    @property
    def reset_password_token_secret(self) -> str:  # type: ignore[override]
        return get_settings().USER_MANAGER_SECRET

    @property
    def verification_token_secret(self) -> str:  # type: ignore[override]
        return get_settings().USER_MANAGER_SECRET

    MIN_PASSWORD_LENGTH = 8

    async def validate_password(self, password: str, user) -> None:  # type: ignore[override]
        # FastAPI-Users' default validate_password is a no-op, which would allow
        # empty/1-char passwords. Enforce a floor and surface a plain-language
        # reason (mapped to a warm message + 4xx by the app's exception handler).
        if len(password) < self.MIN_PASSWORD_LENGTH:
            raise InvalidPasswordException(
                reason="Password must be at least 8 characters."
            )

    async def on_after_register(
        self, user: User, request=None
    ) -> None:
        # Log the event WITHOUT the email address or any credential material.
        logger.info("user_registered user_id=%s", user.id)


async def get_user_manager(
    user_db: SQLAlchemyUserDatabase = Depends(get_user_db),
) -> AsyncGenerator[UserManager, None]:
    """FastAPI dependency yielding a configured UserManager."""
    yield UserManager(user_db)


def _get_jwt_strategy() -> JWTStrategy:
    # Stateless JWT, 1-hour lifetime. "Logout" clears the client-held token but
    # does NOT server-side-revoke an already-issued JWT before expiry (a token
    # denylist is out of scope for v1). The login router (Story 1.3) issues
    # these tokens.
    return JWTStrategy(secret=get_settings().USER_MANAGER_SECRET, lifetime_seconds=3600)


auth_backend = AuthenticationBackend(
    name="jwt",
    transport=BearerTransport(tokenUrl="api/auth/jwt/login"),
    get_strategy=_get_jwt_strategy,
)

fastapi_users = FastAPIUsers[User, uuid.UUID](get_user_manager, [auth_backend])
