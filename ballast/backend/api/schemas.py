"""Pydantic schemas for the user/auth surface.

``UserRead`` deliberately exposes ONLY safe fields (id + email + flags). It
never includes ``password`` or ``hashed_password`` — the register response must
not leak credentials.
"""

from __future__ import annotations

import uuid

from fastapi_users import schemas


class UserRead(schemas.BaseUser[uuid.UUID]):
    """Public representation of a user. No password/hash fields."""


class UserCreate(schemas.BaseUserCreate):
    """Registration payload: email + password (validated server-side)."""
