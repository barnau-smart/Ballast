# Gate 3 — Attended real-money deploy runbook

The final readiness gate: place ONE small real deploy end-to-end
(propose → approve → place → reconcile) against your real Schwab account, you at the
keyboard, to prove the Epic 9/10 deploy path (and the new safety gates 10-9/10-10/10-12/10-13)
on real money. Optionally exercise one cost-switch. Keep the dollar amount tiny; you can sell
the share back in the Schwab app afterward.

> This is the ONLY thing that can't be done autonomously — it needs your hands + real creds.

## 0. Before you start (once)

- **Schwab creds in `ballast/backend/.env`:** `SCHWAB_CLIENT_ID`, `SCHWAB_CLIENT_SECRET`,
  `SCHWAB_CALLBACK_URL=https://127.0.0.1/callback` — the callback MUST match the URL registered
  in your Schwab developer app EXACTLY.
- **(Optional) real coaching narration:** `LLM_ADAPTER=anthropic` + `ANTHROPIC_API_KEY` in
  `.env`. Without it the deploy still works (deterministic math + templated narration).
- **⚠️ Do NOT run the backend test suite against this DB while linked.** The suite deletes
  `brokerage_token`; a session-start guard now REFUSES the run if a live link exists (override is
  `BALLAST_ALLOW_DIRTY_BROKERAGE_DB=1` — don't use it here). Just don't run `pytest` during the
  exercise.
- **Money is safe by construction:** `/approve` places only what you co-sign; Story 10-9 refuses
  any BUY beyond your real settled cash (never margin); Story 10-10 shows a calm margin-account
  note (your account is a margin account). Whole-share sizing; nothing auto-submits.

## 1. Arm real mode + start the app

```bash
cd /Users/blainearnau/repos/ai_practice_project
BALLAST_REAL_BROKER=1 ./scripts/dev.sh
# (or: LLM_ADAPTER=anthropic BALLAST_REAL_BROKER=1 ./scripts/dev.sh)
```

- It prints a loud REAL-MONEY banner and forces `BROKER_ADAPTER=schwab`.
- It serves the frontend over **https://127.0.0.1** (port 443 — the exact Schwab callback
  origin), so the OAuth link completes IN the app. It prompts once for your **sudo** password
  (to bind 443); the browser shows a one-time self-signed-cert warning — accept it.
- Backend on `http://localhost:8000` (`/api/health`), Postgres on 5432.

## 2. Link Schwab (in-app OAuth)

1. Open **https://127.0.0.1**, accept the cert warning, log in to Ballast.
2. **Connect Schwab** → the real Schwab OAuth redirect lands back in-app (no code-paste).
3. Linking imports your portfolio (a `reconcile_portfolio` runs), so your holdings + cash
   populate the dashboard.

## 3. Sanity-check the portfolio + safety surfaces

- Confirm the dashboard shows your real holdings and cash (~$12,182.82 settled + SWVXX parked).
- Open the Coach / deploy card — confirm the **calm margin-account note** appears (Story 10-10),
  since this is a margin account. That's expected and dismissible.

## 4. Keep the deploy SMALL (recommended)

The deploy engine deploys ALL your investable cash — which is large. To make the exercise tiny
and controlled, temporarily raise your **reserve (cash cushion)** so only a small amount is
investable:

- In **Settings → cash cushion / reserve**, set the reserve so `settlement + parked − reserve`
  is just a few hundred dollars (enough for ~1 whole share of the deploy's target fund, but no
  more). Example: with settlement ~$12,183 and SWVXX untagged/parked, set the reserve to leave
  ≈ $100–$300 investable.
- Alternatively, temporarily **un-tag SWVXX as parked** and set the reserve to ≈ your settlement
  cash minus a couple hundred dollars — then investable is just that couple hundred, funded
  entirely from settled cash (no money-market liquidation, no margin, simplest path).
- The deploy will BUY whole shares only; if investable is below one share of the target fund it
  refuses calmly ("less than one whole share") — nudge the reserve down a little and retry.

> Restore your real reserve / parked tags after the exercise (step 8).

## 5. Deploy → review → Approve & Co-sign

1. On the Coach card, click **"Deploy your cash toward your target"** — it fetches the
   deterministic plan and POPULATES a BUY (symbol/side/amount); it submits NOTHING yet.
2. Read the narration + the honest funding line (settled vs money-market split, reserve
   protected). Confirm the amount is the small figure you expect.
3. Click **Approve & Co-sign**. This is the real placement:
   `/approve` → Coach Engine → **real Schwab `place_order`** → reconcile.
4. **Watch the seam:** the card shows the true outcome — `filled` / `partial` (a real position),
   or `pending`/`timeout` (surfaced honestly, never a phantom fill). Only `filled`/`partial` is
   framed as a placed position.

If the amount ever exceeds your settled cash, Story 10-9 **refuses** it (calm 422) rather than
buying on margin — that's the gate working; lower the amount / free cash via the money-market
liquidation flow.

## 6. Verify placement + durable reconcile

- Open **Decisions** (history/replay). The decision should show co-signed with the real
  `broker_ref` and the reconciled fill.
- (Optional) confirm the same position appears in the Schwab app.
- Story 10-12: after the fill, your cached settled cash is debited by the executed cost — a
  second deploy in the same session sees the reduced cash.

## 7. (Optional) one cost-switch

If you hold a higher-fee fund the cost bucket flags, exercise the linked SELL+BUY once: co-sign
the switch SELL → confirm the linked BUY is queued (`awaiting_funds`) and resumes as a second
co-sign when cash settles (Story 10-5). Skip if you have nothing to switch.

## 8. Cleanup

- **Ctrl-C** stops backend + frontend (Postgres keeps running; `docker compose down` to stop it).
- **Restore** your real reserve amount and parked-symbol tags in Settings.
- If you want to undo the test buy, **sell the share in the Schwab app** (Ballast can also SELL it).
- Before running the test suite again, either re-link afterward or run against a disposable DB —
  the suite wipes `brokerage_token`.

## What "pass" looks like

A real `filled` (or `partial`) deploy BUY, co-signed by you, visible in Decisions with a real
`broker_ref` and reconciled outcome, and the same position in Schwab — with the margin note shown
and no margin used. That closes Gate 3 and the real-money-readiness checklist.
