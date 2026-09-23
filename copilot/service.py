"""Copilot service: glue a planner to the governed engine and return a unified response.

This is the single entry point used by the CLI and the API. Flow:
    question --(planner)--> plan | clarify | abstain
    plan --(engine: validate + RLS + compile + execute)--> answer + lineage
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .engine import Engine
from .planner.base import Planner
from .planner.heuristic import HeuristicPlanner
from .security.principal import Principal


def _native(v):
    """Convert numpy / pandas scalars to JSON-serializable Python types.

    DuckDB -> pandas gives numpy ints/floats and pandas Timestamps (e.g. a `quarter`
    dimension), none of which the default JSON encoder can serialize. Normalizing here
    keeps the API responses valid for both scalar and grouped results.
    """
    if v is None:
        return None
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        f = float(v)
        return None if pd.isna(f) else f
    if isinstance(v, (pd.Timestamp, np.datetime64)):
        return pd.Timestamp(v).date().isoformat()
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, float) and pd.isna(v):
        return None
    return v


def _safe_records(df: pd.DataFrame) -> list[dict]:
    return [{k: _native(val) for k, val in rec.items()} for rec in df.to_dict("records")]


@dataclass
class Response:
    status: str                      # ok | clarify | abstain | error
    question: str
    persona: str = ""
    message: str = ""
    rationale: str = ""
    candidates: list[str] = field(default_factory=list)
    answer: str = ""
    value_kind: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    plan: dict | None = None
    sql: str = ""
    lineage: dict = field(default_factory=dict)


class Copilot:
    def __init__(self, fixture_dir, planner: Planner | None = None):
        self.engine = Engine(fixture_dir)
        self.planner = planner or HeuristicPlanner(self.engine.manifest)

    def close(self):
        self.engine.close()

    def personas(self):
        return self.engine.personas()

    def ask(self, question: str, principal: Principal) -> Response:
        pr = self.planner.plan(question)
        if not pr.ok:
            return Response(status=pr.status, question=question, persona=principal.label,
                            message=pr.message, candidates=pr.candidates)
        try:
            res = self.engine.run_plan(pr.plan, principal)
        except Exception as e:  # validation or execution failure -> safe, explicit error
            return Response(status="error", question=question, persona=principal.label,
                            message=str(e), plan=pr.plan.to_dict())

        if res.is_scalar or (not res.grouped_by and len(res.rows) == 1):
            answer = res.format_scalar()
        elif len(res.rows) == 0:
            answer = "no matching data"
        else:
            answer = f"{len(res.rows)} rows across {', '.join(res.grouped_by)}"

        return Response(
            status="ok", question=question, persona=principal.label,
            rationale=pr.rationale, answer=answer, value_kind=res.value_kind,
            columns=list(res.rows.columns), rows=_safe_records(res.rows),
            plan=pr.plan.to_dict(), sql=res.sql, lineage=res.lineage,
        )
