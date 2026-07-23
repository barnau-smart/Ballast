# Adversarial Review — Architecture Spine (Ballast)

**Method:** Construct pairs of units one level down (two modules, or two developers/agents building two
features) that each obey EVERY Architectural Decision (AD) to the letter, yet still build
**incompatibly**. Each constructible divergence is a hole to close with a new or tightened AD.

**Target:** `ARCHITECTURE-SPINE.md` (Ballast v1, coach-only)
**Date:** 2026-07-22
**Verdict:** The spine's *ownership* skeleton (AD-6) and *execution path* (AD-7) are strong, but the
**data-shape contracts are underspecified** — the two things every module must agree on byte-for-byte
(the Recommendation object, the evidence record, the reconciliation/outcome record, and the
portfolio-cache row) are described in prose but never pinned to a schema. Two conformant builders will
produce structurally divergent records. Isolation is stated as intent, not as an enforceable mechanism.
**8 concrete divergences below; each ends with the AD that should close it.**

---

## D1 — `evidence[]` has no fixed shape: two features emit incompatible evidence records

**AD obeyed:** AD-2 (Recommendation carries `evidence[]`), AD-3 (evidence records have "stable IDs",
come from Precedent Engine), AD-5 (evidence is snapshotted immutably).

**The pair:**
- **Feature A — Recovery-precedent (FR15).** The drawdown-matching path in `precedent/` emits evidence
  records shaped like:
  `{id: "prec-<uuid>", kind: "drawdown_recovery", match_pct: -18.4, recovery_days: 214, sample_n: 7}`.
- **Feature B — Missed-growth / headline context (FR19, FR20).** The missed-growth path, also in
  `precedent/`, emits:
  `{ref: "mg-2020-03", label: "...", cagr_if_held: 0.11, source_window: [...], asOf: "..."}`.

**Why they diverge while both conform:** AD-3 requires only "stable IDs" and "evidence records"; it
never fixes the **key name for the ID** (`id` vs `ref`), the required numeric fields, units
(percent-as-`-18.4` vs fraction-as-`0.11`), or whether a human-readable `label` is mandatory. The
AD-2 validator checks "≥1 real evidence record" and "citation IDs ⊆ retrieved set" — both pass for both
shapes. But the **LLM Gateway** (which must render `reasoning` citing these IDs) and the **decision-record
replay** (AD-5, which re-renders the snapshot months later) now face two schemas. A replay renderer
written against Feature A's shape throws or silently drops fields on a Feature B record. The immutable
snapshot means the divergence is *frozen forever* per record — no migration fixes old records.

**Hole → AD to close it:** Tighten **AD-3** (or add **AD-3a "Evidence Record Contract"**): define the
canonical evidence record schema — mandatory `evidence_id` (single key name + ID-prefix convention),
`kind` enum, `claim` (human-readable, required for replay), `values` (typed, with an explicit units
convention: percentages as `Decimal` fractions per the Money/`Decimal` rule), `as_of` (ISO-8601 UTC),
and `source` (`market_daily` provenance). Make the AD-2 validator assert the schema, not just presence.

---

## D2 — Recommendation `action` is unshaped: coach and broker disagree on what "the action" is

**AD obeyed:** AD-2 (fields `{action, reasoning, evidence[], uncertainties[]}`), AD-7 (single execution
path `propose → approve → Coach → Broker Port → reconcile → persist`).

**The pair:**
- **Coach Engine dev** treats `action` as prose for the user: `action: "Make your regular index
  contribution this week."` (natural, matches AD-4's "plain reason" default plan and NFR8 tone).
- **Broker Port dev** needs `action` to be machine-executable to place the order under AD-7:
  `{symbol, side, quantity_or_amount, order_type}`.

**Why they diverge while both conform:** AD-2 names `action` but never says whether it is a *display
string* or an *executable instruction*. AD-7 says "every trade follows propose → … → Broker Port" but
never states **what data structure crosses the Coach→Broker boundary**. The strategy-backed default
(AD-4) is often a *non-trade* ("stick to plan") — so `action` sometimes maps to an order and sometimes
to nothing. Two conformant builders produce a Recommendation whose `action` cannot be both the user
string and the broker instruction. The seam silently requires a translation layer nobody owns.

**Hole → AD to close it:** Tighten **AD-2**: split `action` into `action_label` (user-facing string) and
an optional typed `order_intent` (`null` for no-trade defaults; else
`{symbol, side, amount|quantity, order_type, tif}`). State in **AD-7** that the Coach→Broker Port
contract is exactly `order_intent`, and that a `null` `order_intent` is a valid terminal recommendation
that never reaches the Broker Port.

---

## D3 — Broker Port has no specified order-outcome / reconciliation shape

**AD obeyed:** AD-7 ("Order rejection, partial fills, and timeouts are reconciled against the broker;
the user always sees the true resulting state"), AD-8 (Coach depends only on the interface; SchwabAdapter
swappable), AD-11 (degraded mode, expiry).

**The pair:**
- **SchwabAdapter dev** returns the broker's native reconciliation: Schwab order status enum
  (`FILLED/PARTIALLY_FILLED/REJECTED/WORKING/EXPIRED`), `filledQuantity`, `orderId`, broker timestamps.
- **Coach Engine dev** (the port *consumer* and the AD-6 sole writer of the outcome into the decision
  record) expects a normalized `OrderOutcome` — but writes its own ad-hoc shape:
  `{status: "done"|"partial"|"failed", shares, avg_price}`.

**Why they diverge while both conform:** AD-8 says the adapter is swappable "without changing coach
logic" — which *demands* a port-defined normalized outcome type — but the spine **never defines that
type**. AD-7 lists the outcome cases (reject/partial/timeout) in prose but pins no enum, no field names,
no fill-price/fill-quantity representation, no idempotency/order-id key for dedup. Result: the adapter
leaks Schwab enums or the coach invents a lossy mapping; either way a second adapter (the whole point of
AD-8) cannot be dropped in, and the "true resulting state" the user sees differs by who wrote the mapping.
Timeout handling is worst: AD-7 says "reconciled against the broker" but never says the port must expose
a **query-by-client-order-id** method, so two devs handle an ambiguous timeout differently (one re-polls,
one re-submits → the phantom/duplicate order AD-7 exists to prevent).

**Hole → AD to close it:** Add **AD-7a "Broker Port Contract"**: define the `BrokerPort` interface
including a normalized `OrderOutcome` (status enum, `filled_quantity`, `avg_fill_price` as `Decimal`,
`broker_order_id`, `as_of`), a **client-supplied idempotency key** on submit, and a mandatory
`get_order_status(client_order_id)` for timeout reconciliation. State that adapters MUST map native
status into this enum and MUST NOT leak vendor types past the port.

---

## D4 — Decision-record ownership vs. portfolio-cache reconciliation: two writers of "portfolio truth"

**AD obeyed:** AD-6 ("Coach Engine is the sole writer of decision records; Broker Port is the sole path
to brokerage state"), AD-7 ("persist outcome"), Consistency table ("portfolio truth reconciled from the
broker and cached read-only elsewhere").

**The pair:**
- **Execution path (Coach Engine)** — after AD-7 reconcile, Coach must update the user's holdings so the
  user "sees the true resulting state." It writes the post-trade position into `PORTFOLIO_CACHE`.
- **Portfolio visibility path (FR5–FR6)** — the Capability map assigns portfolio visibility to
  `coach, brokers, db`; a periodic/on-open refresh also reconciles `PORTFOLIO_CACHE` from the Broker Port.

**Why they diverge while both conform:** AD-6 names owners for *decision records*, *Claude calls*,
*market stats*, and *brokerage state* — but **`PORTFOLIO_CACHE` has no named owner**. The Consistency
table says it's "cached read-only elsewhere" and "reconciled from the broker," but read-only-elsewhere
plus two write paths (post-trade update vs. periodic refresh) inside the *same* module is a classic
last-writer-wins race: a refresh that ran mid-trade can clobber the freshly-persisted fill, or the
post-trade write can resurrect a stale position after a manual out-of-band change at the broker. The ER
diagram shows `USER ||--|| PORTFOLIO_CACHE` (one row) but doesn't say whether it's the source of truth or
a projection, nor the refresh/write ordering. "The user always sees the true resulting state" (AD-7)
becomes unenforceable because two conformant code paths define "true" differently.

**Hole → AD to close it:** Add **AD-6a "Portfolio cache is a projection with one writer + reconcile
rule"**: name a single owner (Coach Engine or a dedicated `portfolio` reconciler), declare the broker the
authoritative source and the cache a *derived read-model*, and specify the write discipline —
last-reconcile-wins keyed on broker `as_of`, post-trade writes are optimistic and always superseded by
the next broker reconcile. Forbid direct cache writes outside the owner.

---

## D5 — Precedent Engine ↔ LLM Gateway boundary: who assembles the prompt context / owns retrieval scope

**AD obeyed:** AD-3 (LLM receives retrieved evidence, may cite only handed IDs; Precedent Engine is sole
stat source), AD-6 (LLM Gateway is sole caller of Claude; Precedent Engine is sole stat source), AD-8
(both behind ports).

**The pair:**
- **Coach Engine dev** implements `retrieve → compose → validate → surface` (AD-2): it calls Precedent
  Engine for evidence, then hands the raw evidence list to the LLM Gateway as a generic `messages` blob,
  expecting the Gateway to be a thin transport.
- **LLM Gateway dev** (AD-6 sole Claude caller, owns "structured output, model routing, tone NFR8")
  reasonably owns *prompt assembly* — it decides how evidence is serialized into the prompt, which model
  (Sonnet 4.6 vs Opus 4.8) fires, and enforces the AD-2 structured-output schema itself.

**Why they diverge while both conform:** AD-3 says "the LLM receives retrieved evidence as input" and the
validator (AD-2) "rejects any citation absent from the retrieved set" — but **it never says WHERE the
retrieved set lives or WHO enforces the citation-subset check**. If Coach validates citations but the
Gateway re-shapes/re-orders/truncates evidence during prompt assembly, the ID set Coach validates against
can differ from what the model actually saw → false rejects or, worse, false accepts. Model routing
(Sonnet vs Opus) has **no owner-stated trigger** ("hard reasoning" is undefined), so Coach and Gateway
each assume the other decides → either both route or neither, producing nondeterministic model choice for
identical inputs. The Gateway/Coach seam has no defined request/response contract, so "structured output"
schema drift (Gateway changes a field) silently breaks Coach's validator.

**Hole → AD to close it:** Tighten **AD-3 + AD-6**: define the **LLM Gateway request/response contract** —
Gateway input is `{system_persona, evidence_records[], schema}`, Gateway is responsible for prompt
serialization AND emitting the AD-2 schema; the **citation-subset invariant is enforced inside the
Gateway against the exact evidence set it was handed** (not re-derived downstream), and Coach re-asserts
it defensively. Make **model-routing policy an explicit AD** (or config the Gateway owns) with a stated
trigger, so routing is deterministic for identical inputs.

---

## D6 — Multi-user isolation is an intention, not an enforceable rule as written

**AD obeyed:** AD-10 ("every data access is scoped to the authenticated user"), AD-6, Consistency table
(JWT via FastAPI-Users).

**The pair:**
- **Coach Engine dev** writes queries that always filter `WHERE user_id = :current_user` — conforms.
- **Digest job dev (FR21)** and **Market-Data Ingestion dev** run as **background jobs with no
  authenticated user in context** (there is no request/JWT). The Digest job must read every user's
  decision records to build their weekly email; Ingestion writes global `market_daily` (not user-scoped).

**Why they diverge while both conform:** AD-10 says "every data access is scoped to the *authenticated*
user" — but batch jobs and ingestion have **no authenticated user**, so the rule as literally written
either (a) forbids the digest job from functioning, or (b) is quietly waived for jobs, at which point the
waiver has no boundary and any code can claim "I'm a job." The rule is phrased as a *convention developers
must remember to apply* (add a `WHERE user_id`), not a *mechanism that fails closed* (e.g. Postgres
row-level security, or a repository layer that refuses unscoped queries). A single forgotten filter in one
of a dozen query sites leaks across users, and nothing in the spine catches it. AD-10 is therefore an
intention, not an invariant — it is not "enforceable as written."

**Hole → AD to close it:** Tighten **AD-10** into a *mechanism*: mandate a single scoped-repository /
data-access layer that requires an explicit `user_id` (or an explicit, audited `SYSTEM` scope for
jobs/ingestion) on every query — unscoped access is a compile/lint/runtime failure, not a convention.
Name the two legitimate non-user scopes (batch-per-user iteration, global market data) and require the
digest job to iterate *per user through the same scoped layer*. Consider Postgres RLS as defense-in-depth.

---

## D7 — Immutable decision record vs. evolving evidence/recommendation schema (versioning gap)

**AD obeyed:** AD-5 (blessed Recommendation persisted immutably as decision record; "No feature
re-derives or mutates it"), AD-2 (schema), AD-3.

**The pair:**
- **Sprint-1 dev** persists `recommendation_snapshot` in the AD-2 shape as it exists in v1.
- **Sprint-4 dev** adds a required field to the Recommendation/evidence schema (e.g. `confidence`, or the
  D1/D2 fixes above), and updates the replay renderer (AD-5) to read it.

**Why they diverge while both conform:** AD-5 says the snapshot is immutable and never re-derived — so old
records **cannot** be back-filled. But nothing in the schema carries a **`schema_version`**. The replay
renderer (a single component reading all historical records) now hits a mix of shapes with no way to
branch correctly; it either assumes the new shape (crashes/mis-renders old records) or has to sniff
fields heuristically. Co-sign (AD-5) of an old record and replay of an old record diverge from the same
record depending on which sprint's renderer touches it. The immutability that AD-5 relies on for trust
becomes a liability without a version tag.

**Hole → AD to close it:** Tighten **AD-5 (and the AD-2 schema)**: require every persisted
`recommendation_snapshot` to embed a `schema_version`. Mandate that the replay/co-sign renderer be
**version-aware and forward-tolerant** (must render any historical version). This is the one place
immutability + evolution must be reconciled explicitly.

---

## D8 — AD-9 guru seam is reserved in prose but the pipeline entrypoint isn't defined, so v1 code will foreclose it

**AD obeyed:** AD-9 (guru is a suggestion source feeding INTO `propose → approve → bless`; may never call
execution or skip AD-2/AD-7), AD-2, AD-7.

**The pair:**
- **v1 Coach dev A** builds recommendation generation as a single private method inside Coach Engine:
  Coach *is* the only suggestion source, so `retrieve → compose` is internal and un-parameterized.
- **v1 Coach dev B** (elsewhere, same module) builds the approve/bless/execute half assuming the
  recommendation always originated internally.

**Why they diverge while both conform:** AD-9 says "reserve this boundary now," but it specifies **no
concrete seam** — no named interface for "a suggestion source," no defined hand-off object that a future
guru would produce and the pipeline would consume. Both v1 devs conform to AD-9 (they build no guru) yet
build the pipeline entrypoint as an internal call with no injection point. When the guru arrives, there is
no `SuggestionSource` port to plug into `propose`, so the "reserved" boundary was never actually reserved
— it exists only as intent. "Reserve now" is not enforceable without a defined seam.

**Hole → AD to close it:** Tighten **AD-9**: define the reserved seam concretely now — a
`SuggestionSource` producing a *candidate* (pre-bless) object in the AD-2 shape, and require the
`propose` stage to accept candidates via that interface (v1 has exactly one implementation: the Coach's
own generator). This makes the future guru a second implementation, not a refactor.

---

## Summary table

| # | Divergence (the hole) | AD to add / tighten |
| --- | --- | --- |
| D1 | `evidence[]` shape unfixed → two precedent features emit incompatible, immutably-frozen records | Tighten AD-3 / add AD-3a Evidence Record Contract |
| D2 | `action` is undefined as display-string vs. executable order intent | Tighten AD-2 (`action_label` + typed `order_intent`); pin Coach→Broker payload in AD-7 |
| D3 | No specified order-outcome / reconciliation shape; timeout dedup path undefined | Add AD-7a Broker Port Contract (normalized `OrderOutcome`, idempotency key, `get_order_status`) |
| D4 | `PORTFOLIO_CACHE` has no owner; post-trade write vs. periodic refresh race | Add AD-6a Portfolio cache = single-writer projection, broker-authoritative, reconcile-wins |
| D5 | Precedent↔LLM-Gateway seam: prompt-assembly owner, citation-check location, model-routing trigger all unstated | Tighten AD-3 + AD-6: define Gateway request/response contract; make model routing an explicit deterministic policy |
| D6 | Isolation is a per-query convention, not a fail-closed mechanism; batch/ingestion have no authenticated user | Tighten AD-10 into a mandatory scoped-repository layer + named SYSTEM/global scopes (RLS as defense-in-depth) |
| D7 | Immutable snapshot + evolving schema with no `schema_version` breaks replay/co-sign across sprints | Tighten AD-5 / AD-2: embed `schema_version`, mandate version-aware forward-tolerant renderer |
| D8 | AD-9 guru seam "reserved" in prose but no concrete injection point → v1 forecloses it | Tighten AD-9: define `SuggestionSource` port + candidate object now, one v1 implementation |
