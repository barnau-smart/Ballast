"""Decisions-maintenance scheduler (pre-unattended-prod hardening, 2026-08-06).

Unit-level, no DB / no network: the scheduler's orchestration and lifecycle are
exercised with the underlying reclaim/prune operations stubbed. The reclaimer's
real DB behavior (paging, per-row isolation, rowcount gating) is covered against
a live Postgres in ``test_recoverable_placement.py``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

import coach.maintenance as m
from coach.maintenance import MaintenanceScheduler, run_maintenance_once


class _FakeSessionCM:
    """A trivial async context manager standing in for an ``AsyncSession``.

    The reclaim/prune operations are stubbed in these tests and never touch it.
    """

    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


def _fake_session_maker():
    return _FakeSessionCM()


# --- run_maintenance_once orchestration --------------------------------------


@pytest.mark.asyncio
async def test_run_maintenance_once_runs_both_steps_and_returns_counts(monkeypatch):
    seen: dict = {}

    async def _fake_reclaim(*, session, older_than, limit):
        seen["reclaim"] = {"older_than": older_than, "limit": limit}
        return 3

    async def _fake_prune(*, session, older_than_days):
        seen["prune"] = {"older_than_days": older_than_days}
        return 5

    monkeypatch.setattr(m, "reclaim_orphaned_cosigning", _fake_reclaim)
    monkeypatch.setattr(m, "prune_stale_proposed_decisions", _fake_prune)

    reclaimed, pruned = await run_maintenance_once(
        reclaim_older_than=timedelta(minutes=15),
        retention_days=30,
        reclaim_batch_limit=100,
        session_maker=_fake_session_maker,
    )

    assert (reclaimed, pruned) == (3, 5)
    assert seen["reclaim"] == {"older_than": timedelta(minutes=15), "limit": 100}
    assert seen["prune"] == {"older_than_days": 30}


@pytest.mark.asyncio
async def test_run_maintenance_once_isolates_a_failing_step(monkeypatch):
    # A failure in the reclaim step must NOT prevent the prune step (and vice
    # versa) — both are idempotent, so the failed one retries next tick.
    async def _boom_reclaim(**kwargs):
        raise RuntimeError("reclaim connection blip")

    async def _fake_prune(*, session, older_than_days):
        return 7

    monkeypatch.setattr(m, "reclaim_orphaned_cosigning", _boom_reclaim)
    monkeypatch.setattr(m, "prune_stale_proposed_decisions", _fake_prune)

    reclaimed, pruned = await run_maintenance_once(
        reclaim_older_than=timedelta(minutes=15),
        retention_days=30,
        reclaim_batch_limit=100,
        session_maker=_fake_session_maker,
    )

    # The failed reclaim contributes 0; the prune still ran.
    assert reclaimed == 0
    assert pruned == 7


# --- from_settings mapping ----------------------------------------------------


def test_from_settings_maps_config():
    settings = SimpleNamespace(
        DECISION_MAINTENANCE_INTERVAL_SECONDS=600,
        DECISION_COSIGNING_RECLAIM_AFTER_SECONDS=900,
        DECISION_PROPOSED_RETENTION_DAYS=30,
        DECISION_RECLAIM_BATCH_LIMIT=100,
    )
    sched = MaintenanceScheduler.from_settings(
        settings, session_maker=_fake_session_maker
    )
    assert sched._interval == 600
    assert sched._reclaim_older_than == timedelta(seconds=900)
    assert sched._retention_days == 30
    assert sched._reclaim_batch_limit == 100


# --- lifecycle ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_runs_ticks_then_stops_cleanly(monkeypatch):
    ticked = asyncio.Event()
    calls = {"n": 0}

    async def _fake_once(**kwargs):
        calls["n"] += 1
        ticked.set()
        return (0, 0)

    monkeypatch.setattr(m, "run_maintenance_once", _fake_once)

    sched = MaintenanceScheduler(
        interval_seconds=0.02,
        reclaim_older_than=timedelta(minutes=15),
        retention_days=30,
        reclaim_batch_limit=100,
        session_maker=_fake_session_maker,
    )
    sched.start()
    try:
        await asyncio.wait_for(ticked.wait(), timeout=2.0)
    finally:
        await sched.stop()

    assert calls["n"] >= 1
    # Clean shutdown: the background task is joined and cleared.
    assert sched._task is None


@pytest.mark.asyncio
async def test_scheduler_loop_survives_a_tick_exception(monkeypatch):
    # A tick that raises must not kill the loop — it must tick again — else a
    # single transient failure would silently stop recovering orphans forever.
    reached_second = asyncio.Event()
    calls = {"n": 0}

    async def _fake_once(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first tick blows up")
        reached_second.set()
        return (0, 0)

    monkeypatch.setattr(m, "run_maintenance_once", _fake_once)

    sched = MaintenanceScheduler(
        interval_seconds=0.02,
        reclaim_older_than=timedelta(minutes=15),
        retention_days=30,
        reclaim_batch_limit=100,
        session_maker=_fake_session_maker,
    )
    sched.start()
    try:
        await asyncio.wait_for(reached_second.wait(), timeout=2.0)
    finally:
        await sched.stop()

    assert calls["n"] >= 2  # survived the first exception and ticked again


@pytest.mark.asyncio
async def test_start_is_idempotent_and_stop_is_safe_without_start():
    async def _never(**kwargs):  # pragma: no cover - long interval, never fires here
        return (0, 0)

    sched = MaintenanceScheduler(
        interval_seconds=3600,
        reclaim_older_than=timedelta(minutes=15),
        retention_days=30,
        reclaim_batch_limit=100,
        session_maker=_fake_session_maker,
    )
    # stop() before start() is a harmless no-op.
    await sched.stop()

    sched.start()
    first_task = sched._task
    sched.start()  # second start is a no-op — same task, not a duplicate loop
    assert sched._task is first_task
    await sched.stop()
    assert sched._task is None


# --- app lifespan wiring ------------------------------------------------------


def test_app_lifespan_starts_and_stops_scheduler_when_enabled(monkeypatch):
    # With the flag ON, the app lifespan constructs the scheduler, starts it, and
    # stops it on shutdown. Spy on the class as looked up in api.app so no real
    # background loop / DB work runs.
    import api.app as app_module
    from fastapi.testclient import TestClient

    events: list[str] = []

    class _SpyScheduler:
        @classmethod
        def from_settings(cls, settings, **kwargs):
            return cls()

        def start(self):
            events.append("start")

        async def stop(self):
            events.append("stop")

    monkeypatch.setattr(app_module, "MaintenanceScheduler", _SpyScheduler)
    monkeypatch.setenv("DECISION_MAINTENANCE_ENABLED", "true")

    with TestClient(app_module.create_app()):
        pass  # entering + exiting the context runs the full lifespan

    assert events == ["start", "stop"]


def test_app_lifespan_skips_scheduler_when_disabled(monkeypatch):
    # The suite default (conftest forces the flag OFF): the scheduler is never
    # constructed or started.
    import api.app as app_module
    from fastapi.testclient import TestClient

    started = {"n": 0}

    class _SpyScheduler:
        @classmethod
        def from_settings(cls, settings, **kwargs):  # pragma: no cover - must not run
            started["n"] += 1
            return cls()

        def start(self):  # pragma: no cover - must not run
            started["n"] += 1

        async def stop(self):  # pragma: no cover - must not run
            pass

    monkeypatch.setattr(app_module, "MaintenanceScheduler", _SpyScheduler)
    monkeypatch.setenv("DECISION_MAINTENANCE_ENABLED", "false")

    with TestClient(app_module.create_app()):
        pass

    assert started["n"] == 0
