# Ballast Backend

FastAPI backend for **Ballast**, an AI investing coach app.

This is the greenfield scaffold: the API app, config, structured logging, a
consistent JSON error envelope, a live Postgres health check, and empty
skeleton packages for the domain modules (`coach`, `precedent`, `llm`,
`brokers`, `marketdata`, `digest`) that later epics fill in.

## Requirements

- Python 3.14+
- Docker (for local Postgres 18)

## Setup

All commands below are run from `ballast/backend/` unless noted.

### 1. Create & activate the virtualenv

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -e .            # runtime deps
pip install -e ".[test]"    # runtime + test deps (pytest, httpx)
```

(Or install the exact pins directly:
`pip install "fastapi==0.136.*" "uvicorn[standard]" "psycopg[binary]" pydantic-settings pytest httpx`.)

### 3. Configure environment

Copy `.env.example` to `.env` if you need to override defaults. The defaults
work out of the box against the docker Postgres below. **Never commit a real
`.env`.**

| Variable               | Default                                               | Purpose                          |
| ---------------------- | ----------------------------------------------------- | -------------------------------- |
| `DATABASE_URL`         | `postgresql://ballast:ballast@localhost:5432/ballast` | Postgres connection (psycopg v3) |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:5173`                               | Comma-separated CORS origins     |

## Start Postgres

Run from the **repo root** (where `docker-compose.yml` lives):

```bash
docker compose up -d db
```

Wait until the container is healthy (`docker compose ps` shows `healthy`).

## Run the server

From `ballast/backend/` with the venv active:

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

The API serves on **port 8000**. Check health:

```bash
curl -s http://localhost:8000/api/health
# {"status":"ok","db":"ok"}   when Postgres is reachable
# {"status":"degraded","db":"down"}   when it is not (still HTTP 200)
```

## Run tests

Start Postgres first (the DB-ok test is a real integration test), then:

```bash
pytest            # from ballast/backend/ with the venv active
```

## Conventions

- **Module naming:** interfaces are suffixed `Port` (e.g. `BrokerPort`);
  adapters implementing them are suffixed `Adapter` (e.g. `SchwabAdapter`).
- **Error envelope:** all API errors return
  `{"error": {"type": ..., "message": ...}}`. Reuse this shape in new routes.
- **Logging:** structured `key=value` lines. Never log secrets, tokens, or
  connection strings.
