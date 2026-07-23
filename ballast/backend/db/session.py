"""Async SQLAlchemy engine + session factory.

Lives alongside the existing sync ``connection.py`` (which still powers the
``/api/health`` psycopg probe). This module provides the async layer that
FastAPI-Users needs, derived from the same ``DATABASE_URL`` env var via
``settings.async_database_url`` (the ``postgresql+asyncpg://`` form).

Table creation for local/dev is a lightweight ``create_all`` on startup —
acceptable for v1 per the spine's single-instance assumption. A real migration
tool (Alembic) can replace this later.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Depends
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from api.config import get_settings
from db.models import Base, User


def _make_engine():
    settings = get_settings()
    # NullPool: do not cache connections across requests. asyncpg connections
    # are bound to the event loop that created them; a cached connection would
    # break under an ASGI server that switches loops (e.g. across test clients).
    # For the v1 single-instance scale this is a fine tradeoff for correctness.
    return create_async_engine(
        settings.async_database_url, echo=False, poolclass=NullPool
    )


engine = _make_engine()
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


async def create_db_and_tables() -> None:
    """Create tables that do not yet exist (dev/local convenience).

    Idempotent: only creates missing tables. Called on app startup.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async SQLAlchemy session."""
    async with async_session_maker() as session:
        yield session


async def get_user_db(
    session: AsyncSession = Depends(get_async_session),
) -> AsyncGenerator[SQLAlchemyUserDatabase, None]:
    """FastAPI dependency yielding the FastAPI-Users SQLAlchemy adapter."""
    yield SQLAlchemyUserDatabase(session, User)
