"""Unit tests for the semantic layer, plan validation, compiler, and RLS (no DB needed)."""

from __future__ import annotations

from datetime import date

import pytest

from copilot.semantic.model import load_manifest
from copilot.compiler.plan import QueryPlan, PlanValidationError
from copilot.compiler.compile import compile_plan
from copilot.security.principal import Principal, Grant
from copilot.security.rls import build_rls_predicate

M = load_manifest()


def test_manifest_loads_metrics():
    assert "arr" in M.metrics and len(M.metrics) >= 15


def test_plan_validation_accepts_valid():
    QueryPlan.from_dict({"metric": "arr", "dimensions": ["segment"]}).validate(M)


@pytest.mark.parametrize("bad", [
    {"metric": "revenue"},                                   # unknown metric
    {"metric": "arr", "dimensions": ["stage"]},              # dim invalid for metric
    {"metric": "arr", "filters": [{"dimension": "opp_type", "op": "eq", "values": ["x"]}]},
])
def test_plan_validation_rejects(bad):
    with pytest.raises(PlanValidationError):
        QueryPlan.from_dict(bad).validate(M)


def test_compile_is_parameterized_and_scoped():
    plan = QueryPlan.from_dict({"metric": "arr",
                                "filters": [{"dimension": "region", "op": "eq",
                                             "values": ["EMEA"]}]})
    cq = compile_plan(M, plan, "TRUE", [], date(2026, 6, 30))
    assert "is_internal = FALSE" in cq.sql          # eligibility
    assert "?" in cq.sql                             # parameterized
    assert "EMEA" in cq.params                       # value bound, not interpolated
    assert "EMEA" not in cq.sql


def test_rls_unrestricted_is_true():
    p = Principal("u", grants=[Grant("all", "*")])
    assert build_rls_predicate(p, "2026-06-30") == ("TRUE", [])


def test_rls_no_grants_fails_closed():
    pred, params = build_rls_predicate(Principal("u", grants=[]), "2026-06-30")
    assert pred == "FALSE" and params == []


def test_rls_region_scope():
    pred, params = build_rls_predicate(Principal("u", grants=[Grant("region", "EMEA")]),
                                       "2026-06-30")
    assert "a.region = ?" in pred and params == ["EMEA"]


def test_rls_owned_accounts_is_as_of_subquery():
    pred, params = build_rls_predicate(
        Principal("u", grants=[Grant("owned_accounts", "USR-0001")]), "2026-06-30")
    assert "account_ownership" in pred and "valid_from <= ?" in pred
    assert params[0] == "USR-0001"
