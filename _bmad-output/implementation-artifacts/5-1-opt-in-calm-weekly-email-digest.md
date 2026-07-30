---
title: 'Opt-in calm weekly email digest'
type: 'feature'
created: '2026-07-30'
status: 'done'
baseline_revision: 'df5bc52ae29e898f0965780980007944da50d419'
final_revision: '5ab90faea18e4dcc7a20fb10e2b6216616ff1e32'
review_loop_iteration: 0
followup_review_recommended: false
context:
  - '{project-root}/_bmad-output/implementation-artifacts/epic-5-context.md'
warnings: ['oversized']
---

<intent-contract>

## Intent

**Problem:** Ballast makes no proactive contact today. Story 5.1 (FR21) delivers the product's ONE sanctioned proactive touch — an opt-in, calm weekly email that reassures a user their plan is on track — without ever nagging, alarming, or inducing FOMO (pull-not-push invariant, NFR8).

**Approach:** Build out the empty `digest/` module (AD-6 sole owner) as an email-only channel: a per-user opt-in stored fail-closed, a Settings toggle, an `EmailPort`/adapter behind the factory pattern (AD-8), static calm-voice content composed server-side from the portfolio projection, and an idempotent SYSTEM-scope batch job (`python -m digest.job`) that sends only to opted-in users, with a one-click unsubscribe.

## Boundaries & Constraints

**Always:**
- Opt-in is OFF by default; both enabling and disabling take effect immediately and are persisted per-user through the fail-closed `ScopedRepository` (AD-10).
- Email is the ONLY delivery channel — never push, never SMS. All sending goes through `EmailPort`; the module imports no push/SMS/telephony library.
- Every email includes a working one-click unsubscribe reachable WITHOUT authentication.
- Content = plan status + on-track reinforcement only, in the calm coach voice: patient, warm, honest, plain — no hype, jargon, urgency, scarcity, or FOMO. Tone is a testable acceptance criterion.
- The digest computes NO market statistics of its own; it reads plan status only through the owning module (`brokers.portfolio.get_portfolio`) and maps core holdings via `strategy.index_core.is_index_core`.
- The batch job runs under `Scope.system()` to enumerate opted-in users, then resolves each user's data under `Scope.for_user(owner_id)` — never a cross-user data read.
- The job is idempotent: it never sends the same user more than one digest per ISO week (a `last_sent_week` marker guards re-runs).
- If any market up/down figure is ever shown, it uses green▲ / sky-blue▼ with explicit signs, never red, never color-only.
- Email provider credentials come from env/secret-manager (never the DB, never logged); the real adapter is gated and never crashes at import (mirrors `BROKER_ADAPTER`/`LLM_ADAPTER`).

**Block If:**
- (No unattended blockers anticipated — the fake email adapter is the tested path; the real SMTP adapter is code-shaped but gated on creds, consistent with existing adapters. Scheduling is a deployment concern, intentionally not wired here.)

**Never:**
- No scheduler/cron dependency added to the repo (APScheduler/Celery/etc.); the weekly trigger is a deployment concern — ship the idempotent CLI only.
- No LLM in the digest path (static templated copy keeps tone deterministic and testable); do not route digest content through the LLM gateway.
- No live-broker call for a scheduled summary — read the `portfolio_cache` projection, never reconcile from the broker inside the job.
- No push notification, SMS, in-app nag, or any other proactive channel.
- Do not add an opt-in column to the `user` table (`create_all` won't alter it) — use a dedicated owned table.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Opted-in, not yet sent this week | `opted_in=True`, `last_sent_week != this_week`, portfolio present | Compose calm email, send via `EmailPort`, set `last_sent_week`, commit for that user | Send raises → log warning, continue to next user, do NOT mark sent (retried next run) |
| Opted-out user | `opted_in=False` | No email sent | No error |
| Already sent this week | `last_sent_week == this_week` | Skip — no second send (idempotent) | No error |
| Opted-in, never imported | `PortfolioView.is_empty` (as_of None) | Still send a gentle "nothing to summarize yet — you're set up and steady" variant with unsubscribe | No error |
| Unsubscribe, valid token | `GET /api/digest/unsubscribe?token=<match>` | Set `opted_in=False`, return calm 200 confirmation | Idempotent if already off |
| Unsubscribe, unknown/blank token | token missing or no row matches | Calm 200 (no account enumeration, no leak) | No error surfaced to caller |
| Real adapter, no creds | `EMAIL_ADAPTER=smtp`, `SMTP_HOST` empty | Raises clear `EmailNotConfiguredError` when used | Never crashes at import; fake path unaffected |
| Unknown adapter name | `EMAIL_ADAPTER=bogus` | `get_email_sender()` raises `UnknownEmailAdapterError` | Clear message |

</intent-contract>

## Code Map

- `ballast/backend/db/models.py` -- add `DigestPreference(OwnedEntityMixin, Base)` (owned, per-user opt-in row)
- `ballast/backend/db/scope.py`, `db/repository.py` -- fail-closed scope + repo (reuse; `Scope.system()` names `digest/` as a sanctioned caller)
- `ballast/backend/brokers/portfolio.py` -- `get_portfolio(scope, session) -> PortfolioView` (read-only plan-status source)
- `ballast/backend/strategy/index_core.py` -- `is_index_core(symbol)` (core-plan mapping for reinforcement copy)
- `ballast/backend/marketdata/ingest.py` -- reference pattern for a SYSTEM-scope idempotent job + thin CLI `main()`
- `ballast/backend/llm/{port,factory,fake_adapter}.py` -- reference pattern for Port/Adapter/factory + gated real adapter
- `ballast/backend/api/config.py` -- add email/digest settings
- `ballast/backend/api/app.py` -- register the new digest router (`include_router`)
- `ballast/backend/api/deps.py` -- `get_scope` (reuse for authed preference endpoints)
- `ballast/backend/api/portfolio.py` -- reference router pattern (`get_scope` + `ScopedRepository`)
- `ballast/backend/db/session.py` -- `async_session_maker`, `create_db_and_tables`, `get_async_session` (reuse)
- `ballast/frontend/src/routes/Settings.jsx` -- placeholder to build the opt-in toggle into
- `ballast/frontend/src/lib/session.js` -- `apiFetch` (attaches bearer to backend calls)
- `ballast/backend/digest/__init__.py` -- currently an empty scaffold; this story owns it

## Tasks & Acceptance

**Execution:**
- [x] `ballast/backend/db/models.py` -- add `DigestPreference(OwnedEntityMixin, Base)`, `__tablename__ = "digest_preference"`: UUID `id`; `opted_in: bool` (default False); `unsubscribe_token: str` (unique, indexed); `last_sent_week: str | None` (ISO "YYYY-Www"); tz-aware UTC `created_at`/`updated_at`; `UniqueConstraint("owner_id")` — one row per user. Picked up by `create_all` (no Alembic).
- [x] `ballast/backend/digest/email_port.py` -- define `EmailPort` (ABC, `send(message)`), a frozen `EmailMessage` DTO (`to`, `subject`, `text_body`, `html_body`), and `EmailNotConfiguredError`.
- [x] `ballast/backend/digest/fake_adapter.py` -- `FakeEmailAdapter(EmailPort)` recording sent messages in an in-memory `sent` list (no network) — the tested path.
- [x] `ballast/backend/digest/smtp_adapter.py` -- `SmtpEmailAdapter(EmailPort)`, stdlib `smtplib`/`email.message` imported lazily; raises `EmailNotConfiguredError` if `SMTP_HOST`/from-address absent; never crashes at import; never logs credentials.
- [x] `ballast/backend/digest/factory.py` -- `get_email_sender() -> EmailPort` selecting on `EMAIL_ADAPTER` (default `"fake"`), lazily importing `SmtpEmailAdapter` only when selected; `UnknownEmailAdapterError` otherwise.
- [x] `ballast/backend/digest/compose.py` -- pure `compose_digest(view: PortfolioView, *, unsubscribe_url: str, opted_in_email: str | None = None) -> EmailMessage` producing static calm-voice text + minimal HTML: plan status (holding count, core-plan reinforcement via `is_index_core`), an honest on-track reassurance, and the unsubscribe link. Empty-portfolio variant. No market stats, no alarmist/FOMO/urgency/red language.
- [x] `ballast/backend/digest/preferences.py` -- helpers: `get_or_create_preference(scope, session)` (user scope; mints `secrets.token_urlsafe` on create), `set_opt_in(scope, session, opted_in)`, `list_opted_in(session)` (under `Scope.system()`), `unsubscribe_by_token(session, token) -> bool` (system scope, returns whether a row matched).
- [x] `ballast/backend/digest/job.py` -- `send_weekly_digests(session, sender, *, unsubscribe_base_url, now=None) -> DigestRunResult`: under `Scope.system()` list opted-in prefs; per user compute ISO-week key, skip if `last_sent_week` matches; resolve recipient email from `user` table by `owner_id` and portfolio via `Scope.for_user(owner_id)` → `get_portfolio`; compose; send; on success set `last_sent_week` + commit per-user; isolate per-user failures (log + continue). Add thin CLI `main()` (`python -m digest.job`) mirroring `marketdata.ingest` (configure_logging, one-line summary, non-zero exit on any failure).
- [x] `ballast/backend/api/digest.py` -- router `prefix="/api/digest"`: `GET /preference` → `{opted_in}` (authed via `get_scope`, get-or-create); `PUT /preference` body `{opted_in: bool}` → updated `{opted_in}`; `GET /unsubscribe?token=...` UNAUTHENTICATED → flip off by token, return a calm 200 confirmation page regardless of token validity.
- [x] `ballast/backend/api/app.py` -- `include_router(digest_router)` alongside the existing routers.
- [x] `ballast/backend/api/config.py` -- add `EMAIL_ADAPTER: str = "fake"`, `SMTP_HOST/SMTP_PORT/SMTP_USERNAME/SMTP_PASSWORD` (safe empty/dev defaults), `DIGEST_FROM_ADDRESS: str`, `DIGEST_UNSUBSCRIBE_BASE_URL: str = "http://localhost:8000"`; document them in `.env.example`.
- [x] `ballast/frontend/src/routes/Settings.jsx` (+ optional `Settings.css`) -- add a `.ballast-card` "Weekly digest" section: a toggle bound to `GET`/`PUT /api/digest/preference` via `apiFetch`, calm helper copy (off-by-default, unsubscribe-anytime, gentle framing), fail-quiet on error; `data-testid` on the card and toggle.
- [x] `ballast/backend/tests/test_digest_compose.py` -- unit-test the I/O matrix compose rows: calm voice (no alarmist/FOMO/red words), unsubscribe URL present, empty-portfolio variant, core-plan reinforcement.
- [x] `ballast/backend/tests/test_digest_preference_api.py` -- register/login; GET default `opted_in=False`; PUT true; GET reflects true; per-user isolation (user B never sees user A's preference).
- [x] `ballast/backend/tests/test_digest_job.py` -- two users (one opted in, one not) + `FakeEmailAdapter`: only opted-in receives; idempotency (second same-week run sends 0); one send failing doesn't abort the run and leaves that user unmarked.
- [x] `ballast/backend/tests/test_digest_unsubscribe.py` -- valid token flips `opted_in` False; unknown/blank token → calm 200, no enumeration; structural canary: `digest/` imports no push/SMS/telephony library (email-only).
- [x] `ballast/backend/tests/test_email_adapter.py` -- factory returns fake by default; unknown adapter raises; `SmtpEmailAdapter` gated (raises `EmailNotConfiguredError` without creds, never at import).
- [x] `ballast/frontend/src/test/settings.test.jsx` -- toggle reflects `opted_in` from GET; toggling issues a PUT; calm copy present; no alarmist/FOMO wording.

**Acceptance Criteria:**
- Given a signed-in user with the digest off (default), when they enable it in Settings, then `PUT /api/digest/preference` persists `opted_in=True` and a reload shows the toggle on.
- Given two users, when user A enables the digest, then user B's `GET /api/digest/preference` still returns `opted_in=False` (fail-closed per-user isolation, AD-10).
- Given the weekly job runs, when it enumerates recipients, then it sends exactly one email per opted-in user who has not already been sent this ISO week, and zero emails to opted-out users; a re-run in the same week sends none.
- Given a sent digest, when the user opens it, then it is an email in the calm coach voice (plan status + on-track reinforcement, no alarmist/FOMO/urgency wording) and contains a one-click unsubscribe link that works without logging in.
- Given a user clicks unsubscribe, when the token is honored, then `opted_in` becomes False and they receive no further digests; an unknown token returns a calm confirmation without revealing whether any account matched.
- Given the default configuration, when the job or any test runs, then it uses `FakeEmailAdapter` (no network, no credentials) and the `digest/` module sends only via `EmailPort` — never push or SMS.

## Design Notes

Calm-voice compose is intentionally STATIC (no LLM) so tone is deterministic and unit-testable — the epic makes tone a testable AC, and every external port in this codebase is faked deterministically in tests. Keep copy short and plain, e.g.:

```
Subject: Your steady week with Ballast
Hi — a quick, calm check-in. You're holding 3 core index positions and
staying on your long-term plan. Nothing needs your attention; this note is
just here so you can feel on-track. — Ballast
Not useful? Unsubscribe: {unsubscribe_url}
```

Idempotency marker: `last_sent_week = f"{y}-W{w:02d}"` from `now.isocalendar()`. The job takes an injectable `now` (default `datetime.now(timezone.utc)`) — wall-clock is acceptable here because this is a batch job, not the deterministic precedent-matching path. Per-user commit + failure isolation mirrors `marketdata/ingest.py`. The recipient email is read from the `user` table by `owner_id` inside the system-scope job (the `user` table is the owner, not an owned/scoped entity).

## Verification

**Commands:**
- `cd ballast/backend && python -m pytest tests/test_digest_compose.py tests/test_digest_preference_api.py tests/test_digest_job.py tests/test_digest_unsubscribe.py tests/test_email_adapter.py -q` -- expected: all pass (DB-backed tests need the docker Postgres up)
- `cd ballast/backend && python -m digest.job --help` -- expected: CLI prints usage (module wires cleanly)
- `cd ballast/frontend && npm test -- settings` -- expected: Settings digest-toggle test passes
- `cd ballast/frontend && npm run build` -- expected: build succeeds

## Review Triage Log

### 2026-07-30 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 7: (high 0, medium 5, low 2)
- defer: 0
- reject: 11
- addressed_findings:
  - `[medium]` `[patch]` Unsubscribe was a state-mutating GET (link scanners could silently opt users out). Added a POST `/api/digest/unsubscribe` (RFC 8058 one-click target) sharing the GET logic, plus `List-Unsubscribe`/`List-Unsubscribe-Post` headers (new optional `EmailMessage.list_unsubscribe_url`, set by compose, emitted by the SMTP adapter). GET retained for direct human clicks.
  - `[medium]` `[patch]` `SmtpEmailAdapter` had no connect timeout — one hung SMTP server could stall the whole run. Added `timeout=30`.
  - `[medium]` `[patch]` SMTP adapter only handled STARTTLS on a plain connection; port 465 (implicit TLS) failed. Now uses `SMTP_SSL` for 465 and best-effort STARTTLS (EHLO + capability check) otherwise.
  - `[medium]` `[patch]` `get_or_create_preference` could 500 on a first-time create race (GET-on-mount + PUT-on-toggle). Now catches `IntegrityError`, rolls back, and re-reads the winning row.
  - `[medium]` `[patch]` The weekly job emailed deactivated (`is_active=False`) accounts. Now skips them (recorded as skipped, left unmarked for future reactivation). Added a regression test.
  - `[low]` `[patch]` Tone test forbade the bare substring `"red"`, spuriously matching `covered`/`required`/`hundred`. Switched to word-boundary matching.
  - `[low]` `[patch]` `list_opted_in` / `unsubscribe_by_token` constructed a `Scope.system()` object that was discarded (false-safety theater). Removed the dead assignments; kept honest comments documenting the sanctioned system-context cross-user read.

### 2026-07-30 — Review pass (follow-up)
- intent_gap: 0
- bad_spec: 0
- patch: 1: (high 1, medium 0, low 0)
- defer: 6: (high 0, medium 3, low 3)
- reject: 9
- addressed_findings:
  - `[high]` `[patch]` The weekly job aborted the ENTIRE run on the first per-user send failure whenever a failing user was processed before a good one — defeating the spec's failure-isolation guarantee (I/O matrix: "Send raises → log warning, continue to next user"). `send_weekly_digests` read `pref.owner_id`/`pref.last_sent_week` from ORM instances inside the loop, but the per-user `session.rollback()` expires every instance in the identity map (independent of `expire_on_commit=False`), so the next iteration's expired-attribute read triggered an async lazy-reload → `sqlalchemy.exc.MissingGreenlet`, which propagated out of the loop and ended the run. The existing test passed only by lucky (unordered) DB row order. Fix: snapshot each pref's scalar fields (`owner_id`, `last_sent_week`, `unsubscribe_token`) into plain locals before the loop, and advance the idempotency marker via an explicit `UPDATE digest_preference` (keyed by `owner_id`) instead of mutating the expired ORM instance. Reproduced deterministically with an 8-user probe (failing user first) — pre-fix: `MissingGreenlet` abort every run; post-fix: all 7 good users sent. Hardened `test_one_send_failure_does_not_abort_run` to use several good users around the failing one so DB order can no longer mask a regression.

### 2026-07-30 — Review pass (follow-up 2)
- intent_gap: 0
- bad_spec: 0
- patch: 1: (high 0, medium 0, low 1)
- defer: 0
- reject: 26
- addressed_findings:
  - `[low]` `[patch]` `unsubscribe_by_token` (`digest/preferences.py`) ran its `select` + opt-out mutation + `session.commit()` with no error handling, and `_do_unsubscribe` did not catch either — so a DB/commit failure on a **valid** token propagated out of the unauthenticated `GET`/`POST /api/digest/unsubscribe` endpoints as an HTTP 500. That breaks the spec's I/O-matrix guarantee that unsubscribe **always** returns a calm 200 regardless of token, AND opens a token-validity enumeration side-channel: a blank/unknown token short-circuits to 200 before touching the DB, while a valid token whose commit fails would yield 500 — a distinguishing signal for whether an account/token exists. Fix: wrap the whole DB interaction in `unsubscribe_by_token` in a `try/except`; on any failure, log (no token in the message), roll back best-effort, and return `False` so the caller emits the identical calm 200 either way. Added a DB-free unit test (`test_unsubscribe_by_token_swallows_commit_failure`) driving a session whose `commit()` raises, asserting the function returns `False` (never raises) and rolled back. Flagged independently by both reviewers; verified against the code (the pre-fix commit was genuinely unguarded).

## Auto Run Result

Status: done (follow-up review pass 2)

**Summary:** Second independent follow-up review over the already-implemented Story 5.1 digest module (diff since baseline `df5bc52`). Two adversarial reviewers (general + edge-case) ran in parallel. One LOW-severity correctness/security-hardening finding was patched; all remaining findings were rejected as already-deferred (the 6 existing Story-5.1 ledger entries), spec-sanctioned, factually wrong, or not reachable — no NEW deferred entries were warranted.

**Files changed this pass:**
- `ballast/backend/digest/preferences.py` — guarded `unsubscribe_by_token`'s DB interaction so a commit/connection failure on a valid token returns `False` (rollback best-effort, log without the token) instead of bubbling a 500; added a module logger.
- `ballast/backend/tests/test_digest_unsubscribe.py` — added `test_unsubscribe_by_token_swallows_commit_failure` (DB-free) proving a failing `commit()` yields `False`, never raises, and rolls back.

**Review findings breakdown:**
- Patches applied (1, LOW): unsubscribe commit-failure → HTTP 500 that both broke the "always calm 200" contract and leaked token validity (valid-token-commit-fail 500 vs blank/unknown 200) — see the follow-up-2 Review Triage Log entry.
- Deferred (0 NEW): every deferrable item raised this pass was already captured by the 6 existing Story-5.1 deferred-work entries (non-atomic idempotency guard under concurrent runs; prefetchable in-body unsubscribe GET; UTC ISO-week boundary double-send/skip; blocklist-only calm-tone test + untested unsubscribe HTML; `DIGEST_FROM_ADDRESS` non-empty default; STARTTLS-downgrade cleartext exposure). Marginal new candidates (commit-fails-after-send double-send; unbounded/blocking send loop; email-only-canary weakness; `http://` List-Unsubscribe deliverability) were rejected as substantially covered by the concurrency/idempotency entry, out of the spec's explicitly-deferred scheduling/scaling scope, or too low-value — preferring reject over defer per triage guidance.
- Rejected (26): CASCADE "missing" (false — `OwnedEntityMixin` FK is `ondelete="CASCADE"`); `is_index_core(None)` "raises" (false — it explicitly handles `None`/blank); `last_sent_week` `String(8)` truncation (not reachable — `"YYYY-Www"` is exactly 8 for all realistic years); `user.email` NULL / `user.is_active` None (non-null fastapi-users columns); GET `/preference` writes+mints token (spec'd get-or-create); no Alembic migration (project-wide `create_all` is the sanctioned v1 mechanism); GET-unsubscribe prefetchable + token-in-query (already deferred); ISO-week boundary (already deferred); STARTTLS cleartext + 465-only TLS (already deferred); frontend silent-failure/race (documented fail-quiet posture); RFC 8058 "token in body" (false — token rides the List-Unsubscribe URI query); whitespace token; malformed-`to`/transient-portfolio "retried forever" (correct isolation behavior); factory `ImportError` (stdlib `smtplib`); F1 comment-accuracy nit; List-Unsubscribe header CRLF (not reachable — urlsafe token + config base).

**Verification performed:**
- `pytest tests/test_digest_unsubscribe.py` — 5 passed (incl. the new commit-failure guard test).
- Digest suite (`test_digest_compose/preference_api/job/unsubscribe/email_adapter`) — 22 passed.
- Full backend suite `pytest -q` — 287 passed (286 prior + 1 new).
- Reverted `uv.lock` anthropic drift so the tree carries only intended changes.

**Residual risks:** The 6 pre-existing Story-5.1 deferred items remain open in the ledger for focused follow-up; none block the story as shipped (single weekly cron; `FakeEmailAdapter` is the tested path; the real SMTP adapter is credential-gated). This pass added only one localized low-consequence error-handling patch, so `followup_review_recommended: false`.

