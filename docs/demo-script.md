# Ballast — 15-Minute Demo Script

The secret to wowing them: **don't do a feature tour — tell a story, land 3
emotional "wow" moments, and end on "…and it's real."** Ballast's real-world
value is emotional (it stops the panic-sell), so the demo should be too.

## The one-line strategy

Walk them through **the moment that ruins beginner investors — a market drop at
11pm — and show Ballast doing the opposite of every other app.** Then reveal
it's not a mockup: it places real trades on a real brokerage.

## Before the room (5-min prep — do NOT do this live)

1. Run **`./scripts/dev.sh`** (fake broker = zero risk on stage, but real AI +
   real market history = real numbers).
2. **Pre-register, log in, and Connect Schwab** ahead of time. Land on the
   Dashboard. Never do signup live.
3. **Rehearse your exact coach prompts** — the AI is real, so responses vary.
   Lock in 2 that land well.
4. Seed 1–2 co-signed decisions so **Decisions history** isn't empty.
5. Have your **proof-of-real-trade** ready: pull up your actual **Schwab account
   showing the 1 share of SCHB** (the app bought it for real). That's your mic
   drop.

## Run of show (~15 min)

**0–2 min — The hook (screen off or on the calm dashboard).**

> "Meet Sam — first job, first $5,000 invested. The market drops 8% in a week.
> It's 11pm, portfolio's all red, finger hovering over *Sell Everything*.
> Fortunes aren't lost to crashes — they're lost to panic-selling at the bottom.
> Every app shows Sam **more** red. What if one did the opposite?"

**2–4 min — The calm home base.**

Open the Dashboard. Point at what's *missing*: no flashing tickers, no doom.
Plain English holdings. The **Missed-Growth Meter** — the one gentle nudge.
*"Designed to lower your heart rate, not raise it."*

**4–8 min — The centerpiece (the wow).**

Go to **Coach**, type the real panic question live:

> *"The market's dropping and I'm scared. Should I sell everything and wait?"*

Show the calm AI answer (stick-to-your-plan + honest uncertainty). Then scroll
to **Recovery Precedent** and hit them with the real-data line:

> "This isn't a vibe — in **[N] similar drops over 20 years of real market
> history**, it recovered to breakeven in a median of a couple days, **+14.7%
> over the following year.**" Point at the actual 2008 / 2020 episodes in the
> list.

**This is the moment.** *"Right there, we just talked Sam out of the single
worst money mistake a beginner can make — with evidence, not platitudes."*

**8–11 min — It has principles (and it's honest).**

- Ask: *"Should I put it all in [a hot stock]?"* → it **calmly declines** and
  steers to broad index. *"It won't help you gamble. It's a coach, not a hype
  machine."*
- Point out it **always lists what it doesn't know.** *"Most fintech
  overpromises. This one tells you the uncertainty."*

**11–13 min — You're in control.**

Ask *"Should I invest my $500 paycheck?"* → recommendation appears → hover
**Approve & Co-sign** vs **Not now**. *"The AI proposes; the human decides.
Nothing moves until you co-sign — and every decision is recorded."* (Optional:
**Suggest this order** → a patient resting buy-limit in plain English.)

**13–15 min — The mic drop: it's REAL.**

> "Everything you've seen could be a gorgeous prototype. Here's what it isn't."
> → Switch to your **real Schwab account**: there's **1 share of SCHB the app
> actually bought** — propose → co-sign → real order → real fill, on a live
> brokerage. *"The calm coaching **and** the execution are production-real. It
> doesn't just advise — it can act."*

Close: *"It turns the moment that wrecks beginners — panic — into calm,
evidence, and a good decision. And it can pull the trigger for you."*

## The 3 money moments to nail

1. **Real precedent data** stopping the panic-sell (emotional + credible).
2. **The AI refusing to gamble / showing uncertainty** (counterintuitive
   restraint).
3. **The real Schwab trade** (it's not a toy).

## Honest guardrails (so you don't overclaim on stage)

- Real in this demo: **AI coaching + market history + the live trade** (see
  below). The **weekly email is faked** — don't demo it as sending.
- The AI is non-deterministic — that's why you rehearse the prompts.

---

# Live real trade — operator runbook

**Decision (2026-08-10):** demo runs **during US market hours**, and we place a
**live real market order on stage** and watch it fill *inside Ballast* — no
Schwab-app switch. A pre-placed trade is kept as the fallback floor.

⚠️ **This spends real money.** `BROKER_ADAPTER=schwab` is the only dangerous
switch in the system. Everything below assumes you *intend* to place a real
order against your real Schwab account.

## Pre-stage prep (do NOT do live)

1. **Launch in real-money mode** — NOT the normal safe script:
   ```
   BALLAST_REAL_BROKER=1 ./scripts/dev.sh
   ```
   Confirm the loud `⚠️ REAL-MONEY BROKER MODE` banner prints. (The plain
   `./scripts/dev.sh` forces the fake broker and will NOT place real orders.)
   Real-broker mode also serves the frontend at **`https://127.0.0.1`** (port
   443) — it will **prompt for your sudo password** once. That https origin is
   the exact Schwab callback URL, so the real link completes in-app.
2. **Open `https://127.0.0.1`** and accept the one-time self-signed-cert warning
   (Chrome: "Advanced → Proceed"). Then **log in** on that origin. (It's a
   different origin from `localhost:5173`, so you'll log in fresh here.)
3. **Link Schwab through the app and verify it's green** — click **Connect
   Schwab** → real Schwab login → approve → the browser lands back on
   `https://127.0.0.1/callback`, which is the app; it completes the link and
   shows **"Your Schwab account is connected."** No code-paste, no script. The
   Schwab token lasts ~7 days, so link within a week of the demo (ideally the
   morning of). Do this minutes before you present and don't touch it after.
4. **Place the fallback trade now** — during market hours, do the full flow once
   (below) for 1 share. It lands in **Decisions** with a real `broker_ref`. If
   the live attempt hiccups, you show this instead and no one notices.
5. **Rehearse the exact amount.** Real dollars = the amount you co-sign, sized
   `floor(amount / ask)`. SCHB ≈ $30/share, so enter **~$40 → 1 share (~$30)**.
   Do **not** use the "$500 paycheck" wording for the live order — that buys ~16
   shares (~$480).

> **Note on the Schwab callback URL:** it stays exactly `https://127.0.0.1/callback`
> in the Schwab developer portal — do **not** change it (that triggers a ~day-long
> re-approval). The app now serves that exact address locally in real-broker mode.

## The live co-sign (on stage) — exact steps

1. Go to **Coach**.
2. In the order controls enter **symbol `SCHB`, amount `40`, side `buy`** (or
   ask a buy question that names SCHB). Leave the order-model controls at their
   default — that keeps it a **MARKET** order.
3. **Do NOT click "Suggest this order"** for the live fill — that produces a
   resting LIMIT *below* market that won't fill on stage. (You can demo the
   Suggest button separately as the "patient discipline" feature — just don't
   co-sign it as your live-fill order.)
4. Click **Ask** → the coach returns a blessed recommendation with the market
   order.
5. Click **Approve & Co-sign** → button shows **Placing…** → **Placed.** During
   market hours a market order fills in seconds.
6. Go to **Decisions** → the new decision is at the top with the symbol, a real
   **broker_ref**, and the reconciled fill. Select it for the verbatim replay.
7. (Optional) **Dashboard** → the portfolio now reflects the new share after
   reconcile.

Land it: *"That was a real order, on a real brokerage, with real money —
proposed by the AI, co-signed by me, filled in seconds, and every bit of it
recorded right here in the app."*

## If something goes wrong live (say it calmly, don't panic)

- **"Placing…" hangs / indeterminate / 5xx** — never claim it didn't place.
  Say *"the app refuses to guess whether that went through — let me confirm on
  the record,"* go to **Decisions**, and show the reconciled status. Worst case,
  fall back to the pre-placed trade from prep.
- **409 reconnect** — the Schwab session dropped. Fall back to the pre-placed
  trade; don't try to re-link on stage.
- **422 refused** — a deliberate pre-placement refusal (e.g. amount < 1 share).
  Nothing was placed; bump the amount and it's a feature ("it won't place a
  nonsensical order"). Safer to just fall back.

## After the demo

Real shares now sit in your Schwab account (the fallback + any live fills). No
cleanup is required, but note you've spent real money and hold the shares.
