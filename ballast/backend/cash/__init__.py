"""Cash Intelligence (Epic 9): honest three-state cash — ready-to-trade,
parked money-market, and a user-declared reserve.

Story 9.1 lays the foundation: the per-user :class:`~db.models.CashConfig`
owned entity plus the read/write helpers that funnel through the fail-closed
:class:`~db.repository.ScopedRepository` (AD-10). It only *exposes* the model;
the yield-aware missed-growth recalculation (9-2) and liquidation/deferred-buy
flow (9-3) build on it.
"""
