"""Demonstrate + sanity-check the governed engine against a fixture.

    python scripts/demo_engine.py [data/fixture_a]

Shows metric answers, a trend, RLS scoping across personas, and plan validation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copilot.engine import Engine
from copilot.compiler.plan import PlanValidationError


def hr(t):
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)


def main():
    fx = sys.argv[1] if len(sys.argv) > 1 else "data/fixture_a"
    eng = Engine(fx)
    fixture_arr = json.loads((Path(fx) / "manifest.json").read_text())["headline_kpis"]["arr_usd"]
    fin = eng.principal_for_persona("PER-FINANCE")

    hr(f"ENGINE on {fx}  (as_of={eng.as_of})")

    # 1) scalar ARR vs the fixture's independent oracle
    res = eng.run_plan_dict({"metric": "arr"}, fin)
    print(f"ARR (Finance, as-of): {res.format_scalar()}")
    print(f"oracle headline ARR:  ${fixture_arr:,.0f}")
    print(f"match: {abs(res.scalar() - fixture_arr) <= max(2.0, 0.0001 * fixture_arr)}")

    # 2) a few more metrics
    for m in ["active_customers", "net_new_mrr", "bookings", "open_pipeline", "win_rate",
              "net_revenue", "mau"]:
        r = eng.run_plan_dict({"metric": m, "time": {"type": "last_n_months", "n": 12}}, fin)
        print(f"  {m:<18} (LTM): {r.format_scalar()}")

    # 3) a grouped breakdown
    hr("ARR by segment (Finance)")
    r = eng.run_plan_dict({"metric": "arr", "dimensions": ["segment"]}, fin)
    print(r.rows.to_string(index=False))

    # 4) a trend
    hr("Bookings by quarter, last 4 quarters (Finance)")
    r = eng.run_plan_dict({"metric": "bookings", "dimensions": ["quarter"],
                           "time": {"type": "last_n_months", "n": 12}}, fin)
    print(r.rows.to_string(index=False))

    # 5) RLS: same question, different personas -> different (correctly scoped) answers
    hr("ROW-LEVEL SECURITY: ARR by persona (same plan)")
    for pid in ["PER-FINANCE", "PER-REVOPS", "PER-SALESMGR", "PER-CSLEAD", "PER-AE",
                "PER-ACCTTEAM"]:
        p = eng.principal_for_persona(pid)
        r = eng.run_plan_dict({"metric": "arr"}, p)
        scopes = ",".join(f"{g.scope_type}={g.scope_value}" for g in p.grants)
        print(f"  {p.label:<32} [{scopes:<26}] -> {r.format_scalar()}")

    # 6) plan validation rejects hallucinated / illegal plans
    hr("PLAN VALIDATION (hard gate)")
    for bad, desc in [
        ({"metric": "revenue"}, "unknown metric"),
        ({"metric": "arr", "dimensions": ["stage"]}, "dimension not valid for metric"),
        ({"metric": "arr", "filters": [{"dimension": "opp_type", "op": "eq",
                                        "values": ["New Business"]}]}, "illegal filter"),
    ]:
        try:
            eng.run_plan_dict(bad, fin)
            print(f"  {desc:<32} -> NOT REJECTED (bug!)")
        except PlanValidationError as e:
            print(f"  {desc:<32} -> rejected: {e}")

    eng.close()
    print("\nEngine demo complete.")


if __name__ == "__main__":
    main()
