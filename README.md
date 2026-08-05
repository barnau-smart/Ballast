# Ballast

**An AI investing coach for anxious beginners.** Ballast connects to your Charles Schwab account and acts as an always-available, plain-English coach that reviews every move, explains its reasoning, backs it with real historical precedent, and stops you from self-sabotaging. Its job is **not** to beat the market — it's to help a nervous investor *feel confident and act consistently*, capturing the market's return instead of chasing it.

> ⚠️ **Personal / educational project — not financial advice.** Ballast is a private, educational tool built for a single user. It is not a product or service, is not marketed, and does not provide personalized securities advice to the public. Nothing here is investment advice. Use at your own risk.

## The core idea

The enemy is **regret from a self-caused loss** (and its cousins — omission bias and headline-paralysis). Every feature is aimed at that enemy across the timeline of a decision:

- **Before** — the Precedent Engine shows how similar past market events *actually* played out (recovery precedent), and contextualizes scary headlines.
- **At** — the Coach proposes a concrete, reasoned, precedent-backed recommendation you **approve and co-sign** ("invest $X into your index core"). You approve; Ballast executes to Schwab.
- **After** — when the market dips and you get anxious, Ballast **replays the reasoning you both agreed on** — calm-past-self talking to panicked-present-self, so you hold.

Index-core, capture-not-beat. **No market-timing predictions, no unprompted FOMO alerts.**

## Architecture

A **modular monolith, hexagonal (ports-and-adapters)** at the external edges:

- **Backend** — FastAPI (Python 3.12+) modular monolith; owns *all* business logic. Every external dependency (brokerage, LLM, market data) sits behind a **port** with a swappable **adapter** (fake by default → real via config). PostgreSQL for persistence. See [`ballast/backend/README.md`](ballast/backend/README.md).
- **Frontend** — Vite + React SPA, **presentation-only** (holds no business logic; renders what the backend returns). See [`ballast/frontend/README.md`](ballast/frontend/README.md).

**Load-bearing invariants:**
- The **LLM never computes or recalls a number** — all market statistics come from the Precedent Engine as evidence records; the model may only cite IDs it was handed, and a validator rejects anything else. The model's `reasoning` *is* the just-in-time teaching.
- **Single owners, no bypass:** the Coach Engine is the sole writer of decision records; the LLM Gateway the sole caller of the Claude API; the Precedent Engine the sole source of market stats; the Broker Port the sole path to brokerage state.
- **Every trade** follows `propose → user-approve → Coach Engine → Broker Port → reconcile → persist outcome`. Rejections, partial fills, and timeouts are reconciled against the broker; the user always sees the true state. **A human always executes.**

## Repository layout

```
ballast/            # the application
  backend/          # FastAPI modular monolith (all logic) + tests
  frontend/         # Vite + React SPA (presentation only)
  README.md         # app overview + local run steps
_bmad-output/       # BMad planning & implementation artifacts (PRD, epics, stories, sprint status)
docs/               # project documentation
docker-compose.yml  # local PostgreSQL
```

## Quick start

Runs fully offline out of the box — the broker, LLM, and market-data adapters all default to in-process **fakes**, so no credentials are needed to run or test it. Real Schwab / Anthropic / Tiingo integrations are a config swap (see `ballast/backend/.env.example`).

See **[`ballast/README.md`](ballast/README.md)** for the full three-step local run (PostgreSQL → backend API on :8000 → frontend on :5173).

```bash
docker compose up -d db          # PostgreSQL on localhost:5432
cd ballast/backend && uv run pytest -q   # run the backend test suite
```

## Status

v1 is **coach-only** and feature-complete through the order-interface expansion (market, marketable-limit, and resting limit/GTC/cancel orders, plus an AI "suggest & populate" helper). The one remaining pre-launch step is a gated, human-supervised real-money exercise against the live Schwab API. Real-money trading is off until that gate is deliberately cleared.
