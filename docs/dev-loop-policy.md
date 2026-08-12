# Dev-loop policy — hard gates

Standing process rules for the bmad-loop autonomous build. The live enforcement
lives in `.bmad-loop/policy.toml`, which is **gitignored (machine-local)**, so this
committed doc is the source of truth for what that policy MUST be set to.

## Hard gate: every story requires an approved governing spec

**Adopted 2026-08-12 (Epic 10 retrospective).**

No story may enter dev without an **approved governing spec file**. No story ships
"specless."

**Enforcement:** in `.bmad-loop/policy.toml`:

```toml
[gates]
mode = "per-story-spec-approval"
```

**Why:** In Epic 10, Story 10-1 (target-allocation model) shipped with **no spec
file at all**, and `expense_ratio.py` (a Story 10-4 file) was bundled into earlier
commits. The missing spec removed the acceptance-audit anchor an independent review
relies on. Separately, the loop's own self-review shipped a HIGH deploy-cash money
bug (it had even *explicitly rejected* the issue in its rejected-findings list). A
per-story spec-approval gate ensures every story has a reviewable contract before
code is written.

**Trade-off (accepted):** this pauses the otherwise-autonomous loop for spec
approval at each story, which partially qualifies the "run the loop autonomously,
pause only at real blockers" working style. That is the intended cost — a governing
spec per story is treated as a real blocker if absent.

## Related standing rule (Epic 9): mandatory independent review

Loop-built money-path / guardrail stories get an **independent adversarial review
before merging to `main`** — never rely on the loop's own "reviewed" stamp. Proven
twice (Epic 9: 8 fixes; Epic 10: a HIGH parked-cash bug the loop had rejected).

## Companion rule: no cross-story file bundling

A file belongs to exactly one story's commits. Don't land a later story's file
(e.g. a Story 10-4 module) inside an earlier story's commit — it breaks
per-story traceability.
