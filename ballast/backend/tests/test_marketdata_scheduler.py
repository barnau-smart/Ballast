"""Scheduled market-data ingest (2026-08-07).

Unit-level, no DB / no network: the scheduler's orchestration and lifecycle are
exercised with the underlying ingest stubbed. The real ingest against Postgres /
Tiingo is covered by test_market_ingest.py + the manual backfill.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

import marketdata.scheduler as m
from marketdata.scheduler import MarketDataIngestScheduler, run_ingest_once


class _FakeSessionCM:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


def _fake_session_maker():
    return _FakeSessionCM()


class _FakeResult:
    rows_written = 3
    symbols_ingested = ["VTI"]
    symbols_failed: dict = {}


# --- run_ingest_once ----------------------------------------------------------


@pytest.mark.asyncio
async def test_run_ingest_once_computes_window_and_calls_ingest(monkeypatch):
    captured: dict = {}

    async def _fake_ingest(session, source, symbols, start, end):
        captured.update(symbols=symbols, start=start, end=end, source=source)
        return _FakeResult()

    monkeypatch.setattr(m, "ingest_market_daily", _fake_ingest)
    sentinel_source = object()

    r = await run_ingest_once(
        symbols=["VTI", "BND"],
        lookback_days=7,
        session_maker=_fake_session_maker,
        source_factory=lambda: sentinel_source,
        now=datetime(2026, 8, 7, tzinfo=timezone.utc),
    )

    assert r is not None and r.rows_written == 3
    assert captured["symbols"] == ["VTI", "BND"]
    assert captured["end"] == date(2026, 8, 7)
    assert captured["start"] == date(2026, 7, 31)  # 7 days back
    assert captured["source"] is sentinel_source


@pytest.mark.asyncio
async def test_run_ingest_once_skips_with_no_symbols(monkeypatch):
    async def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("should not ingest with no symbols")

    monkeypatch.setattr(m, "ingest_market_daily", _boom)
    r = await run_ingest_once(
        symbols=[], lookback_days=7, session_maker=_fake_session_maker,
        source_factory=lambda: object(),
    )
    assert r is None


@pytest.mark.asyncio
async def test_run_ingest_once_isolates_failure(monkeypatch):
    async def _fail(*a, **k):
        raise RuntimeError("tiingo unreachable")

    monkeypatch.setattr(m, "ingest_market_daily", _fail)
    r = await run_ingest_once(
        symbols=["VTI"], lookback_days=7, session_maker=_fake_session_maker,
        source_factory=lambda: object(),
    )
    assert r is None  # failure logged, not propagated


def test_from_settings_maps_config():
    settings = SimpleNamespace(
        MARKETDATA_INGEST_INTERVAL_SECONDS=86_400,
        MARKETDATA_INGEST_LOOKBACK_DAYS=7,
        MARKETDATA_INGEST_SYMBOLS="VTI, BND ,VXUS",
    )
    sch = MarketDataIngestScheduler.from_settings(
        settings, session_maker=_fake_session_maker
    )
    assert sch._interval == 86_400
    assert sch._lookback_days == 7
    assert sch._symbols == ["VTI", "BND", "VXUS"]  # split + trimmed


# --- lifecycle ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_ticks_immediately_then_stops(monkeypatch):
    # Work-first: it ticks right away even with a large interval (proves it won't
    # be skipped on a long daily interval).
    ticked = asyncio.Event()
    calls = {"n": 0}

    async def _fake_once(**kwargs):
        calls["n"] += 1
        ticked.set()
        return None

    monkeypatch.setattr(m, "run_ingest_once", _fake_once)
    sch = MarketDataIngestScheduler(
        interval_seconds=3600, symbols=["VTI"], lookback_days=7,
        session_maker=_fake_session_maker,
    )
    sch.start()
    try:
        await asyncio.wait_for(ticked.wait(), timeout=2.0)
    finally:
        await sch.stop()
    assert calls["n"] >= 1
    assert sch._task is None


@pytest.mark.asyncio
async def test_scheduler_survives_a_tick_exception(monkeypatch):
    reached_second = asyncio.Event()
    calls = {"n": 0}

    async def _fake_once(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first tick blows up")
        reached_second.set()
        return None

    monkeypatch.setattr(m, "run_ingest_once", _fake_once)
    sch = MarketDataIngestScheduler(
        interval_seconds=0.02, symbols=["VTI"], lookback_days=7,
        session_maker=_fake_session_maker,
    )
    sch.start()
    try:
        await asyncio.wait_for(reached_second.wait(), timeout=2.0)
    finally:
        await sch.stop()
    assert calls["n"] >= 2


@pytest.mark.asyncio
async def test_start_is_idempotent():
    async def _never(**kwargs):  # pragma: no cover - long interval
        return None

    sch = MarketDataIngestScheduler(
        interval_seconds=3600, symbols=["VTI"], lookback_days=7,
        session_maker=_fake_session_maker,
    )
    await sch.stop()  # safe before start
    sch.start()
    first = sch._task
    sch.start()
    assert sch._task is first
    await sch.stop()
    assert sch._task is None


# --- app lifespan wiring ------------------------------------------------------


def test_app_lifespan_starts_marketdata_scheduler_when_enabled(monkeypatch):
    import api.app as app_module
    from fastapi.testclient import TestClient

    events: list[str] = []

    class _Spy:
        @classmethod
        def from_settings(cls, settings, **kwargs):
            return cls()

        def start(self):
            events.append("start")

        async def stop(self):
            events.append("stop")

    monkeypatch.setattr(app_module, "MarketDataIngestScheduler", _Spy)
    monkeypatch.setenv("MARKETDATA_INGEST_ENABLED", "true")
    monkeypatch.setenv("DECISION_MAINTENANCE_ENABLED", "false")  # isolate

    with TestClient(app_module.create_app()):
        pass

    assert events == ["start", "stop"]


def test_app_lifespan_skips_marketdata_scheduler_when_disabled(monkeypatch):
    import api.app as app_module
    from fastapi.testclient import TestClient

    started = {"n": 0}

    class _Spy:
        @classmethod
        def from_settings(cls, settings, **kwargs):  # pragma: no cover
            started["n"] += 1
            return cls()

        def start(self):  # pragma: no cover
            started["n"] += 1

        async def stop(self):  # pragma: no cover
            pass

    monkeypatch.setattr(app_module, "MarketDataIngestScheduler", _Spy)
    monkeypatch.setenv("MARKETDATA_INGEST_ENABLED", "false")

    with TestClient(app_module.create_app()):
        pass

    assert started["n"] == 0
