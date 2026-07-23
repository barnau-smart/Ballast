# Architecture Spine — Technology Reality Check

**Date of review:** 2026-07-22
**Reviewed file:** `ARCHITECTURE-SPINE.md` (Stack table + inline tech mentions)
**Method:** Live web verification (WebSearch/WebFetch against PyPI, npm, official project sites, GitHub) plus the claude-api skill for Claude model IDs.

## Verdict

All committed technologies exist and are real. Every pinned version is a genuine release. Three pins are behind the current release (FastAPI-Users, Vite, schwab-py) and one dependency (schwab-py) shows no releases in ~1 year — flagged below. No fabricated, retired, or misidentified technologies were found.

## Findings by item

| Tech | Pinned | Status | Notes |
| --- | --- | --- | --- |
| Python | 3.12+ | OK | 3.12+ is current and fully supported; FastAPI 0.136 requires Python 3.10+, so 3.12+ is comfortably valid. |
| FastAPI | 0.136 | OK | Real and current. Latest is 0.136.x (0.136.3 released May 2026; further patch releases into July 2026). Requires Python ≥3.10. Actively maintained, monthly cadence. |
| FastAPI-Users | 13.x | STALE (behind) | Real release (13.0.0, Mar 2024) but the current major is **15.x** (15.0.5, Mar 2026). The project is now in **maintenance mode** — security/dependency updates only, no new features. 13.x is two majors behind and will not receive feature work. Recommend pinning to 15.x for a new build. |
| React | 19.2 | OK | Real and current. 19.2 released Oct 2025, still the current minor; patch releases through mid-2026 (e.g. 19.2.7 in June 2026). Correct choice. |
| Vite | 7.x | STALE (behind) | Real (7.x latest is 7.3.0, Dec 2025) but **Vite 8** is now current (8.1.x, released 2026, Rolldown/Oxc-based). 7.x is one major behind. Fine to pin for stability, but not the latest major. |
| PostgreSQL | 18 | OK | Real and current. PostgreSQL 18 went GA 2025-09-25; it is the current major version. |
| schwab-py (alexgolec) | 1.5.0 | REAL but STALE / low activity | Confirmed the **alexgolec** package — unofficial wrapper for the real Schwab HTTP/Trader API (NOT the itsjafer scraper). 1.5.0 is a real release; latest published is **1.5.1** (June 2025). No releases in ~1 year and development pace has visibly slowed. Package is real and usable but maintenance cadence is a risk for a broker-integration dependency. Recommend pinning 1.5.1 and monitoring the repo. |
| Anthropic Python SDK | current | OK | Real, actively maintained by Anthropic. Latest ~0.117.0 (2026). "current" is an acceptable pin; suggest pinning an explicit minor for reproducibility. |
| Claude model — Sonnet 4.6 | default | OK | Real, current, active. Exact API model id: **`claude-sonnet-4-6`** (do not append a date suffix). |
| Claude model — Opus 4.8 | hard reasoning | OK | Real, current, most-capable GA Opus tier. Exact API model id: **`claude-opus-4-8`**. |
| Market data — Tiingo | free EOD | OK (verify limits) | Tiingo exists and still offers a free tier with end-of-day stock data. Free tier is rate-limited (commonly cited ~50 symbols/hour, limited history/bandwidth). Free EOD access is real; confirm the exact free-tier symbol/history limits fit the v1 fund set. Stooq (backup) is likewise still available. |

## Verification identity note

Explicitly confirmed: `schwab-py` in the spine is the **alexgolec** package (`pypi.org/project/schwab-py`, `github.com/alexgolec/schwab-py`), described as an "unofficial API wrapper for the Schwab HTTP API." This is the real-API wrapper, NOT the itsjafer scraper. Correct dependency.

## Recommended edits (non-blocking)

- Bump **FastAPI-Users** pin `13.x` → `15.x` (and note it is now maintenance-mode).
- Consider **Vite 8** (current major) instead of 7.x, or explicitly document 7.x as a deliberate stability pin.
- Bump **schwab-py** `1.5.0` → `1.5.1`; add a note that the dependency is lightly maintained (broker-integration risk; ports/adapters isolation in AD-8 already mitigates this).
- Pin an explicit **Anthropic SDK** minor rather than "current" for reproducibility.
- Model ids to hardcode: `claude-sonnet-4-6` and `claude-opus-4-8` (no date suffixes).
