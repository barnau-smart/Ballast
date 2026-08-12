"""Allocation Coach (Epic 10): prescriptive portfolio analysis that answers
"I have money — what do I do with it?".

Story 10.1 lays the foundation: the per-user
:class:`~db.models.TargetAllocationConfig` (which named model portfolio the user
picked) plus read/write helpers that funnel through the fail-closed
:class:`~db.repository.ScopedRepository` (AD-10). The model portfolios themselves
are global reference data in :mod:`strategy.target_allocation`. Later stories
(10-2/10-3/10-4) build the gap-to-target engine + advisor narration on top.
"""
