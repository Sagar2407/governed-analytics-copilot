"""Integration tests: engine answers, RLS scoping, planner behavior, oracle agreement."""

from __future__ import annotations

from copilot.engine import Engine
from copilot.service import Copilot
from evals.oracle import compute_oracle, unrestricted_principal


def test_engine_arr_positive(fixture_dir):
    eng = Engine(fixture_dir)
    fin = eng.principal_for_persona("PER-FINANCE")
    res = eng.run_plan_dict({"metric": "arr"}, fin)
    assert res.scalar() > 0
    eng.close()


def test_rls_scopes_reduce(fixture_dir):
    eng = Engine(fixture_dir)
    fin = eng.principal_for_persona("PER-FINANCE")
    lead = eng.principal_for_persona("PER-CSLEAD")     # segment-scoped
    total = eng.run_plan_dict({"metric": "arr"}, fin).scalar()
    scoped = eng.run_plan_dict({"metric": "arr"}, lead).scalar()
    assert 0 <= scoped <= total
    eng.close()


def test_planner_statuses(fixture_dir):
    cp = Copilot(fixture_dir)
    fin = cp.engine.principal_for_persona("PER-FINANCE")
    assert cp.ask("what is our ARR", fin).status == "ok"
    assert cp.ask("show me revenue", fin).status == "clarify"
    assert cp.ask("why did churn go up", fin).status == "abstain"
    assert cp.ask("what's the weather", fin).status == "abstain"
    cp.close()


def test_engine_matches_independent_oracle(fixture_dir):
    eng = Engine(fixture_dir)
    as_of = eng.as_of.isoformat()
    fin = eng.principal_for_persona("PER-FINANCE")
    for metric in ["arr", "active_customers", "bookings", "net_revenue"]:
        plan_dict = {"metric": metric}
        from copilot.compiler.plan import QueryPlan
        plan = QueryPlan.from_dict(plan_dict)
        eng_val = float(eng.run_plan(plan, fin).scalar())
        orc_val, kind = compute_oracle(eng.con, plan, fin, as_of)
        if kind == "count":
            assert round(eng_val) == round(orc_val)
        else:
            assert abs(eng_val - orc_val) <= max(1.0, 1e-4 * abs(orc_val))
    eng.close()


def test_restricted_persona_does_not_leak(fixture_dir):
    """A scoped persona's answer must equal its independently-computed scoped oracle,
    and be <= the unrestricted total (no leakage)."""
    eng = Engine(fixture_dir)
    as_of = eng.as_of.isoformat()
    from copilot.compiler.plan import QueryPlan
    plan = QueryPlan.from_dict({"metric": "arr"})
    lead = eng.principal_for_persona("PER-CSLEAD")
    eng_val = float(eng.run_plan(plan, lead).scalar())
    scoped_oracle, _ = compute_oracle(eng.con, plan, lead, as_of)
    global_oracle, _ = compute_oracle(eng.con, plan, unrestricted_principal(), as_of)
    assert abs(eng_val - scoped_oracle) <= max(1.0, 1e-4 * max(abs(scoped_oracle), 1))
    assert eng_val <= global_oracle + 1.0
    eng.close()
