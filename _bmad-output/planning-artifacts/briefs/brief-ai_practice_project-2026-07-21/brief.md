---
title: "Product Brief: Ballast — Your AI Investing Coach (working title)"
status: ready
created: 2026-07-21
updated: 2026-07-21
---

# Product Brief: Ballast — Your AI Investing Coach (working title)

> Working title only — "Ballast" = the weight that keeps a ship steady (and a nod to the
> steady "core" that anchors the portfolio). Rename freely.

## Executive Summary

Ballast is a personal, web-based AI investing companion that connects to your Charles Schwab account and acts like a fee-only fiduciary advisor you can actually afford — always available, explaining everything in plain English, and built above all to make investing feel *less scary*. It gives you full visibility into your portfolio, reviews every move before you make it, stops you from doing anything self-destructive, and teaches you as you go. The nagging *"am I doing it right?"* question quietly fades.

Its primary personality is a **coach, not a stock-picker** — a deliberate, evidence-based choice. Decades of data show that timing the market and chasing hot ideas reliably *lose* to a calm, consistent, low-cost plan. So Ballast's core job is to keep you on that plan and help you understand *why* it works. For the natural itch to "do more," Ballast includes an optional, leashed **"guru" mode** that hunts for ideas — but only in a space that can never hurt your real wealth: a risk-free paper-trading simulator, and (if you choose) a small, configurable, capped slice of real money. The guru always shows its own counter-argument; the coach always has the final word.

Built first for an audience of one — its creator — but architected cleanly so it could open up to others later. Why now: retail brokerage APIs and AI capable of explaining finance like a patient teacher have both finally matured.

## The Problem

The target user isn't reckless — they're **anxious**. They know they *should* invest, they've heard "just buy an index fund every paycheck," but they don't *understand* it well enough to feel confident. So every decision carries a low hum of *"am I doing this right? am I missing something? is now a bad time?"* Existing options don't fix this:

- **DIY brokerages (Schwab, etc.)** hand you powerful tools and zero guidance — a silent bank draft that never teaches or reassures.
- **Robo-advisors** automate allocation but are a black box; they don't grow your understanding or answer "why."
- **Human fee-only advisors** would help but want ~$250k minimums or ~1%/yr — out of reach for a beginner.
- **Finance media / generic AI chatbots** give answers disconnected from *your* actual money.

The cost is real: people either sit paralyzed in cash (the biggest long-term loser) or invest without conviction and panic-sell at the worst moment — the single most documented way retail investors destroy their own returns.

## The Solution

Ballast is the affordable, always-on, plain-English coach that closes that gap. Connected to your Schwab account, it:

- **Gives you visibility** — your whole portfolio, explained, in one place.
- **Reviews before you act** — propose a trade and Ballast sanity-checks it against your plan and evidence-based principles, flags anything self-destructive, and explains its reasoning. You approve; it executes to Schwab. *(Propose-and-approve — you're never out of the loop.)*
- **Teaches continuously** — every interaction plus a regular plain-English digest grow your literacy, so confidence compounds alongside your balance.
- **Feeds curiosity safely** — an optional guru mode you *summon* (it never nags) that pitches ideas only inside a risk-free paper simulator and/or a small self-capped slice of real money, always paired with the honest counter-case.

The outcome: you stop dreading investment decisions, you understand what you own and why, and you have a system you actually trust.

## What Makes This Different

- **Coach-first, honestly.** Unlike "AI that gives you hot picks," Ballast is opinionated toward what the evidence supports — consistency over cleverness. It won't sell you a fantasy.
- **Advice + execution in one loop.** The good advice tools (e.g., PortfolioPilot) stop at recommendations and send you elsewhere to trade; the auto-traders don't coach. Ballast reviews *and* executes (propose-and-approve) in your own Schwab account — a combination that barely exists today.
- **It knows your real money.** Wired to your actual portfolio, so guidance is specific, not generic.
- **The itch, defused.** The leashed, counter-argued, capped guru satisfies curiosity without letting it hurt you — and teaches you firsthand why the core is the core.
- **Honest moat (for a personal tool):** it does *exactly* what its owner wants, for free, and he owns it.

## Who This Serves

**Primary (and first) user:** the creator — a self-aware beginner investor who wants to be more deliberate than blind auto-investing, is honest about not being an expert, and wants to *understand* and feel in control rather than just outsource. Success for them: less anxiety, more literacy, a plan they stick to through downturns.

Architected so a future audience of similar "anxious beginners" could be served without a rewrite.

## How It Works (at a glance)

- **Coach (always on, in charge):** visibility, trade review, guardrails, education, and portfolio-level guidance. The recommended **core is exactly what you'd expect — consistent, low-cost buying of a broad index fund** (an S&P 500 or total-market fund as the backbone). "Broad" means the coach reviews your *whole* portfolio and can diversify beyond the S&P if you choose (e.g., total-market / international) — it *never* means avoiding the index.
- **Guru (opt-in, leashed):** idea generation, on-demand only (*pull, not push*), always with counter-evidence, confined to paper and/or a configurable capped real-money satellite — never the core.
- **Risk is a dial you own:** you set the satellite size and appetite; the coach obeys but always voices its opinion as you turn it up; an optional self-locked ceiling (with a cooldown to raise) lets calm-you protect impulsive-you.
- **Execution:** propose-and-approve through the Schwab Trader API; a broker-adapter design so other brokerages can be added later.
- **Delivery:** web-app dashboard + chat, plus a regular plain-English digest/notification.

## Scope

**In (v1):**
- Schwab account connection (read: balances / positions / history).
- Coach: portfolio visibility, trade review + guardrails, plain-English explanations, periodic digest.
- Propose-and-approve execution of core trades via the Schwab Trader API.
- Guru **paper mode** (risk-free simulator), configurable, with mandatory counter-evidence.
- Configurable risk dial + optional self-locked ceiling.

**Deferred (fast-follow):**
- Guru **real-money capped satellite** (same propose-approve + dial machinery, real dollars — sequenced just behind paper to keep v1 focused).
- Additional broker adapters (e.g., Alpaca).
- Opening the tool to other users (and any regulatory work that implies).

**Explicitly out (by principle):**
- Market-timing predictions and "you're missing out" FOMO alerts.
- Guru access to core holdings.
- Rebuilding brokerage/custody — Schwab holds the money; Ballast is the brain.

## Key Risks & Honest Caveats

- **The goal is to *capture* the market's return, not *beat* it.** Ballast makes no promise of market-beating outperformance — ~92% of professionals can't deliver it either. Its aim is to help you reliably *capture* the market's very good long-run return (historically ~10%/yr) — which most investors *fail* to do. Most lose ~1–8%/yr to panic, bad timing, and fees. It wins by helping you *not lose*: closing that "behavior gap" puts you ahead of the average investor without any market-beating magic. Judge it on anxiety reduced, mistakes avoided, and literacy gained — not on returns vs. the S&P.
- **Schwab Trader API friction:** multi-day app approval and a refresh token that expires every ~7 days (a weekly manual re-login). This is the main engineering pain, and it lives in the execution feature. Read-only visibility is easy; execution is the hard half.
- **Guru integrity risk:** an idea engine can quietly become a FOMO machine. Mitigated by hard design rules — pull-not-push, mandatory counter-evidence, capped, coach-final-word.
- **"Door open later" = latent regulatory exposure:** the moment it gives *other* people personalized securities advice/execution, registered-investment-adviser (RIA) rules apply. Personal use plus paper/educational framing keep exposure low for now.

## Vision

Start as one anxious beginner's trusted, private coach. If it works — if it reliably turns *"I don't know what I'm doing"* into *"I understand my money and I'm calm about it"* — it can become a fee-only-fiduciary-in-your-pocket for the millions priced out of real advice and underserved by black-box robos: the honest, teaching alternative that makes investing a little less scary, one explained decision at a time.
