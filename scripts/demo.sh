#!/usr/bin/env bash
#
# scripts/demo.sh — SAFE TEAM DEMO.
#
# Runs the full Ballast experience (Coach → deploy plan → advisor narration →
# Approve & Co-sign → filled) on FAKE account data, in a SEPARATE database, so:
#   • no real Schwab / personal financial data is ever shown, and
#   • no real trade can ever be placed (the fake broker is forced — /approve
#     "places" against the fake adapter, never Schwab).
#
# It reuses scripts/dev.sh (which hard-forces BROKER_ADAPTER=fake). The only
# differences from `./scripts/dev.sh` are: a dedicated demo database (your real
# `ballast` DB is never touched) and DEMO_PORTFOLIO=1 (a richer, presentation
# portfolio: an all-US position + a couple single stocks + a few thousand cash,
# so the deploy tells the clean "all-US → buy Bonds & International" story).
#
#   ./scripts/demo.sh                          # fake broker, fake data, demo DB
#   LLM_ADAPTER=anthropic ./scripts/demo.sh    # + real AI coaching narration (needs ANTHROPIC_API_KEY)
#
# Demo flow in the app (all fake): register a throwaway account → Connect Schwab
# (completes in-browser, imports the demo portfolio) → set a cash cushion + pick a
# target mix (e.g. Balanced) → Coach → "Deploy your cash toward your target" →
# Ask the coach → Approve & Co-sign → filled. Nothing real happens.

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEMO_DB="${DEMO_DB:-ballast_demo}"

echo "▶ Ballast TEAM DEMO"
echo "    fake broker + fake data, separate DB '$DEMO_DB' — your real 'ballast' DB is UNTOUCHED."
echo

# --- 1. Postgres up, then ensure the demo database exists (safe to re-run) ----
echo "▶ Starting Postgres…"
( cd "$ROOT" && (docker compose up -d --wait db 2>/dev/null || docker compose up -d db) ) \
  || { echo "✗ failed to start Postgres"; exit 1; }

if ! ( cd "$ROOT" && docker compose exec -T db \
        psql -U ballast -tAc "SELECT 1 FROM pg_database WHERE datname='$DEMO_DB'" 2>/dev/null ) \
      | grep -q 1; then
  echo "▶ Creating demo database '$DEMO_DB'…"
  ( cd "$ROOT" && docker compose exec -T db createdb -U ballast "$DEMO_DB" ) \
    || echo "    (createdb error — it may already exist; continuing)"
fi
# (The app auto-provisions all tables on first boot: create_all + startup migrations.)

# --- 2. Point the app at the demo DB + the richer demo portfolio, then run ----
#     scripts/dev.sh FORCES the fake broker, so no real money is ever possible.
export DATABASE_URL="postgresql://ballast:ballast@localhost:5432/$DEMO_DB"
export DEMO_PORTFOLIO=1
echo "▶ Launching the app (fake broker) against '$DEMO_DB' with the demo portfolio…"
echo
exec "$ROOT/scripts/dev.sh"
