"""Planners turn a natural-language question into a validated QueryPlan.

The planner is the ONLY place an LLM is (optionally) involved, and it never sees data or
writes SQL -- it emits a structured plan that the deterministic engine validates and runs.
A rule-based HeuristicPlanner ships as the default so the product runs fully offline; an
LLMPlanner adapter is available when an API key is configured.
"""

from .base import Planner, PlanResult  # noqa: F401
from .heuristic import HeuristicPlanner  # noqa: F401

__all__ = ["Planner", "PlanResult", "HeuristicPlanner"]
