# Epic 5 Context: Weekly Digest

<!-- Generated from planning artifacts. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Deliver the one and only proactive contact Ballast makes: an **opt-in, calm weekly email digest** that summarizes the user's plan status and reinforces that they're on track. This epic is the concrete embodiment of the product's "pull, not push" invariant — the digest exists so a user can feel steady between sessions without ever being nagged, alarmed, or induced into FOMO. It matters because trust is the product; a single alarmist or hype-toned message would violate the calm-coach promise the rest of the app is built to protect.

## Stories

- Story 5.1: Opt-in calm weekly email digest

## Requirements & Constraints

- **Opt-in only, from Settings.** The digest is off by default. Users turn it on (and off) from the Settings screen, which also hosts Schwab connection status, theme, and account. There is no other delivery channel.
- **Email only.** No push notifications and no SMS in v1 — email is the sole medium. Every send must include an easy unsubscribe path.
- **Content = plan status + on-track reinforcement.** The digest summarizes where the user's plan stands and reassures them they're on track. It must never contain alarmist, urgency-driven, or FOMO-style content ("you're missing out," scary framing, hype).
- **Calm coach voice (tone is an acceptance criterion, not a nicety).** All copy embodies a patient, warm, honest teacher: plain-spoken, never condescending, never hype, no jargon. Tone is reviewable and testable, not merely "no jargon."
- **Pull-not-push invariant (superset of no-FOMO).** The digest is the *only* sanctioned proactive/unprompted contact in the entire product. Nothing else may nag or surface unprompted. Because it is proactive, it earns extra scrutiny: opt-in gated, calm, and trivially reversible via unsubscribe.
- **Market up/down framing.** Any market movement shown uses green▲ / sky-blue▼ with explicit signs — never red, never color-dependent (consistent with the app-wide calming color rules).

## Technical Decisions

- **`digest` is its own domain module.** The backend is a modular monolith (one FastAPI deployable, hexagonal at external edges). The weekly digest lives in a dedicated `digest/` module as a scheduled email job — it is the single owner of digest concerns (AD-6: one owner per concern). It must not reach around other owners: market statistics come only from the Precedent Engine, brokerage/portfolio state only via the Broker Port / portfolio projection.
- **Runs under an explicit SYSTEM/global scope.** Persistence is fail-closed: every query needs an explicit scope. The digest job is a non-user (batch) context, so it must run under a named SYSTEM/global scope and then resolve each opted-in user's data under that user's scope — never all-access, never cross-user leakage (AD-10).
- **Email transport behind a port.** External dependencies sit behind ports with swappable adapters (AD-8); email/SMTP is an external edge and should follow the same `Port`/`Adapter` convention (interfaces suffixed `Port`, adapters suffixed `Adapter`).
- **Backend owns all logic; SPA is presentation-only.** Opt-in state is persisted and enforced server-side (React SPA holds no business logic, AD-1). Email content is composed server-side.
- **Stack:** Python 3.12+, FastAPI 0.136, FastAPI-Users 15.x (JWT sessions), PostgreSQL 18, SQLAlchemy-style scoped repositories. Secrets (including any email provider credentials) come from env/secret-manager, not the database.

## UX & Interaction Patterns

- **Settings hosts the opt-in toggle** alongside Schwab connection/re-auth, theme, and account controls. Enabling and disabling must both be simple and immediate.
- **The digest is framed as the single proactive touch** — the product otherwise "speaks only when asked." Copy should read like the calm coach card voice used elsewhere (clear, plain, reassuring), never like a marketing email.
- **Unsubscribe is first-class**, honored reliably, and reachable directly from the email itself.

## Cross-Story Dependencies

- **Depends on Epic 1** for accounts and per-user isolation (opt-in preference is per-user, fail-closed scoped).
- **Depends on Epic 2** for the portfolio projection that "plan status" summarizes.
- **May draw on Epic 3/4** outputs (precedent/plan state) for on-track reinforcement content, but must consume them through their owning modules — the digest computes no market statistics of its own.
