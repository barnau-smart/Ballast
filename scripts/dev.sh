#!/usr/bin/env bash
#
# scripts/dev.sh — run Ballast locally: Postgres + backend (:8000) + frontend (:5173).
#
# Runs fully offline by default (all adapters default to fakes — no credentials).
# SAFETY: this is a LOCAL PLAYGROUND. It ALWAYS runs the fake broker (no real
# money) — a stray `export BROKER_ADAPTER=schwab` in your shell will NOT arm real
# trades here. Real trading is opt-in ONLY via an explicit BALLAST_REAL_BROKER=1
# (this footgun bit us on 2026-08-10: a leftover export silently ran the "test"
# app in real-money mode).
#
#   ./scripts/dev.sh                          # fake broker (safe, offline)
#   LLM_ADAPTER=anthropic ./scripts/dev.sh    # real AI coaching (needs ANTHROPIC_API_KEY), still no money
#   BALLAST_REAL_BROKER=1 ./scripts/dev.sh    # ⚠️ REAL money path — /approve places REAL orders
#
# LLM_ADAPTER / MARKETDATA_ADAPTER are left to ballast/backend/.env unless you
# override them on the command line. Ctrl-C stops the backend + frontend;
# Postgres is left running (stop it with `docker compose down`).

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/ballast/backend"
FRONTEND="$ROOT/ballast/frontend"

# --- Broker safety: force the FAKE broker unless real trading is EXPLICITLY armed.
#     A leftover/stray BROKER_ADAPTER in the shell must never silently arm real
#     money in this playground — only BALLAST_REAL_BROKER=1 does.
echo "▶ Ballast local dev"
if [ "${BALLAST_REAL_BROKER:-}" = "1" ]; then
  export BROKER_ADAPTER=schwab
  echo "    ⚠️  ⚠️  ⚠️  REAL-MONEY BROKER MODE — BROKER_ADAPTER=schwab  ⚠️  ⚠️  ⚠️"
  echo "        /approve will place REAL orders against your REAL Schwab account."
  echo "        Press Ctrl-C NOW if you did not mean to do this."
else
  if [ "${BROKER_ADAPTER:-fake}" != "fake" ]; then
    echo "    note: ignoring inherited BROKER_ADAPTER='${BROKER_ADAPTER}' — this script"
    echo "          forces the fake broker for safety. For real trades, re-run with:"
    echo "          BALLAST_REAL_BROKER=1 ./scripts/dev.sh"
  fi
  export BROKER_ADAPTER=fake
  echo "    broker adapter : fake  (no real money — real trading needs BALLAST_REAL_BROKER=1)"
fi
echo "    llm adapter    : ${LLM_ADAPTER:-(from .env)}"
echo

# --- Prerequisites -----------------------------------------------------------
for cmd in docker uv npm curl; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "✗ required command not found on PATH: '$cmd'"
    exit 1
  fi
done

# --- Cleanup: stop backend + frontend on exit; leave Postgres running --------
backend_pid=""
frontend_pid=""
cleanup() {
  echo
  echo "▶ Stopping backend + frontend…"
  [ -n "$backend_pid" ]  && kill "$backend_pid"  2>/dev/null || true
  [ -n "$frontend_pid" ] && kill "$frontend_pid" 2>/dev/null || true
  echo "  (Postgres left running — 'docker compose down' to stop it.)"
}
trap cleanup INT TERM EXIT

# --- 1. Postgres -------------------------------------------------------------
echo "▶ Starting Postgres (docker compose up -d db)…"
( cd "$ROOT" && (docker compose up -d --wait db 2>/dev/null || docker compose up -d db) ) \
  || { echo "✗ failed to start Postgres"; exit 1; }

# --- 2. Backend on :8000 -----------------------------------------------------
echo "▶ Starting backend → http://localhost:8000"
( cd "$BACKEND" && exec uv run uvicorn api.main:app --port 8000 ) &
backend_pid=$!

printf "    waiting for backend health"
backend_ok=""
for _ in $(seq 1 45); do
  if curl -sf http://localhost:8000/api/health >/dev/null 2>&1; then
    backend_ok="yes"; printf " ✓\n"; break
  fi
  # bail early if the backend process already died
  if ! kill -0 "$backend_pid" 2>/dev/null; then
    printf " ✗ (backend exited)\n"; exit 1
  fi
  printf "."; sleep 1
done
[ -n "$backend_ok" ] || { printf " ✗ (timed out)\n"; exit 1; }

# --- 3. Frontend on :5173 ----------------------------------------------------
if [ ! -d "$FRONTEND/node_modules" ]; then
  echo "▶ Installing frontend deps (first run)…"
  ( cd "$FRONTEND" && npm install ) || { echo "✗ npm install failed"; exit 1; }
fi
if [ ! -f "$FRONTEND/.env" ] && [ -f "$FRONTEND/.env.example" ]; then
  cp "$FRONTEND/.env.example" "$FRONTEND/.env"
fi
echo "▶ Starting frontend → http://localhost:5173"
( cd "$FRONTEND" && exec npm run dev ) &
frontend_pid=$!

# --- Up ----------------------------------------------------------------------
echo
echo "✅ Ballast is running:"
echo "     • App       http://localhost:5173"
echo "     • API       http://localhost:8000   (health: /api/health)"
echo "     • Postgres  localhost:5432"
echo
echo "   Press Ctrl-C to stop the backend + frontend."
echo

# Wait on the app processes; the trap cleans them up on Ctrl-C / exit.
wait
