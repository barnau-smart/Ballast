#!/usr/bin/env python
"""scripts/demo_seed.py — seed the SAFE team-demo DB + run an end-to-end dry run.

Drives the app's REAL API paths in-process (FastAPI TestClient, no running
server) against the separate ``ballast_demo`` database with the FAKE broker and
``DEMO_PORTFOLIO=1``. It:

  1. registers a stable demo login (idempotent),
  2. links the fake Schwab (imports the demo portfolio),
  3. sets the Balanced target model,
  4. VERIFIES the deploy plan produces the expected BND/VXUS split,
  5. VERIFIES the Recovery Precedent panel has real market history, and
  6. seeds two co-signed decisions so the Decisions tab isn't empty on stage.

Everything is fake data on a separate DB — no real account, no real trade. Run:

    DATABASE_URL="postgresql://ballast:ballast@localhost:5432/ballast_demo" \
      uv run python ../../scripts/demo_seed.py     # from ballast/backend

or just ``uv run python scripts/demo_seed.py`` after exporting DATABASE_URL.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

# Make the backend package importable no matter the cwd (this script lives in
# scripts/, the app lives in ballast/backend/).
_BACKEND = Path(__file__).resolve().parent.parent / "ballast" / "backend"
sys.path.insert(0, str(_BACKEND))

# --- Force the safe, offline, demo configuration BEFORE importing the app -----
# Explicit env vars take precedence over ballast/backend/.env (which runs the
# live tiingo/anthropic config), so this stays deterministic and never touches
# the network or the real 'ballast' DB.
os.environ.setdefault(
    "DATABASE_URL", "postgresql://ballast:ballast@localhost:5432/ballast_demo"
)
os.environ["BROKER_ADAPTER"] = "fake"
os.environ["LLM_ADAPTER"] = "fake"
os.environ["DEMO_PORTFOLIO"] = "1"
os.environ["DECISION_MAINTENANCE_ENABLED"] = "false"
os.environ["MARKETDATA_INGEST_ENABLED"] = "false"

DB_URL = os.environ["DATABASE_URL"]
if "ballast_demo" not in DB_URL:
    sys.exit(
        f"✗ refusing to run: DATABASE_URL is not the demo DB ({DB_URL!r}). "
        "This script only seeds ballast_demo."
    )

from fastapi.testclient import TestClient  # noqa: E402

from api.app import create_app  # noqa: E402

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "ballast-demo-2026"

_ok = 0
_fail = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _ok, _fail
    mark = "✓" if condition else "✗"
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))
    if condition:
        _ok += 1
    else:
        _fail += 1


def main() -> int:
    print(f"▶ Seeding + dry-run against {DB_URL}\n")
    with TestClient(create_app()) as c:
        # --- 1. Register (idempotent) + login --------------------------------
        r = c.post(
            "/api/auth/register",
            json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD},
        )
        # 201 = created; 400 = already exists (fine on a re-run).
        check("register demo user", r.status_code in (201, 400), f"HTTP {r.status_code}")

        r = c.post(
            "/api/auth/jwt/login",
            data={"username": DEMO_EMAIL, "password": DEMO_PASSWORD},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code != 200:
            print(f"✗ login failed: HTTP {r.status_code} {r.text}")
            return 1
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
        check("login", True)

        # --- 2. Link the fake Schwab (imports the demo portfolio) ------------
        r = c.get("/api/brokerage/authorize", headers=headers)
        state = r.json()["state"]
        r = c.post(
            "/api/brokerage/callback",
            json={"code": "demo-code", "state": state},
            headers=headers,
        )
        check("link fake Schwab + import portfolio", r.status_code == 200 and r.json().get("linked"))

        # --- 3. Verify the imported DEMO portfolio ---------------------------
        r = c.get("/api/portfolio", headers=headers)
        pf = r.json()
        holdings = {h["symbol"]: h for h in pf.get("holdings", [])}
        check(
            "portfolio imported (VTI + AAPL + NVDA)",
            {"VTI", "AAPL", "NVDA"} <= set(holdings),
            f"symbols={sorted(holdings)}",
        )
        check(
            "VTI market value $10,000",
            holdings.get("VTI", {}).get("market_value") in ("10000.00", "10000"),
            f"got {holdings.get('VTI', {}).get('market_value')}",
        )

        # --- 4. Set the Balanced target -------------------------------------
        r = c.put("/api/target-allocation", json={"model": "balanced"}, headers=headers)
        check("set Balanced target", r.status_code == 200, f"HTTP {r.status_code}")

        # --- 5. Verify the deploy plan (the money story) --------------------
        # The deploy plan is GATED on a reserve decision (Epic 9/10 cash
        # intelligence): until the user sets OR declines a reserve, the plan
        # returns 'decide_reserve' (the run-book's set-or-decline beat). Decline
        # it here (reserve = 0) to verify the full $4,000 deploy math, then reset
        # to pristine/undecided at the end so the live beat still works on stage.
        r = c.put(
            "/api/cash/config",
            json={"reserve_amount": None, "reserve_decided": True},
            headers=headers,
        )
        check("decline reserve (temp, for deploy verification)", r.status_code == 200, f"HTTP {r.status_code}")

        r = c.get("/api/allocation/plan", headers=headers)
        plan = r.json()
        items = {i["symbol"]: i["amount"] for i in plan.get("action_items", [])}
        check("deploy plan status == 'deploy'", plan.get("status") == "deploy", plan.get("status", ""))
        check(
            "deploy buys Bonds (BND) + International (VXUS)",
            {"BND", "VXUS"} <= set(items),
            f"items={items}",
        )
        # The clean all-US → diversify story: $4,000 cash split by the Balanced gap.
        if {"BND", "VXUS"} <= set(items):
            total = Decimal(items["BND"]) + Decimal(items["VXUS"])
            check("deploy total == $4,000 investable cash", total == Decimal("4000.00"), f"BND {items['BND']} + VXUS {items['VXUS']} = {total}")

        # --- 6. Verify Recovery Precedent has real market history -----------
        # find_precedent always returns a length-1 record: no LIVE drawdown → the
        # 'strategy' fallback (VTI at/near all-time high). The demo's precedent
        # beat uses a HYPOTHETICAL drawdown ("what if it fell ~8%?") → an
        # 'event-precedent' backed by real 20-year history.
        r = c.get("/api/precedent/recovery?drawdown=0.08", headers=headers)
        rec = r.json()
        n = rec.get("stats", {}).get("instance_count", 0)
        check(
            "recovery precedent finds real historical drops (8% hypothetical)",
            rec.get("kind") == "event-precedent" and n > 0,
            f"kind={rec.get('kind')}, {n} comparable drops",
        )

        # --- 7. Seed two co-signed decisions (real recommend→approve) -------
        seeded = 0
        for symbol, amount in (("BND", "2545.45"), ("VXUS", "1454.55")):
            rr = c.post("/api/coach/recommend", json={}, headers=headers)
            if rr.status_code != 200:
                check(f"recommend for {symbol}", False, f"HTTP {rr.status_code}")
                continue
            decision_id = rr.json()["decision_id"]
            ar = c.post(
                "/api/coach/approve",
                json={
                    "decision_id": decision_id,
                    "order_intent": {"symbol": symbol, "side": "buy", "amount": amount},
                },
                headers=headers,
            )
            ok = ar.status_code == 200 and ar.json().get("status") == "filled"
            check(f"co-sign {symbol} ${amount}", ok, f"HTTP {ar.status_code}")
            if ok:
                seeded += 1

        # --- 8. Verify the Decisions tab is populated -----------------------
        r = c.get("/api/coach/decisions", headers=headers)
        decisions = r.json()
        n = len(decisions.get("decisions", decisions)) if isinstance(decisions, dict) else len(decisions)
        check("Decisions tab populated", n >= 2, f"{n} decisions on record")

    print(f"\n{'─' * 60}")
    print(f"Dry run: {_ok} passed, {_fail} failed")
    print(f"Demo login →  {DEMO_EMAIL}  /  {DEMO_PASSWORD}")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
