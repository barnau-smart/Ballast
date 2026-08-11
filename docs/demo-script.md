# Ballast — Live Demo Run-Book (~18 min, everything real)

The secret to wowing them: **don't do a feature tour — tell a story, land 4
emotional "wow" moments, and the whole thing is real the entire time.** Ballast's
value is emotional (it stops the panic-sell), so the demo is too.

**This run-book is the ALL-LIVE version:** real Schwab account, real AI coaching,
20 years of real market history, real trade on stage. Nothing is a mockup.

> ⚠️ **All-live means your REAL balances are on screen.** You've chosen to show
> your actual portfolio (that's what makes the cash-intelligence beat land — it's
> *your* money). Just know the room sees your real numbers.

## The one-line strategy

Walk them through **the moment that ruins beginner investors — a market drop at
11pm — and show Ballast doing the opposite of every other app.** Then show it
understands *your real money honestly*, and place a **real trade on stage.**

## The 4 money moments to nail

1. **Real precedent data** stops the panic-sell (emotional + credible).
2. **The AI refuses to gamble / shows its uncertainty** (counterintuitive restraint).
3. **The honest money story** — your money-market cash shown as *parked cash*, not
   a fake "stock that's down," with three honest cash states. *This one is yours:
   the feature exists because the app first got YOUR money wrong.*
4. **The real Schwab trade** — proposed by AI, co-signed by you, filled live.

---

## Pre-stage prep — do NOT do any of this live

Everything below runs in **real-broker mode**. Budget ~20 min the morning of.

### 1. Link Schwab fresh (token lives ~7 days — do it the morning of)

Launch in real-money mode (NOT the plain script):

```bash
BALLAST_REAL_BROKER=1 ./scripts/dev.sh
```

- Confirm the loud `⚠️ REAL-MONEY BROKER MODE — BROKER_ADAPTER=schwab` banner prints.
- It serves the frontend at **`https://127.0.0.1`** (port 443) and **prompts once
  for your sudo password** (needed to bind 443). That https origin is the exact
  Schwab callback URL, so the real link completes in-app.
- Backend is on `http://localhost:8000` (health: `/api/health`); Postgres on 5432.

### 2. Open the app and connect Schwab

1. Open **`https://127.0.0.1`** → accept the one-time self-signed-cert warning
   (Chrome: *Advanced → Proceed*).
2. **Log in** on that origin (it's a different origin from `localhost:5173`, so
   log in fresh here — use your demo account).
3. Click **Connect Schwab** → real Schwab login → approve → the browser lands back
   on `https://127.0.0.1/callback` (that's the app) → it shows **"Your Schwab
   account is connected."** No code-paste, no script.
4. Land on the **Dashboard** and confirm your real holdings imported (you should
   see your real positions, including your money-market fund, e.g. **SWVXX**).

> The Schwab callback URL stays exactly `https://127.0.0.1/callback` in the Schwab
> developer portal — do **not** change it (that triggers a ~day-long re-approval).

### 3. Reset the demo state so the live before/afters actually work

The two live "watch this" moments (tagging parked cash; deciding a reserve) only
pop if the app is in its *pristine, undecided* state for your account. Clear your
cash config so SWVXX is **untagged** and the reserve is **undecided**:

```bash
# Replace the email with your DEMO login (the one you use on https://127.0.0.1).
docker exec ai_practice_project-db-1 psql -U ballast -d ballast -c \
  "DELETE FROM cash_config WHERE owner_id = (SELECT id FROM \"user\" WHERE email = 'YOUR_DEMO_EMAIL');"
```

Reload the Dashboard. You should now see: the calm **set-or-decline reserve
prompt**, and your **SWVXX sitting in \"The rest\" as a stock-like holding** (the
"wrong" state you're about to fix on stage).

### 4. Place the FALLBACK trade now (during market hours)

Do the full co-sign flow once for **1 share** so there's a real order on the
record. If the live attempt hiccups, you show this instead and no one notices.

1. **Coach** → order controls: symbol **`SCHB`**, amount **`40`**, side **`buy`**
   (SCHB ≈ $30, sized `floor(amount/ask)` → 1 share ≈ $30). Leave order-model
   controls at default → **MARKET** order. Do **NOT** click "Suggest this order"
   (that makes a resting limit below market that won't fill).
2. **Ask** → **Approve & Co-sign** → **Placing… → Placed.**
3. **Decisions** → confirm the new decision is at the top with a real `broker_ref`
   and reconciled fill. This is your floor.

### 5. Rehearse

- The AI is **real and non-deterministic** — lock in 2–3 exact prompts that land
  (see the run of show). Say them the same way on stage.
- Seed **1–2 co-signed decisions** so **Decisions** isn't empty (step 4 gives you
  one; do one more if you like).
- Confirm the demo is **during US market hours** for the live trade.
- Leave the app running and **don't touch the Schwab link** after you've verified
  it's green. **Do NOT run the test suite against this DB** — it wipes the
  brokerage link (you'd have to re-link).

---

## Run of show (~18 min)

### 0–2 min — The hook (screen on the calm Dashboard)

> "Meet Sam — first job, first $5,000 invested. The market drops 8% in a week.
> It's 11pm, portfolio's all red, finger over *Sell Everything*. Fortunes aren't
> lost to crashes — they're lost to panic-selling at the bottom. Every app shows
> Sam **more** red. What if one did the opposite?"

### 2–4 min — The calm home base

Point at what's *missing*: no flashing tickers, no doom. Plain-English holdings.
The **Missed-Growth Meter** — the one gentle nudge. *"Designed to lower your heart
rate, not raise it."*

### 4–8 min — The centerpiece (wow #1 + #2)

Go to **Coach**, type the real panic question **live**:

> *"The market's dropping and I'm scared. Should I sell everything and wait?"*

Show the calm AI answer (stick-to-your-plan + honest uncertainty). Scroll to
**Recovery Precedent** and hit the real-data line:

> "This isn't a vibe — in **[N] similar drops over 20 years of real market
> history**, it recovered to breakeven in a median of a couple days, **+14.7% over
> the following year.**" Point at the actual **2008 / 2020** episodes in the list.

**Wow #1.** *"Right there we just talked Sam out of the worst mistake a beginner
can make — with evidence, not platitudes."*

Then, **wow #2** — ask:

> *"Should I put it all in [a hot stock]?"*

It **calmly declines** and steers to the broad index, and it **lists what it
doesn't know.** *"It won't help you gamble — it's a coach, not a hype machine. And
most fintech overpromises; this one tells you the uncertainty."*

### 8–12 min — The honest money story (wow #3 — this one is YOURS)

> "Here's the feature I'm proudest of, and it exists for a dumb, honest reason:
> when I first linked my *own* account, Ballast got my money wrong."

1. On the **Dashboard**, point at your **SWVXX** sitting in *"The rest"* as if it
   were a stock. *"See this big 'holding'? That's not an investment — it's my cash,
   parked in a money-market fund. The app was treating my cash like a bet."*
2. Go to **Settings → Cash setup**. **Tick SWVXX** as parked (a money-market fund).
3. Back to the **Dashboard**: SWVXX now renders in its own **"Parked cash (money
   market)"** group — **no up/down arrow** (it's cash, not a bet) — and the cash
   summary splits into three honest states: **Ready to trade / Parked cash / Set
   aside (reserve).**
4. Handle the calm **set-or-decline reserve prompt**: *"It also asks — up front,
   never assuming — how much I never want it to touch."* Set a reserve (or decline)
   in the Cash setup card and show it reflected as **protected**.
5. Point at the **Missed-Growth Meter** again — now it's **honest**: it counts
   only genuinely investable cash (`cash + parked − reserve`), and it **discloses**
   that it treats the money-market cash as already earning ~4% a year. *"It won't
   guilt me about money that's already working, or money I've set aside."*

> **Wow #3:** *"Most apps would happily tell me I'm 'missing out' on $90k that's
> actually my emergency cash already earning interest. Ballast refuses to lie to
> me about my own money."*

*(Optional, advanced — safe to show, do NOT co-sign: if you now ask to buy more
than your ready-to-trade cash, it pre-fills a money-market **sell** to free up the
cash and a resumable "pending buy" — nothing places until you approve. Show the
card, then don't approve it: "it even plans the just-in-time liquidation for me —
but nothing moves without my co-sign.")*

### 12–15 min — You're in control

Ask *"Should I invest my $500 paycheck?"* → recommendation appears → hover
**Approve & Co-sign** vs **Not now**. *"The AI proposes; the human decides.
Nothing moves until you co-sign — and every decision is recorded."* (Optional:
**Suggest this order** → a patient resting buy-limit in plain English.)

### 15–18 min — The mic drop: place a REAL trade, live

> "Everything you've seen is real — real AI, my real account, 20 years of real
> market data. Let me prove the last piece: it can actually *act.*"

1. **Coach** → order controls: symbol **`SCHB`**, amount **`40`**, side **`buy`**,
   order-model controls at default (**MARKET**). Do **NOT** click "Suggest this
   order" for the live fill.
2. **Ask** → blessed recommendation with the market order.
3. **Approve & Co-sign** → **Placing… → Placed.** During market hours it fills in
   seconds.
4. **Decisions** → the new decision is at the top with symbol, real **broker_ref**,
   reconciled fill. Select it for the **verbatim replay** — *"calm past-self,
   recorded, ready to talk down panicked future-self."*

Land it: *"A real order, on a real brokerage, with real money — proposed by the AI,
co-signed by me, filled in seconds, and every bit of it recorded right here. It
doesn't just advise — it can pull the trigger, and only when I say so."*

Close: *"It turns the moment that wrecks beginners — panic — into calm, evidence,
and a good decision. And it understands my money honestly enough that I actually
trust it to act."*

---

## If something goes wrong live (say it calmly, don't panic)

- **"Placing…" hangs / indeterminate / 5xx** — never claim it didn't place. Say
  *"the app refuses to guess whether that went through — let me confirm on the
  record,"* go to **Decisions**, show the reconciled status. Worst case, fall back
  to the pre-placed trade from prep.
- **409 reconnect** — the Schwab session dropped. Fall back to the pre-placed
  trade; don't re-link on stage.
- **422 refused** — a deliberate pre-placement refusal (e.g. amount < 1 share, or
  the order doesn't match the co-signed recommendation). Nothing placed; it's a
  feature ("it won't place a nonsensical or mismatched order"). Safer to fall back.
- **Cash-setup beat won't show the before/after** — you skipped the reset (prep
  step 3). Re-run the `DELETE FROM cash_config…` and reload. (Don't fix this live.)
- **AI answer wanders** — it's non-deterministic; re-ask your rehearsed wording.
- **https page won't load** — port 443 bind failed (sudo/cert). Check the vite
  output; worst case restart `BALLAST_REAL_BROKER=1 ./scripts/dev.sh`.

## Honest guardrails (so you don't overclaim on stage)

- Real in this demo: **AI coaching, market history, your account + balances, the
  live trade, and the honest cash model.** The **weekly email digest is faked** —
  don't demo it as sending.
- The AI is non-deterministic — that's why you rehearse the prompts.
- `BALLAST_REAL_BROKER=1` is the ONLY switch that spends real money. The plain
  `./scripts/dev.sh` can never place a real order.

## After the demo

Real shares now sit in your Schwab account (the fallback + any live fill). No
cleanup required, but note you've spent real money and hold the shares. Your
cash-config tags (parked SWVXX, reserve) persist — leave them or re-clear with the
prep step 3 command.
