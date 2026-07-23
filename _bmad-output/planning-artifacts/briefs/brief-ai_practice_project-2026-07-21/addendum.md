# Ballast — Brief Addendum

Depth and decision-history that belongs downstream (PRD / architecture) but would bloat the brief. Companion to `brief.md`.

## 1. The rejected direction (and why) — market timing

The concept started as a **market-timing engine**: analyze economic signals + congressional trades (Capitol Trades) to decide *when* the S&P is "ripe," then buy. This was deliberately rejected in favor of the coach concept. The evidence:

- **Timing loses.** ~92% of professional funds trail the index over 20 years (S&P SPIVA). The average investor's "behavior gap" cost ~1%/yr long-run and ~8.5% in 2024 alone (DALBAR) — driven by moving in/out at the wrong times.
- **Time in > timing.** Missing just the ~10 best days roughly halves a 20-yr return; best days cluster within ~7 days of the worst. A lump sum invested immediately beats averaging-in ~68% of the time (Vanguard) because markets rise most of the time. Cash waiting for a signal is usually just losing.
- **Trading is hazardous.** The most active retail traders underperformed buy-and-hold by ~6.5%/yr (Barber & Odean). An "opportunity alert" feature is therefore actively counter-productive — hence FOMO alerts are banned by design.

### Congressional-trade signal (Capitol Trades) — rejected as a timing input
- The NANC / KRUZ "copy Congress" ETFs exist; NANC beat the S&P (~+7 pts since 2023) but research attributes that to a **tech-weighting tilt, not a signal** (KRUZ *lagged* ~8 pts). Post-STOCK-Act studies find little/no significant congressional outperformance.
- Decisive mismatch for this use case: congressional trades are **individual stock picks** disclosed **up to 45 days late** — neither property maps to timing entries into a broad **index**. If ever wanted, Autopilot already auto-copies Congress for ~$100/yr.

### Indicators assessed (for entry timing)
- *Mildly defensible as risk/regime context, NOT precise timing:* yield curve (10y–3m), 200-day MA trend rule (benefit = smaller drawdowns, not higher returns), credit spreads / VIX regime.
- *Long-run expectations only:* CAPE / Shiller P/E (predicts ~10-yr returns, ~random at 1-yr).
- *Noise for timing:* Sahm rule (lagging), market breadth.

## 2. Options considered

| Decision | Options weighed | Chosen | Why |
|---|---|---|---|
| Product identity | market-timing engine / confidence coach / hybrid | **Coach (boss) + leashed guru** | Honest, achievable, matches the real goal (anxiety, not alpha) |
| Do we even build? | use Schwab auto-invest (no app) / build | **Build** | Schwab solves *execution*, not the *confidence/education* gap |
| Broker access | read-only / full execution / propose-and-approve | **Propose-and-approve** | User in the loop on every trade; still needs Trader API in v1 |
| Guru playground | paper only / real-capped / both (paper→promote) | **Both** | Paper = safe learning default; real-capped = optional skin-in-the-game |
| Guru risk limit | fixed % cap / configurable dial | **Configurable dial** | Fits "a system *I* trust"; coach still voices opinion + optional self-locked ceiling |
| Scope | S&P only / broad portfolio | **Broad** | Coach reviews the whole portfolio |
| Audience | just me / me + door open / product | **Me, architected to open later** | Keeps regulatory burden low now without painting into a corner |
| Form factor | web / mobile / CLI | **Web app** | Easiest to build + iterate; digest via email/text |

## 3. Technical constraints & landscape

- **Schwab Trader API (Individual):** replaced the TD Ameritrade API (TDA shut down May 2024). Supports reading balances/positions and placing equity/option orders via OAuth2 REST. Pain points: multi-day manual app approval; **refresh token expires ~7 days with no programmatic renewal (weekly interactive re-login)**; ~120 order req/min per account. Community wrapper: `schwab-py`.
- **Extensibility:** wrap the broker behind an adapter interface. **Alpaca** is the easiest alternative (API-first, free paper trading, no minimum) and a natural second adapter / paper-mode backend.
- **Competitive landscape:** *Advice* is crowded — **PortfolioPilot** (RIA, connects accounts, reviews portfolio, ~$30/mo, **advice-only, no execution**), **Magnifi** (AI copilot). *Execution* exists separately — robo-advisors (auto-trade own managed accounts), **Autopilot** (executes copy-trades in a linked brokerage). The **advice + coaching + execution-in-your-own-account** combination barely exists — the seam Ballast occupies. Daily-brief newsletters (Morning Brew etc.) are a free commodity.

## 4. Behavioral-design principles (product invariants)

1. **Coach is boss.** The guru can never touch core holdings; coach has final word.
2. **Pull, not push.** Guru speaks only when summoned. No unprompted opportunity/FOMO alerts.
3. **Counter-evidence mandatory.** Every guru idea ships with its honest bear case + odds.
4. **Configurable, never silent.** User owns the risk dial; the coach always voices its opinion as risk rises.
5. **Commitment device.** Optional self-locked risk ceiling with a cooldown-to-raise — calm-user protects impulsive-user.
6. **Honest success metric.** Measured by anxiety reduced / mistakes avoided / literacy gained — not by beating the index.

## 5. Regulatory note (for later)

Personal use is unencumbered. If the tool is ever opened to others and provides personalized securities recommendations and/or executes trades for them, U.S. **investment-adviser (RIA)** registration and fiduciary obligations likely apply. Paper-trading and "educational/informational" framing materially reduce exposure. Revisit before any multi-user release.
