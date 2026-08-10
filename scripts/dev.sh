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
# REAL-BROKER MODE also serves the frontend over https at https://127.0.0.1
# (port 443) instead of http://localhost:5173. That is the EXACT callback URL
# registered with Schwab (https://127.0.0.1/callback), so the real OAuth link
# completes IN the app — no Schwab URL change, no code-paste helper. Binding
# port 443 is privileged, so this prompts once for your sudo password, and the
# browser shows a one-time self-signed-cert warning on https://127.0.0.1.
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
# REAL_LINK_HTTPS=1 ⇒ serve the frontend over https on 127.0.0.1:443 (the real
# Schwab callback origin). Only real-broker mode needs it (the fake broker links
# in-app with no external redirect).
REAL_LINK_HTTPS=""
if [ "${BALLAST_REAL_BROKER:-}" = "1" ]; then
  export BROKER_ADAPTER=schwab
  REAL_LINK_HTTPS=1
  echo "    ⚠️  ⚠️  ⚠️  REAL-MONEY BROKER MODE — BROKER_ADAPTER=schwab  ⚠️  ⚠️  ⚠️"
  echo "        /approve will place REAL orders against your REAL Schwab account."
  echo "        Frontend will serve on https://127.0.0.1 (port 443) so the real"
  echo "        Schwab link completes in-app — this needs sudo (port 443)."
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
  if [ -n "$frontend_pid" ]; then
    # In real-link mode the frontend runs under sudo (port 443), so it's a root
    # process — a plain kill can't reap it. Try sudo first, then fall back.
    if [ -n "$REAL_LINK_HTTPS" ]; then
      sudo kill "$frontend_pid" 2>/dev/null || kill "$frontend_pid" 2>/dev/null || true
    else
      kill "$frontend_pid" 2>/dev/null || true
    fi
  fi
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
if [ -n "$REAL_LINK_HTTPS" ]; then
  echo "▶ Starting frontend → https://127.0.0.1  (port 443, needs sudo)"
  # Pre-authenticate sudo in the FOREGROUND first. A backgrounded `sudo` (below)
  # can't display its password prompt properly, so it would silently fail to bind
  # 443 and the page never loads — pre-caching the credential here fixes that.
  echo "    Enter your sudo password to bind port 443:"
  if ! sudo -v; then
    echo "✗ sudo authentication failed — cannot bind port 443 for https."
    echo "  (Re-run and enter your password, or use plain ./scripts/dev.sh for fake mode.)"
    exit 1
  fi
  # `env "PATH=$PATH"` re-exports the caller's PATH under sudo so node/npm resolve
  # (sudo resets PATH to a restricted secure_path by default). --mode httpslink
  # turns on https+443+/api-proxy in vite.config (via `define`). The backgrounded
  # sudo now reuses the cached credential — no prompt.
  ( cd "$FRONTEND" && exec sudo env "PATH=$PATH" npm run dev:httpslink ) &
  frontend_pid=$!
  FRONTEND_URL="https://127.0.0.1"
  # Probe the https frontend so a bind failure is LOUD, not a "page won't load".
  printf "    waiting for https frontend"
  frontend_ok=""
  for _ in $(seq 1 30); do
    if curl -k -sf https://127.0.0.1 >/dev/null 2>&1; then
      frontend_ok="yes"; printf " ✓\n"; break
    fi
    if ! kill -0 "$frontend_pid" 2>/dev/null; then
      printf " ✗ (frontend exited)\n"; break
    fi
    printf "."; sleep 1
  done
  [ -n "$frontend_ok" ] || echo "    ⚠️  https://127.0.0.1 did not come up — check the vite output above (port 443 in use? cert?)."
else
  echo "▶ Starting frontend → http://localhost:5173"
  ( cd "$FRONTEND" && exec npm run dev ) &
  frontend_pid=$!
  FRONTEND_URL="http://localhost:5173"
fi

# --- Up ----------------------------------------------------------------------
echo
echo "✅ Ballast is running:"
echo "     • App       $FRONTEND_URL"
echo "     • API       http://localhost:8000   (health: /api/health)"
echo "     • Postgres  localhost:5432"
if [ -n "$REAL_LINK_HTTPS" ]; then
  echo
  echo "   Real-broker mode: open $FRONTEND_URL (accept the one-time cert warning),"
  echo "   log in, then Connect Schwab — the real OAuth redirect lands back in-app."
fi
echo
echo "   Press Ctrl-C to stop the backend + frontend."
echo

# Wait on the app processes; the trap cleans them up on Ctrl-C / exit.
wait
