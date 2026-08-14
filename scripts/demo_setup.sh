#!/usr/bin/env bash
#
# scripts/demo_setup.sh — idempotently provision the SAFE team-demo database.
#
# Makes `ballast_demo` a fully-loaded, realistic demo every time, so the story
# beats all land on FAKE data in a SEPARATE DB (your real `ballast` is untouched):
#   • schema (auto-provisioned by a brief app boot),
#   • 20 years of market history (cloned from your real `ballast` DB — powers the
#     Recovery Precedent beat; no Tiingo key / network needed), and
#   • a seeded demo account: portfolio + Balanced target + 2 co-signed decisions,
#     with the cash reserve left UNDECIDED so the live set-or-decline beat works.
#
# Idempotent + safe to re-run: it clones market history only if the demo DB has
# none, and seeds the account only if it isn't already seeded (so decisions don't
# pile up). Called automatically by scripts/demo.sh; runnable on its own too.
#
#   ./scripts/demo_setup.sh            # provision if missing (idempotent, default)
#   ./scripts/demo_setup.sh reset      # PUT IT BACK: after you've played with the
#                                       #   data, restore the clean demo baseline
#                                       #   (portfolio + Balanced target + 2 decisions
#                                       #   + $4k cash + reserve undecided). Keeps the
#                                       #   DB/schema/market history, so the running
#                                       #   app keeps working — just RELOAD the page.
#   ./scripts/demo_setup.sh fresh      # NUKE: drop + rebuild the whole demo DB
#                                       #   (re-clones market history; STOP the app
#                                       #   first — this drops the DB out from under it).
#
# Demo login →  demo@example.com  /  ballast-demo-2026

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/ballast/backend"
DEMO_DB="${DEMO_DB:-ballast_demo}"
SOURCE_DB="${SOURCE_DB:-ballast}"       # where 20y of market history already lives
DEMO_URL="postgresql://ballast:ballast@localhost:5432/$DEMO_DB"
DEMO_EMAIL="demo@example.com"

MODE="provision"
case "${1:-}" in
  reset|--reset)          MODE="reset" ;;
  fresh|--fresh|nuke)     MODE="fresh" ;;
  ""|provision)           MODE="provision" ;;
  -h|--help)
    sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown argument '$1' (use: reset | fresh | --help)"; exit 2 ;;
esac

psql_demo() { ( cd "$ROOT" && docker compose exec -T db psql -U ballast -d "$DEMO_DB" "$@" ); }

echo "▶ Provisioning demo DB '$DEMO_DB' (safe: fake data, separate DB)…"

# --- 1. Postgres up + demo DB exists -----------------------------------------
( cd "$ROOT" && (docker compose up -d --wait db 2>/dev/null || docker compose up -d db) ) \
  || { echo "✗ failed to start Postgres"; exit 1; }

# `fresh`: drop the whole demo DB first (--force terminates any app connections),
# so everything below rebuilds from zero, re-cloning market history.
if [ "$MODE" = "fresh" ]; then
  echo "  • dropping '$DEMO_DB' for a fresh rebuild (stop the app first if it's running)…"
  ( cd "$ROOT" && docker compose exec -T db dropdb -U ballast --force --if-exists "$DEMO_DB" ) \
    || echo "    (drop reported an error; continuing)"
fi

if ! psql_demo -tAc "SELECT 1" >/dev/null 2>&1; then
  echo "  • creating database '$DEMO_DB'…"
  ( cd "$ROOT" && docker compose exec -T db createdb -U ballast "$DEMO_DB" ) \
    || echo "    (createdb error — it may already exist; continuing)"
fi

# --- 2. Provision the schema (directly, in-process — no server, no port) ------
# The app's own idempotent create_all + startup migrations, run against the demo
# DB. Doing it in Python (not a throwaway uvicorn boot) avoids a subtle footgun:
# `uv run uvicorn …` spawns a CHILD process, so killing the `uv` wrapper can leave
# a zombie server squatting the port; a later rebuild's health check then hits the
# STALE server and wrongly concludes the (empty) DB is provisioned.
if ! psql_demo -tAc "SELECT to_regclass('public.market_daily')" 2>/dev/null | grep -q market_daily; then
  echo "  • creating schema…"
  ( cd "$BACKEND" && DATABASE_URL="$DEMO_URL" BROKER_ADAPTER=fake LLM_ADAPTER=fake \
      DECISION_MAINTENANCE_ENABLED=false MARKETDATA_INGEST_ENABLED=false \
      uv run python -c "
import asyncio
from db.session import create_db_and_tables, engine
from db.migrations import run_startup_migrations
async def _s():
    await create_db_and_tables()
    await run_startup_migrations(engine)
asyncio.run(_s())
" ) || { echo "✗ schema provisioning failed"; exit 1; }
fi

# --- 3. Clone 20y of market history from the real DB (only if empty) ----------
# Fails LOUDLY: ON_ERROR_STOP + a post-clone count check, so a missing table or a
# partial copy can never masquerade as success (which would silently kill the
# Recovery Precedent beat).
demo_rows="$(psql_demo -tAc "SELECT count(*) FROM market_daily" 2>/dev/null | tr -d '[:space:]')"
if [ "${demo_rows:-0}" = "0" ]; then
  src_rows="$( ( cd "$ROOT" && docker compose exec -T db psql -U ballast -d "$SOURCE_DB" \
      -tAc "SELECT count(*) FROM market_daily" ) 2>/dev/null | tr -d '[:space:]' )"
  if [ "${src_rows:-0}" = "0" ]; then
    echo "  ⚠️  '$SOURCE_DB' has no market history to clone — the Recovery Precedent"
    echo "      beat will be empty. Backfill it first (python -m marketdata.ingest …)."
  else
    echo "  • cloning market_daily from '$SOURCE_DB' ($src_rows rows)…"
    if ! ( cd "$ROOT" && docker compose exec -T db sh -c \
        "pg_dump -U ballast --data-only --table=market_daily $SOURCE_DB | psql -U ballast -d $DEMO_DB -q -v ON_ERROR_STOP=1" ); then
      echo "✗ market_daily clone failed"; exit 1
    fi
    got="$(psql_demo -tAc "SELECT count(*) FROM market_daily" 2>/dev/null | tr -d '[:space:]')"
    if [ "${got:-0}" -lt "$src_rows" ] 2>/dev/null; then
      echo "✗ clone incomplete: expected $src_rows rows, got ${got:-0}"; exit 1
    fi
    echo "    cloned $got rows ✓"
  fi
else
  echo "  • market history already present ($demo_rows rows) — skipping clone."
fi

# --- 3b. `reset`: wipe the demo account's mutable state (played-with data) ----
# Clears decisions, cash config (reserve + parked tags), and any pending buys for
# the demo user, then falls through to the seed below (which re-links → re-imports
# the demo portfolio, re-sets the Balanced target, and re-seeds exactly 2 decisions).
# Keeps schema + market history, so a running app keeps its connection — just reload.
if [ "$MODE" = "reset" ]; then
  rid="$(psql_demo -tAc "SELECT id FROM \"user\" WHERE email='$DEMO_EMAIL'" 2>/dev/null | tr -d '[:space:]')"
  if [ -n "$rid" ]; then
    echo "  • reset: clearing played-with data for ${DEMO_EMAIL}…"
    psql_demo -c "
      DELETE FROM decision_record WHERE owner_id='$rid';
      DELETE FROM cash_config     WHERE owner_id='$rid';
      DELETE FROM pending_buy     WHERE owner_id='$rid';
    " >/dev/null 2>&1
  fi
fi

# --- 4. Seed the demo account (only if not already seeded) -------------------
seeded="$(psql_demo -tAc \
  "SELECT count(*) FROM decision_record dr JOIN \"user\" u ON u.id = dr.owner_id WHERE u.email = '$DEMO_EMAIL'" \
  2>/dev/null | tr -d '[:space:]')"
if [ "${seeded:-0}" -ge 2 ] 2>/dev/null; then
  echo "  • demo account already seeded ($seeded decisions) — skipping seed."
else
  echo "  • seeding demo account (portfolio + Balanced target + 2 decisions)…"
  ( cd "$BACKEND" && DATABASE_URL="$DEMO_URL" uv run python "$ROOT/scripts/demo_seed.py" ) \
    | grep -E "✓|✗|Dry run|Demo login" || true
  duid="$(psql_demo -tAc "SELECT id FROM \"user\" WHERE email='$DEMO_EMAIL'" 2>/dev/null | tr -d '[:space:]')"
  if [ -n "$duid" ]; then
    # Leave the reserve UNDECIDED so the live set-or-decline beat works on stage,
    # AND restore the $4,000 cash baseline: the 2 seeded co-signs are MARKET buys
    # totalling exactly $4,000, and the debit-after-fill logic (Story 10.12)
    # correctly spends that cash — which would leave the demo showing $0 ready-to-
    # trade. The seeded decisions are just history for the Decisions tab; reset the
    # cash so the deploy-my-cash story still has its $4,000 to work with.
    psql_demo -c "
      DELETE FROM cash_config WHERE owner_id='$duid';
      UPDATE portfolio_balance SET cash='4000.00' WHERE owner_id='$duid';
      UPDATE portfolio_cache   SET cash='4000.00' WHERE owner_id='$duid';
    " >/dev/null 2>&1
  fi
fi

echo "✅ Demo DB '$DEMO_DB' ready.  Login →  demo@example.com  /  ballast-demo-2026"
