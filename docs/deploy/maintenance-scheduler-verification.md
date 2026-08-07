# Deploy verification — decisions-maintenance scheduler

**Purpose:** confirm the in-process **decisions-maintenance scheduler** (Story 7.2 +
pre-unattended-prod hardening) actually runs in the deployed environment. It
periodically (a) **reclaims** crash-orphaned `cosigning` decision records — a row
that may carry a live order and would otherwise strand forever — and (b) **prunes**
stale never-co-signed `proposed` records. It's built, on by default, and covered
by tests/dev; this checklist is the one thing that can only be confirmed on a real
deploy (Epic 7 action item, 2026-08-07).

Code: `coach/maintenance.py` (`MaintenanceScheduler`, `run_maintenance_once`),
started in the FastAPI lifespan (`api/app.py`). Loggers: `ballast.api` (startup) and
`ballast.coach.maintenance` (loop).

---

## 0. Prerequisite — log level must be INFO

The startup and per-tick confirmations are **INFO** logs. If the deployment runs at
`WARNING`+, you'll only see failures, not the healthy ticks. Set the app log level to
`INFO` for this verification (failure logs are `ERROR`/exception and show regardless).

## 1. Confirm the settings

Check the deployed config resolves to (defaults shown; override via env only if you
mean to):

| Setting | Default | Meaning |
|---|---|---|
| `DECISION_MAINTENANCE_ENABLED` | `true` | master switch — must be `true` in prod |
| `DECISION_MAINTENANCE_INTERVAL_SECONDS` | `600` | seconds between ticks (loop sleeps this long **before** the first tick) |
| `DECISION_COSIGNING_RECLAIM_AFTER_SECONDS` | `900` | age a `cosigning` row must exceed to be treated as an orphan — must stay comfortably longer than a real approve→place→cosign round-trip |
| `DECISION_RECLAIM_BATCH_LIMIT` | `100` | max orphans reclaimed per tick |
| `DECISION_PROPOSED_RETENTION_DAYS` | `30` | age at which stale `proposed` rows are pruned |

## 2. Startup check (on boot)

In the boot logs you should see **both**:

```
decisions_maintenance_scheduler_enabled interval_s=600          # logger: ballast.api
maintenance_scheduler_started interval_s=600 reclaim_after=0:15:00 retention_days=30 batch_limit=100   # logger: ballast.coach.maintenance
```

- If you see **neither** and `DECISION_MAINTENANCE_ENABLED=true`, the scheduler didn't
  start — investigate before relying on unattended recovery.
- `reclaim_after=0:15:00` is the 900s window rendered as `H:MM:SS` (expected).

## 3. Tick check (the main confirmation)

**Gotcha:** the loop **sleeps one full interval before its first tick** (no DB work at
boot). At the default 600s that means the first tick appears **~10 minutes after boot**.
You're looking for:

```
maintenance_tick reclaimed=0 pruned=0        # logger: ballast.coach.maintenance
```

`reclaimed=0 pruned=0` is the **healthy** result on a clean deployment — it means the
tick ran and found nothing to do.

**Fast path (recommended for verification):** temporarily set
`DECISION_MAINTENANCE_INTERVAL_SECONDS=60`, boot, wait ~1 minute, confirm one
`maintenance_tick …` line appears, then **revert to 600** and redeploy. (Don't leave a
tiny interval in prod.)

## 4. Shutdown check (optional)

On a clean shutdown/redeploy you should see:

```
maintenance_scheduler_stopped                # logger: ballast.coach.maintenance
```

Confirms the loop stops cooperatively (it isn't killed mid-tick).

## 5. Failure signals — what they mean

The loop is designed to **survive any tick error** so it never silently dies. If you see:

```
maintenance reclaim step failed; will retry next tick
maintenance prune step failed; will retry next tick
maintenance_tick failed; loop continues (retry next interval)
```

…a single tick hit an error (e.g. a DB blip) but the loop is still alive and will retry
next interval. **One-off:** fine. **Repeating every interval:** investigate (DB
connectivity, migrations, credentials) — recovery isn't happening.

## 6. Notes

- **Single-instance assumption:** the app assumes one instance. If you run more than
  one, each starts its own scheduler — that's **safe** (reclaim + prune are idempotent
  and rowcount-gated), you'll just see ticks from each instance.
- **Functional depth:** proving a reclaim actually recovers an orphan is covered by the
  test suite (`test_recoverable_placement.py`); the deploy check only needs to confirm
  the loop is **alive and ticking** in the real environment.

## 7. Sign-off

Verified when, in the deployed environment:

- [ ] `decisions_maintenance_scheduler_enabled` + `maintenance_scheduler_started` seen at boot
- [ ] at least one `maintenance_tick reclaimed=… pruned=…` observed
- [ ] no repeating `…failed…` lines
- [ ] interval reverted to the intended prod value (600s) if changed for the fast path

Record the date + who verified in the Epic 7 action-items entry
(`sprint-status.yaml`) and flip that item to `done`.
