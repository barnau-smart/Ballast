"""Read-only pre-flight orchestrator + CLI (Story 7.6).

Drives the five READ-reachable live money-path seams IN ORDER, letting the
passive capture taps in the adapters write redacted shape skeletons, then runs
:mod:`preflight.drift` per seam and writes ONE human-readable report (a per-seam
field table + overall PASS / DRIFT / INCOMPLETE verdict + the token-reconstructed
and order-status out-of-scope caveats) alongside the redacted captures.

Seam order:
  1. token decrypt          -> ``_to_broker_tokens`` seam (see caveat below)
  2. account numbers        -> ``_account_hash``
  3. balance / positions    -> ``fetch_portfolio``
  4. one quote ask (VTI)    -> ``_quote_ask``
  5. /recommend             -> the Anthropic ``_parse_message`` seam

TOKEN-SEAM CAVEAT: the orchestrator drives the token seam from the Ballast-
RECONSTRUCTED token dict, not Schwab's raw OAuth payload, so a token PASS confirms
only that our reconstruction matches our mapper. The raw Schwab token shape is
captured at OAuth-LINK time; a true read-only exchange-boundary token drift check
is deferred (see drift.token_reconstructed_caveat_line).

STRUCTURAL ZERO-ORDER GUARANTEE: this module (and the whole ``preflight``
package) NEVER references any order-mutating client method (``place_order``,
``cancel_order``, ``replace_order``) nor the ``approve`` path — only read methods
are driven. A source scan of the package enforces it, and a harness run against a
broker spy records zero placements.

Everything is injectable (broker / gateway / driver callables) so the offline
tests exercise it with fakes; nothing here requires real credentials to import.
The one credential-gated LIVE run is a human runbook step, not a CI/loop task.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from api.config import get_settings
from preflight import drift

#: The single broad ETF the read-only quote seam is exercised against.
PREFLIGHT_QUOTE_SYMBOL = "VTI"

#: The report file the orchestrator writes into the capture dir.
REPORT_FILENAME = "preflight-report.txt"


@dataclass
class SeamReport:
    """The drift outcome for one seam: its results + PASS/DRIFT verdict."""

    seam: str
    results: list[drift.FieldResult]
    verdict: str


@dataclass
class PreflightReport:
    """The full harness outcome: per-seam reports + overall verdict + report text."""

    seams: list[SeamReport]
    overall: str
    text: str


def _drive_default(broker: Any, gateway: Any) -> list[tuple[str, str]]:
    """Drive the five READ seams in order against a live-configured session.

    ONLY read methods are invoked: token normalize/decrypt, ``_account_hash``,
    ``fetch_portfolio``, one ``_quote_ask``, and the LLM ``/recommend``
    completion. No order-mutating / cancel / replace / order-release surface is ever
    touched.

    EACH seam drive is isolated: a failure in one (a network hiccup, a typed
    ``LLMError``, an ambiguous account) is recorded and the remaining seams still
    run, so a diagnostic harness never loses seams 1-3 because seam 4 raised.
    Returns the list of ``(step, error)`` failures (empty on a clean run).
    """
    errors: list[tuple[str, str]] = []

    def _step(label: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — a diagnostic harness must survive one seam failing
            errors.append((label, f"{type(exc).__name__}: {exc}"))

    # 1: token decrypt -> normalize (taps the token seam). The decrypted token
    # dict is read via the broker's bound accessor and normalized through the
    # read-only ``_to_broker_tokens`` mapper (which taps the shape); no network.
    # NOTE: this is the Ballast-RECONSTRUCTED token dict, not Schwab's raw OAuth
    # payload (see drift.token_reconstructed_caveat_line).
    token_read_func = getattr(broker, "_token_read_func", None)
    if callable(token_read_func):
        _step("token", lambda: broker._to_broker_tokens(token_read_func()))
    else:
        errors.append(("token", "broker has no _token_read_func bound"))
    # 2-4: build the client, resolve the account hash (taps the account-numbers
    # seam), read the portfolio (taps the balance seam), and one quote ask.
    client_box: list[Any] = []
    _step("client-build", lambda: client_box.append(broker._trading_client()))
    if client_box:
        _step("account_numbers", lambda: broker._account_hash(client_box[0]))
        _step(
            "quote",
            lambda: broker._quote_ask(client_box[0], PREFLIGHT_QUOTE_SYMBOL),
        )
    _step("portfolio", broker.fetch_portfolio)
    # 5: the /recommend path — a real paid Anthropic completion (taps the message
    # + parsed-output seams). It moves no money and places no order.
    if gateway is not None:
        _step("llm", lambda: gateway.complete(_recommend_probe_request()))
    else:
        errors.append(("llm", "no gateway provided"))
    return errors


def _recommend_probe_request():
    """A minimal structured /recommend request for the LLM seam probe.

    Built lazily so importing this module needs no LLM wiring / credentials.
    """
    from coach.recommendation import RECOMMENDATION_OUTPUT_SCHEMA
    from llm.port import LLMMessage, LLMRequest

    return LLMRequest(
        messages=(
            LLMMessage(
                "user",
                "Pre-flight shape probe: return a minimal recommendation.",
            ),
        ),
        output_schema=RECOMMENDATION_OUTPUT_SCHEMA,
        hard_reasoning=False,
    )


def _load_capture(capture_dir: Path, seam: str) -> Any | None:
    """Read a written ``<seam>.json`` skeleton, or None if it was not captured."""
    path = capture_dir / f"{seam}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _clear_captures(directory: Path) -> None:
    """Remove any prior-run ``<seam>.json`` + report so a stale capture can NEVER
    be folded into this run's verdict (cross-run contamination -> false verdict).
    """
    if not directory.exists():
        return
    for seam in drift.declared_seams():
        (directory / f"{seam}.json").unlink(missing_ok=True)
    (directory / REPORT_FILENAME).unlink(missing_ok=True)


def _render_report(
    seam_reports: list[SeamReport],
    overall: str,
    drive_errors: list[tuple[str, str]] | None = None,
) -> str:
    """Render the human-readable per-seam field table + overall verdict + notes."""
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("Ballast live pre-flight payload-shape harness (Story 7.6)")
    lines.append("READ-ONLY — zero orders placed.")
    lines.append("=" * 70)
    lines.append("")
    for sr in seam_reports:
        lines.append(f"Seam: {sr.seam}  ->  {sr.verdict}")
        if not sr.results:
            lines.append("  (no capture found — seam not exercised this run)")
        for r in sr.results:
            hint = f"  (candidate: {r.candidate})" if r.candidate else ""
            lines.append(f"  [{r.verdict:<17}] {r.field_label}{hint}")
        lines.append("")
    if drive_errors:
        lines.append("Drive errors (seams that could not be exercised):")
        for step, err in drive_errors:
            lines.append(f"  - {step}: {err}")
        lines.append("")
    lines.append("-" * 70)
    lines.append(f"OVERALL: {overall}")
    lines.append("")
    lines.append(drift.token_reconstructed_caveat_line())
    lines.append(drift.order_status_out_of_scope_line())
    lines.append("")
    return "\n".join(lines)


def build_report(
    capture_dir: str | Path,
    drive_errors: list[tuple[str, str]] | None = None,
) -> PreflightReport:
    """Load the captured skeletons, run drift per seam, render the report.

    Reads the redacted ``<seam>.json`` skeletons the taps wrote, runs
    :func:`preflight.drift.compare` for each declared seam, computes the overall
    verdict, and renders the report text. A declared seam with NO capture is
    ``INCOMPLETE`` — it drags the overall off PASS so a partial run can never
    read as confirmed. ``overall`` is PASS only when every declared seam was
    captured AND every field is OK; a field mismatch is DRIFT; an un-captured
    seam (with no field mismatch) is INCOMPLETE. All three non-PASS states fail
    the CLI gate.
    """
    directory = Path(capture_dir)
    seam_reports: list[SeamReport] = []
    all_results: list[drift.FieldResult] = []
    missing_any = False
    for seam in drift.declared_seams():
        shape = _load_capture(directory, seam)
        if shape is None:
            missing_any = True
            seam_reports.append(
                SeamReport(seam=seam, results=[], verdict=drift.INCOMPLETE)
            )
            continue
        results = drift.compare(seam, shape)
        seam_reports.append(
            SeamReport(
                seam=seam,
                results=results,
                verdict=drift.overall_verdict(results),
            )
        )
        all_results.extend(results)
    field_verdict = drift.overall_verdict(all_results)
    if field_verdict == drift.DRIFT:
        overall = drift.DRIFT
    elif missing_any:
        overall = drift.INCOMPLETE
    else:
        overall = drift.PASS
    text = _render_report(seam_reports, overall, drive_errors)
    return PreflightReport(seams=seam_reports, overall=overall, text=text)


def run(
    *,
    broker: Any = None,
    gateway: Any = None,
    settings: Any = None,
    driver: Callable[[Any, Any], None] | None = None,
) -> PreflightReport:
    """Run the read-only pre-flight harness end to end.

    ``driver`` drives the five READ seams (default :func:`_drive_default`); it is
    injectable so tests exercise the orchestration with fakes/spies. The taps in
    the adapters write the redacted skeletons; this function then loads them,
    runs drift, writes the report + returns it.

    Requires ``PREFLIGHT_CAPTURE_DIR`` to be set (that is the whole point of a
    live run) — raises if it is empty, since with capture OFF nothing would be
    written to compare.
    """
    settings = settings or get_settings()
    capture_dir = getattr(settings, "PREFLIGHT_CAPTURE_DIR", "") or ""
    if not capture_dir:
        raise RuntimeError(
            "PREFLIGHT_CAPTURE_DIR is not set — the pre-flight harness needs a "
            "capture directory to write redacted skeletons into."
        )
    # Mirror the resolved dir into the PROCESS env so the adapter taps' cheap
    # ``os.environ`` gate agrees with this settings-resolved value. Settings can
    # source ``PREFLIGHT_CAPTURE_DIR`` from a ``.env`` file (the project's normal
    # mechanism) that never reaches ``os.environ`` — without this, the orchestrator
    # would drive every seam while each tap no-ops, silently producing an all-
    # INCOMPLETE run with no captures. This is the harness entry (only invoked for
    # the manual pre-flight run), so setting the process env for its duration is
    # exactly the intended scope.
    import os

    os.environ["PREFLIGHT_CAPTURE_DIR"] = capture_dir
    # Clear any prior-run captures first so a stale <seam>.json can never leak
    # into this run's verdict.
    _clear_captures(Path(capture_dir))
    # Drive the seams; a raised driver is a backstop (per-seam isolation lives in
    # _drive_default) — we ALWAYS proceed to build+write the report so a partial
    # run still produces a diagnostic, with un-captured seams marked INCOMPLETE.
    try:
        drive_errors = (driver or _drive_default)(broker, gateway) or []
    except Exception as exc:  # noqa: BLE001 — always emit a report, even on a bad driver
        drive_errors = [("driver", f"{type(exc).__name__}: {exc}")]
    report = build_report(capture_dir, drive_errors=drive_errors)
    (Path(capture_dir) / REPORT_FILENAME).write_text(report.text)
    return report


def main() -> int:
    """CLI entry: build a live broker/gateway from settings and run the harness.

    Only invoked for the credential-gated MANUAL live run (the human runbook).
    Imports the live factories lazily so ``python -m preflight.run --help``-style
    import never requires credentials.
    """
    settings = get_settings()
    capture_dir = getattr(settings, "PREFLIGHT_CAPTURE_DIR", "") or ""
    if not capture_dir:
        print(
            "PREFLIGHT_CAPTURE_DIR is not set. Set it to a local directory to run "
            "the read-only pre-flight harness."
        )
        return 2

    from brokers.factory import get_broker
    from llm.factory import get_llm_gateway

    broker = get_broker()
    gateway = get_llm_gateway()
    report = run(broker=broker, gateway=gateway, settings=settings)
    print(report.text)
    print(f"\nReport + redacted captures written to: {capture_dir}")
    return 0 if report.overall == drift.PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
