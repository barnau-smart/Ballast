"""Application configuration, loaded from environment via pydantic-settings.

Secrets are never hardcoded here — everything is env-driven with safe local
defaults. See .env.example for the documented variables.
"""

from __future__ import annotations

from pydantic import Field
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

    # --- Brokerage / Story 2.1 ------------------------------------------------

    # Fernet key used to encrypt brokerage OAuth tokens at the application layer
    # (AD-10 / NFR1). The key lives HERE in the environment, never in the DB.
    # This dev default is a syntactically-valid Fernet key that is PUBLIC and
    # therefore INSECURE — it exists only so the fake flow runs locally out of
    # the box. A real, secret key MUST be supplied via env in any non-local
    # environment (generate one with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # ). NEVER commit a real key.
    # (decodes to the ASCII marker "INSECURE-dev-key-do-not-use!!!\x00\x00").
    TOKEN_ENCRYPTION_KEY: str = "SU5TRUNVUkUtZGV2LWtleS1kby1ub3QtdXNlISEhAAA="

    # Which broker adapter the factory returns. "fake" for dev/test (no creds),
    # "schwab" when the three SCHWAB_* values below are set.
    BROKER_ADAPTER: str = "fake"

    # Schwab developer app credentials. Empty by default — the SchwabAdapter is
    # code-complete but gated: using it without these raises a clear
    # "Schwab not configured" error (it never crashes at import).
    SCHWAB_CLIENT_ID: str = ""
    SCHWAB_CLIENT_SECRET: str = ""
    SCHWAB_CALLBACK_URL: str = ""

    # --- Market data / Story 3.1 ---------------------------------------------

    # Which market-data adapter the factory returns. "fake" for dev/test (no
    # creds, no network, deterministic), "tiingo" when TIINGO_API_KEY is set.
    MARKETDATA_ADAPTER: str = "fake"

    # Tiingo API key. Empty by default — the TiingoAdapter is code-shaped but
    # gated: using it without this raises a clear "Tiingo not configured" error
    # (it never crashes at import, and the fake adapter needs no creds).
    TIINGO_API_KEY: str = ""

    # --- LLM / Story 4.1 ------------------------------------------------------

    # Which LLM gateway adapter the factory returns. "fake" for dev/test (no
    # creds, no network, deterministic — the tested path), "anthropic" when
    # ANTHROPIC_API_KEY is set.
    LLM_ADAPTER: str = "fake"

    # Anthropic API key. Empty by default — the AnthropicGateway is code-shaped
    # but gated: constructing it without this raises a clear "LLM not configured"
    # error (it never crashes at import, never imports the SDK at import time,
    # and the fake adapter needs no creds). The key is never logged.
    ANTHROPIC_API_KEY: str = ""

    # Per-request transport budget for the live Anthropic path (Story 7.4). The
    # SDK client is built once (connection reuse) with these values so a hung
    # call surfaces as a typed LLMTransportError the coach degrades to the
    # default plan in SECONDS, not the SDK's ~10-minute non-streaming default.
    # 60s clears a normal interactive Opus structured /recommend completion with
    # margin; env-tunable so ops can dial it against real latency at Story 7.6.
    # Applied when the pooled gateway/client is built, so a change is picked up on
    # the next rebuild (the factory keys its memo on this value); must be > 0
    # (a 0/negative timeout is fail-loud rather than a degenerate instant-timeout).
    LLM_REQUEST_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0)
    # Retry budget the SDK client is built with (Story 7.4). Makes the SDK's own
    # default (2) explicit and env-tunable for 7.6 real-world tuning; must be >= 0
    # (0 disables retries).
    LLM_MAX_RETRIES: int = Field(default=2, ge=0)

    # --- Weekly digest / Story 5.1 -------------------------------------------

    # Which email adapter the factory returns. "fake" for dev/test (no creds, no
    # network, records messages in memory — the tested path), "smtp" when the
    # SMTP_* + DIGEST_FROM_ADDRESS values below are set.
    EMAIL_ADAPTER: str = "fake"

    # SMTP transport settings for the real SmtpEmailAdapter. Empty by default —
    # the adapter is code-complete but gated: using it without SMTP_HOST /
    # DIGEST_FROM_ADDRESS raises a clear "email not configured" error (it never
    # crashes at import, and the fake adapter needs none of these). The password
    # is never logged.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""

    # The From: address the weekly digest is sent from (required by the smtp
    # adapter). Harmless dev default; set a real address in any environment that
    # actually sends.
    DIGEST_FROM_ADDRESS: str = "ballast@localhost"

    # Base URL the one-click unsubscribe link is built on. The link points at the
    # backend's GET /api/digest/unsubscribe. Defaults to local dev.
    DIGEST_UNSUBSCRIBE_BASE_URL: str = "http://localhost:8000"

    # --- Decisions history scale / Story 6.6 ---------------------------------

    # Default page size for the paginated GET /api/coach/decisions history read,
    # and the hard upper cap a client may request (a larger ``limit`` is a 422,
    # never silently clamped). The composite (owner_id, co_signed_at) index keeps
    # the scoped/ordered/windowed SQL bounded and fast.
    DECISION_PAGE_SIZE: int = 50
    DECISION_MAX_PAGE_SIZE: int = 100

    # Retention window (days) for never-co-signed ``proposed`` decision records:
    # the cron-invoked ``python -m coach.prune_job`` deletes proposed rows older
    # than this. Cosigned (on-the-record) rows are immutable and never pruned.
    DECISION_PROPOSED_RETENTION_DAYS: int = 30

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
