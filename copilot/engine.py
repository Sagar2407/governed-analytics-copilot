"""The governed query engine: validate a plan, inject RLS, compile, execute, explain.

This is the deterministic core the whole product rests on. No number here originates in an
LLM: given a validated plan and a principal, the answer and its lineage are fully
reproducible. The LLM's only job (elsewhere) is to propose the plan.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from .semantic.model import Manifest, load_manifest
from .compiler.plan import QueryPlan, PlanValidationError
from .compiler.compile import compile_plan
from .security.principal import Principal, load_persona, load_principal, list_personas
from .security.rls import build_rls_predicate


@dataclass
class AnswerResult:
    plan: QueryPlan
    rows: pd.DataFrame
    sql: str
    params: list
    value_kind: str
    grouped_by: list
    lineage: dict

    @property
    def is_scalar(self) -> bool:
        return not self.grouped_by and len(self.rows) == 1

    def scalar(self):
        if not len(self.rows):
            return None
        return self.rows.iloc[0]["value"]

    def format_scalar(self) -> str:
        v = self.scalar()
        if v is None:
            return "no data"
        if self.value_kind == "currency":
            return f"${v:,.0f}"
        if self.value_kind == "ratio":
            return f"{v:.1%}"
        return f"{v:,.0f}"


class Engine:
    def __init__(self, fixture_dir: str | Path, manifest: Manifest | None = None):
        self.fixture_dir = Path(fixture_dir)
        self.manifest = manifest or load_manifest()
        fm = json.loads((self.fixture_dir / "manifest.json").read_text(encoding="utf-8"))
        self.as_of = date.fromisoformat(fm["config"]["as_of_date"])
        self.currency = self.manifest.default_currency
        self.con = duckdb.connect(str(self.fixture_dir / "warehouse.duckdb"), read_only=True)

    # --- lifecycle --------------------------------------------------------------------
    def close(self):
        self.con.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- principals -------------------------------------------------------------------
    def personas(self) -> list[dict]:
        return list_personas(self.con)

    def principal_for_persona(self, persona_id: str) -> Principal:
        return load_persona(self.con, persona_id)

    def principal_for_user(self, user_id: str, label: str = "") -> Principal:
        return load_principal(self.con, user_id, label=label)

    # --- run --------------------------------------------------------------------------
    def run_plan(self, plan: QueryPlan, principal: Principal) -> AnswerResult:
        plan.validate(self.manifest)  # hard gate: raises PlanValidationError
        rls_pred, rls_params = build_rls_predicate(principal, self.as_of.isoformat())
        cq = compile_plan(self.manifest, plan, rls_pred, rls_params, self.as_of, self.currency)
        df = self.con.execute(cq.sql, cq.params).fetchdf()

        lineage = dict(cq.lineage)
        lineage["principal"] = {
            "user_id": principal.user_id,
            "label": principal.label,
            "scopes": [f"{g.scope_type}={g.scope_value}" for g in principal.grants],
            "unrestricted": principal.is_unrestricted,
            "row_level_security": "enforced in the data layer (injected filter)",
        }
        return AnswerResult(plan=plan, rows=df, sql=cq.sql, params=cq.params,
                            value_kind=cq.value_kind, grouped_by=cq.grouped_by,
                            lineage=lineage)

    def run_plan_dict(self, plan_dict: dict, principal: Principal) -> AnswerResult:
        return self.run_plan(QueryPlan.from_dict(plan_dict), principal)
