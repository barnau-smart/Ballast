"""Read-only live pre-flight payload-shape harness (Story 7.6).

An opt-in, default-OFF, READ-ONLY harness that drives the five read-reachable
live money-path seams (Schwab OAuth token, account numbers, balance/positions,
quote ask, and the Anthropic structured output), captures each RAW provider
payload as a REDACTED shape skeleton (keys + value-types + array-lengths only —
no leaf values survive, so secrets cannot leak), and runs a per-seam drift
comparison against the exact field paths our mappers read.

Everything here is fully offline-testable with synthetic payloads. The ONLY live
step is a credential-gated manual run (see ``preflight.run``); it places ZERO
orders — enforced structurally: nothing in this package references the sole
order-mutating client method (nor any cancel/replace/order-release surface). A source
scan confirms the guarantee.
"""
