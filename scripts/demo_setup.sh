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
#   ./scripts/demo_setup.sh
#
# Demo login →  demo@example.com  /  ballast-demo-2026

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/ballast/backend"
DEMO_DB="${DEMO_DB:-ballast_demo}"
SOURCE_DB="${SOURCE_DB:-ballast}"       # where 20y of market history already lives
DEMO_URL="postgresql://ballast:ballast@localhost:5432/$DEMO_DB"

psql_demo() { ( cd "$ROOT" && docker compose exec -T db psql -U ballast -d "$DEMO_DB" "$@" ); }

echo "▶ Provisioning demo DB '$DEMO_DB' (safe: fake data, separate DB)…"

# --- 1. Postgres up + demo DB exists -----------------------------------------
( cd "$ROOT" && (docker compose up -d --wait db 2>/dev/null || docker compose up -d db) ) \
  || { echo "✗ failed to start Postgres"; exit 1; }
if ! psql_demo -tAc "SELECT 1" >/dev/null 2>&1; then
  echo "  • creating database '$DEMO_DB'…"
  ( cd "$ROOT" && docker compose exec -T db createdb -U ballast "$DEMO_DB" ) \
    || echo "    (createdb error — it may already exist; continuing)"
fi

# --- 2. Provision the schema (a brief, throwaway backend boot) ---------------
# The app auto-provisions all tables on startup (create_all + migrations). Boot
# it briefly against the demo DB with the FAKE broker + fake LLM (no creds, no
# network), wait for health, then stop it — the tables persist.
if ! psql_demo -tAc "SELECT to_regclass('public.market_daily')" 2>/dev/null | grep -q market_daily; then
  echo "  • creating schema (brief app boot)…"
  ( cd "$BACKEND" && DATABASE_URL="$DEMO_URL" BROKER_ADAPTER=fake LLM_ADAPTER=fake \
      DECISION_MAINTENANCE_ENABLED=false MARKETDATA_INGEST_ENABLED=false \
      uv run uvicorn api.main:app --port 8019 >/tmp/ballast_demo_provision.log 2>&1 ) &
  boot_pid=$!
  for _ in $(seq 1 45); do
    curl -sf http://localhost:8019/api/health >/dev/null 2>&1 && break
    kill -0 "$boot_pid" 2>/dev/null || { echo "✗ provisioning boot exited — see /tmp/ballast_demo_provision.log"; exit 1; }
    sleep 1
  done
  kill "$boot_pid" 2>/dev/null || true
  wait "$boot_pid" 2>/dev/null || true
fi

# --- 3. Clone 20y of market history from the real DB (only if empty) ----------
demo_rows="$(psql_demo -tAc "SELECT count(*) FROM market_daily" 2>/dev/null | tr -d '[:space:]')"
if [ "${demo_rows:-0}" = "0" ]; then
  src_rows="$( ( cd "$ROOT" && docker compose exec -T db psql -U ballast -d "$SOURCE_DB" \
      -tAc "SELECT count(*) FROM market_daily" ) 2>/dev/null | tr -d '[:space:]' )"
  if [ "${src_rows:-0}" = "0" ]; then
    echo "  ⚠️  '$SOURCE_DB' has no market history to clone — the Recovery Precedent"
    echo "      beat will be empty. Backfill it first (python -m marketdata.ingest …)."
  else
    echo "  • cloning market_daily from '$SOURCE_DB' ($src_rows rows)…"
    ( cd "$ROOT" && docker compose exec -T db sh -c \
        "pg_dump -U ballast --data-only --table=market_daily $SOURCE_DB | psql -U ballast -d $DEMO_DB -q" ) \
      >/dev/null 2>&1 || echo "    (clone reported an error; check manually)"
  fi
else
  echo "  • market history already present ($demo_rows rows) — skipping clone."
fi

# --- 4. Seed the demo account (only if not already seeded) -------------------
seeded="$(psql_demo -tAc \
  "SELECT count(*) FROM decision_record dr JOIN \"user\" u ON u.id = dr.owner_id WHERE u.email = 'demo@example.com'" \
  2>/dev/null | tr -d '[:space:]')"
if [ "${seeded:-0}" -ge 2 ] 2>/dev/null; then
  echo "  • demo account already seeded ($seeded decisions) — skipping seed."
else
  echo "  • seeding demo account (portfolio + Balanced target + 2 decisions)…"
  ( cd "$BACKEND" && DATABASE_URL="$DEMO_URL" uv run python "$ROOT/scripts/demo_seed.py" ) \
    | grep -E "✓|✗|Dry run|Demo login" || true
  # Leave the reserve UNDECIDED so the live set-or-decline beat works on stage.
  duid="$(psql_demo -tAc "SELECT id FROM \"user\" WHERE email='demo@example.com'" 2>/dev/null | tr -d '[:space:]')"
  [ -n "$duid" ] && psql_demo -c "DELETE FROM cash_config WHERE owner_id='$duid';" >/dev/null 2>&1
fi

echo "✅ Demo DB '$DEMO_DB' ready.  Login →  demo@example.com  /  ballast-demo-2026"
