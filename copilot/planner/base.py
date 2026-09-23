"""Planner interface + result type.

A planner maps a natural-language question to one of three outcomes:
  * ok       -> a validated QueryPlan the engine can run
  * clarify  -> the question is ambiguous; ask, don't guess
  * abstain  -> no supported metric answers this; say so instead of inventing one
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..compiler.plan import QueryPlan


@dataclass
class PlanResult:
    status: str                       # "ok" | "clarify" | "abstain"
    plan: QueryPlan | None = None
    message: str = ""
    rationale: str = ""
    candidates: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class Planner(ABC):
    @abstractmethod
    def plan(self, question: str) -> PlanResult:
        ...
