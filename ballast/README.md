# Ballast

An AI investing coach on top of Charles Schwab — a Vite/React SPA (presentation-only) backed by a FastAPI modular monolith and PostgreSQL. This is the app source root; BMad planning/tracking artifacts live under `../_bmad-output/`.

- `frontend/` — Vite 8 + React 19 SPA (presentation only; holds no business logic). See [frontend/README.md](frontend/README.md).
- `backend/` — FastAPI (Python 3.12+) modular monolith; owns all logic. See [backend/README.md](backend/README.md).

## Run it locally (three steps)

From the repository root (`..`):

**1. Start PostgreSQL 18**
```bash
docker compose up -d db      # postgres:18 on localhost:5432 (db/user/pw: ballast)
```

**2. Start the backend API** (port 8000)
```bash
cd ballast/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
cp .env.example .env         # adjust if needed; real .env is gitignored
uvicorn api.main:app --host 0.0.0.0 --port 8000
# verify: curl http://localhost:8000/api/health  ->  {"status":"ok","db":"ok"}
```

**3. Start the frontend dev server** (port 5173)
```bash
cd ballast/frontend
npm install
cp .env.example .env         # VITE_API_BASE_URL defaults to http://localhost:8000
npm run dev
```

Open http://localhost:5173 — the Dashboard shows the live backend/db health, confirming the SPA reaches the API.

## Tests
```bash
cd ballast/backend  && .venv/bin/pytest          # requires the db container running
cd ballast/frontend && npm test && npm run lint:css
```

## Conventions
- The React SPA is **presentation-only**; all decision logic and invariant enforcement live server-side (architecture AD-1).
- All UI values come from CSS-variable design tokens in `frontend/src/theme/tokens.css` (theme `ballast-terminal`); stylelint fails the build on hardcoded colors/font-sizes.
- Backend module naming: interfaces suffix `Port`, adapters suffix `Adapter`; secrets come from the environment and are never committed or logged.
