# Deferred Work

Incidental, real issues surfaced during autonomous review passes but out of scope for the story that found them. Each entry is appended by the dev-auto review step; do not edit existing entries.

- source_spec: `_bmad-output/implementation-artifacts/3-1-market-data-ingestion-market-daily.md`
  summary: Harden the TiingoAdapter response parser against real-vendor payload shapes (missing/None OHLC fields, non-list error payloads, robust ISO-8601 timezone→calendar-day handling) when real Tiingo network wiring lands.
  evidence: The adapter's `fetch_eod` assumes well-formed rows (`row["date"][:10]`, `Decimal(str(row[field]))`, `row.get("volume")`); a missing/null field or a non-list payload would raise inside the per-symbol try/except and silently skip that symbol. Deliberately out of scope for 3.1 (credential-gated stub, "do NOT wire real network here"), but it is real code that will run once `MARKETDATA_ADAPTER=tiingo` with a live key — revisit alongside the first real Tiingo integration.
