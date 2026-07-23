"""Application configuration, loaded from environment via pydantic-settings.

Secrets are never hardcoded here — everything is env-driven with safe local
defaults. See .env.example for the documented variables.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings sourced from environment variables / .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Postgres connection string used by the db module.
    DATABASE_URL: str = "postgresql://ballast:ballast@localhost:5432/ballast"

    # Comma-separated list of allowed CORS origins for the frontend.
    CORS_ALLOWED_ORIGINS: str = "http://localhost:5173"

    # Secret used by FastAPI-Users' UserManager to sign registration/reset
    # tokens. Dev default only — a real secret must be supplied via env in any
    # non-local environment. Never commit a real value.
    USER_MANAGER_SECRET: str = "dev-only-insecure-user-manager-secret-change-me"

    @property
    def cors_origins(self) -> list[str]:
        """Parse CORS_ALLOWED_ORIGINS into a clean list."""
        return [o.strip() for o in self.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

    @property
    def async_database_url(self) -> str:
        """Return DATABASE_URL in the SQLAlchemy asyncpg form.

        The existing sync health check uses the plain ``postgresql://`` URL via
        psycopg. FastAPI-Users / SQLAlchemy async needs the
        ``postgresql+asyncpg://`` driver form; we derive it here so a single
        DATABASE_URL env var drives both layers.
        """
        url = self.DATABASE_URL
        if url.startswith("postgresql+asyncpg://"):
            return url
        if url.startswith("postgresql://"):
            return "postgresql+asyncpg://" + url[len("postgresql://") :]
        if url.startswith("postgres://"):
            return "postgresql+asyncpg://" + url[len("postgres://") :]
        return url


def get_settings() -> Settings:
    """Return a fresh Settings instance.

    Not cached so tests can override the environment (e.g. a bad DATABASE_URL)
    and get the updated value on the next call.
    """
    return Settings()
