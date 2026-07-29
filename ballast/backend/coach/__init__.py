"""Coach Engine — recommendation pipeline. Implemented in Epic 4."""

from coach.pipeline import (
    COACH_SYSTEM_PROMPT,
    CoachDecision,
    build_default_plan,
    compose_request,
    is_hard_reasoning,
    run_coach_pipeline,
    surface,
)

__all__ = [
    "CoachDecision",
    "COACH_SYSTEM_PROMPT",
    "run_coach_pipeline",
    "build_default_plan",
    "surface",
    "compose_request",
    "is_hard_reasoning",
]
