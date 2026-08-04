---
status: blocked
---

> **Renumber note (2026-08-02):** This result predates the insertion of the read-only pre-flight harness. The real-money gate it describes was **Story 7.6** at the time of writing and is now **Story 7.7**; the new **Story 7.6** is `7-6-live-preflight-payload-shape-harness` (read-only, zero orders), which runs first and de-risks this gate. References to "Story 7.6" below mean this (now 7.7) real-money gate. The runbook is unchanged and still accurate.

# BMad Dev Auto Result

Status: blocked

> **Re-verified 2026-08-04 (fresh `/bmad-dev-auto 7-7` invocation):** working tree clean on `main`; no `.env` and no `SCHWAB_*`/`ANTHROPIC_API_KEY`/`TOKEN_ENCRYPTION_KEY` in the environment; `sprint-status.yaml` shows `7-7-gated-live-exercise-real-money: backlog` and `7-6` preflight `done`; epics.md still marks the story **"Human pause point — not a loop task."** No offline code slice exists to build. Remains blocked — unchanged. The gate has not been crossed.
>
> **Re-verified 2026-08-03 (fresh `/bmad-dev-auto 7-7` invocation):** working tree clean on `main`; no `.env` and no `SCHWAB_*`/`ANTHROPIC_API_KEY` in the environment; `sprint-status.yaml` shows `7-7-gated-live-exercise-real-money: backlog` and `7-6` preflight `done`; epics.md still marks the story **"Human pause point — not a loop task."** No offline code slice exists to build. Remains blocked — unchanged.
Blocking condition (re-confirmed 2026-08-03, Story 7.6 now `done`): Story 7.7 is the credential- and real-money human gate — it cannot be run by the autonomous loop. It requires real `SCHWAB_*` + `ANTHROPIC_API_KEY` + a **funded** Schwab account, places a **real order with real money** against live Schwab, and contains a **reserved product decision** (partial-fill terminality) that is MasterB's to make. Every acceptance criterion needs live real-world I/O that cannot be executed or tested offline. The loop must not fabricate live payload results or place a real trade. Handing to MasterB to run manually.

## Why this halts (not a failure — the designed pause point)

Story 7.6's own text: *"Human pause point — not a loop task. Requires real `SCHWAB_*` + `ANTHROPIC_API_KEY` + a funded Schwab account."* This matches the standing build mandate: run the loop autonomously, **pause only at real blockers (creds / product decisions / live trades)**. Story 7.6 is all three at once. There is no offline code deliverable to complete first — the whole story *is* the live exercise plus confirming the guessed payload shapes against what actually comes back.

## Readiness check — the gate is fully open (7.1–7.6 all landed)

All prerequisites are `done` in `sprint-status.yaml` and the code seams they harden are in place. **7.7 is now the sole remaining story in Epic 7 before retrospective:**

| Story | What it guarantees for the live run | Status |
|------|--------------------------------------|--------|
| 7.1 | Prod DB migration path — the unique index + queryable columns actually exist on a provisioned DB (idempotent) | done |
| 7.2 | Generalized atomic-claim primitive — no double-place, no stranded order, `broker_ref` persisted in the placement claim | done |
| 7.3 | Calm 409 reconnect envelopes + provider-match on the live broker seams (never a raw 500) | done |
| 7.4 | LLM client built once, tight request timeout — a hung call degrades to the default plan in seconds | done |
| 7.5 | Off-event-loop reads + explicit multi-account selection (a taxable buy can't land in an IRA) + re-link clears projection | done |
| 7.6 | Read-only pre-flight harness — confirms 5 of 6 guessed payload shapes (token, account-numbers, balance/positions, quote, LLM structured-output) with **zero orders** before any money moves | done |

The de-risking pre-flight harness offered in the prior recommendation has since been built, reviewed, and merged (commit `5d21829`). Run **7.6's read-only live pass first** to catch payload drift on the 5 read seams, then run 7.7's single real order (which is the only way to confirm the remaining order-status/fill `_map_order` seam).

Working tree is clean, on `main`. No `.env` and no real creds are present locally — confirming the gate has not been crossed.

## Runbook — the one live pass (MasterB runs this manually)

**Preconditions**
1. Schwab developer app approved; `SCHWAB_CLIENT_ID` / `SCHWAB_CLIENT_SECRET` / `SCHWAB_CALLBACK_URL` in hand (the Schwab API approval is the long-pole — set this up first).
2. A **funded** Schwab account with **enough cash to buy ≥ 1 whole share** of a broad index ETF (sizing is `floor(amount / ask)`; it refuses calmly below one share — so pick `amount` ≥ one share's price, e.g. a low-priced broad ETF, to keep the real spend small).
3. A paid `ANTHROPIC_API_KEY`.
4. Set env: `LLM_ADAPTER=anthropic`, `BROKER_ADAPTER=schwab`, plus `TOKEN_ENCRYPTION_KEY`, the `SCHWAB_*` vars, and `ANTHROPIC_API_KEY`. Never commit these.

**The pass (behind the existing gates)**
1. **Link** the funded account via the Schwab OAuth flow; if the login exposes more than one account, make the explicit account selection (7.5 refuses a silent `accounts[0]`).
2. **`/refresh`** — import real cash + positions. Confirm the cash-only mapping shows true idle cash and the missed-growth meter reads honestly (6.5).
3. **`/recommend`** — a real paid Anthropic structured-output call returns a blessed recommendation (or degrades honestly to the default plan). Watch latency stays inside the 7.4 envelope.
4. **`/approve`** — one **small** in-scope whole-share market order → placed exactly once → reconciled → co-signed with a truthful `OrderOutcome`.
5. **Confirm the guessed live JSON field mappings** (order-status, token, balance payloads) against the **real** Schwab responses; fix any drift found. This is the core purpose of the exercise.

**The product decision to make and encode during this pass**
- **Partial-fill terminality (carried from 6.7):** decide whether a `partial` fill is terminal, or whether a partial that later completes may be re-reconciled to `filled`. Encode the chosen behavior. *(This is the one decision reserved for you — the loop deliberately did not guess it.)*

**Safety rails already in force during the run**
- Never double-place / never phantom-fill; on any ambiguous placement the system stays `pending` and asks for explicit human re-confirmation (Schwab honors no client idempotency key).
- Calm 409 reconnect envelope on token-decrypt / config-auth / provider-mismatch faults (7.3) — never a raw 500.

## After the run

- Record the confirmed payload shapes + any drift fixes as a follow-up code change (that fix *is* loop-runnable once you've captured the real payloads).
- Mark `7-6-gated-live-exercise-real-money: done` in `sprint-status.yaml` and close the three open Epic 6 action items it exercises (go-live spike, partial-fill decision, ledger sweep).
- Then "the Coach works against a live LLM and a live broker" is a proven fact, and Epic 7 can go to retrospective.

## Recommendation

Do not attempt to autonomate this. When your Schwab creds + a funded account are ready, run the pass above yourself and capture the real payloads. If you'd rather I first build a **guarded pre-flight harness** (a read-only dry-run that exercises `/refresh` + `/recommend` and dumps the live payload shapes for drift comparison **without placing an order**), say so — that piece *is* loop-safe and I can spec + build it as a separate story to de-risk the real trade. Otherwise this stays paused until you run it.
