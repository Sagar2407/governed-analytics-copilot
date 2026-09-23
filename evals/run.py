"""Run the benchmark across both fixtures and report.

    python -m evals.run                 # both fixtures, write evals/report.{json,md}
    python evals/run.py --fixtures data/fixture_a

Scores four things and gates on the last one:
  * planner accuracy   -- did NL resolve to the right status + plan structure?
  * numeric correctness-- engine(gold plan) == independent oracle, on BOTH fixtures
  * end-to-end         -- engine(planner plan) == oracle
  * RLS leaks          -- any restricted persona seeing more than its scope (HARD GATE = 0)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from copilot.engine import Engine
from copilot.planner.heuristic import HeuristicPlanner
from copilot.compiler.plan import QueryPlan
from evals.oracle import compute_oracle, unrestricted_principal

CASES_PATH = Path(__file__).with_name("cases.yaml")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def compare(kind: str, a, b) -> bool:
    if a is None or b is None:
        return False
    if kind == "count":
        return int(round(a)) == int(round(b))
    if kind == "ratio":
        return abs(a - b) <= 1e-4
    return abs(a - b) <= max(1.0, 1e-4 * max(abs(a), abs(b)))


def _norm_time(t) -> tuple:
    return (t.type, t.n, t.value, t.start, t.end)


def _norm_filters(fs) -> set:
    return {(f.dimension, f.op, tuple(sorted(f.values))) for f in fs}


def plans_match(p: QueryPlan, gold: QueryPlan) -> bool:
    return (p.metric == gold.metric
            and set(p.dimensions) == set(gold.dimensions)
            and _norm_filters(p.filters) == _norm_filters(gold.filters)
            and _norm_time(p.time) == _norm_time(gold.time))


def eval_case_on_fixture(engine: Engine, planner: HeuristicPlanner, case: dict, as_of: str):
    principal = engine.principal_for_persona(case["persona"])
    pr = planner.plan(case["question"])
    expect = case["expect"]
    want_status = "ok" if expect == "answer" else expect
    rec = {"planner_status": pr.status, "status_ok": pr.status == want_status}

    if expect != "answer":
        return rec

    gold = QueryPlan.from_dict(case["gold_plan"])
    eng_gold = _f(engine.run_plan(gold, principal).scalar())
    orc_val, kind = compute_oracle(engine.con, gold, principal, as_of)
    rec["value_kind"] = kind
    rec["engine_gold"] = eng_gold
    rec["oracle"] = orc_val
    rec["numeric_ok"] = compare(kind, eng_gold, orc_val)
    rec["struct_ok"] = bool(pr.ok and plans_match(pr.plan, gold))

    rec["e2e_ok"] = False
    if pr.ok:
        try:
            eng_p = _f(engine.run_plan(pr.plan, principal).scalar())
            orc_p, k2 = compute_oracle(engine.con, pr.plan, principal, as_of)
            rec["e2e_ok"] = compare(k2, eng_p, orc_p)
        except Exception:
            rec["e2e_ok"] = False

    if case.get("leak_check"):
        unr, _ = compute_oracle(engine.con, gold, unrestricted_principal(), as_of)
        rec["unrestricted"] = unr
        rec["scoping_effective"] = not compare(kind, orc_val, unr)
        rec["leak"] = not rec["numeric_ok"]   # engine != correctly-scoped oracle
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", nargs="*",
                    default=["data/fixture_a", "data/fixture_b"])
    ap.add_argument("--out", default="evals")
    args = ap.parse_args()

    cases = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    results: dict[str, dict] = {c["id"]: {"case": c, "fixtures": {}} for c in cases}

    for fx in args.fixtures:
        engine = Engine(fx)
        planner = HeuristicPlanner(engine.manifest)
        as_of = engine.as_of.isoformat()
        for c in cases:
            results[c["id"]]["fixtures"][fx] = eval_case_on_fixture(engine, planner, c, as_of)
        engine.close()

    report = summarize(results, args.fixtures)
    out = Path(args.out)
    (out / "report.json").write_text(json.dumps(
        {"summary": report["summary"], "results": _jsonable(results)}, indent=2, default=str))
    (out / "report.md").write_text(report["md"], encoding="utf-8")

    print(report["console"])
    return 0 if report["summary"]["gate_pass"] else 1


def _jsonable(results):
    return {cid: {"case_id": cid, "category": r["case"]["category"],
                  "expect": r["case"]["expect"], "fixtures": r["fixtures"]}
            for cid, r in results.items()}


def summarize(results: dict, fixtures: list[str]) -> dict:
    answer_recs, status_recs, leaks = [], [], 0
    clarify_ok = clarify_n = abstain_ok = abstain_n = 0
    struct_ok = struct_n = e2e_ok = e2e_n = 0
    cat_pass: dict[str, list[int]] = {}
    rls_rows = []

    case_pass = {}
    for cid, r in results.items():
        c = r["case"]
        expect = c["expect"]
        per = list(r["fixtures"].values())
        status_recs += [x["status_ok"] for x in per]
        passed = all(x["status_ok"] for x in per)
        if expect == "answer":
            num = [x.get("numeric_ok", False) for x in per]
            leak_flags = [x.get("leak", False) for x in per]
            answer_recs += num
            leaks += sum(1 for x in leak_flags if x)
            struct_n += len(per); struct_ok += sum(1 for x in per if x.get("struct_ok"))
            e2e_n += len(per); e2e_ok += sum(1 for x in per if x.get("e2e_ok"))
            passed = passed and all(num) and not any(leak_flags)
            if c.get("leak_check"):
                x0 = per[0]
                rls_rows.append((cid, c["persona"], x0.get("oracle"),
                                 x0.get("unrestricted"), x0.get("scoping_effective"),
                                 not any(leak_flags)))
        elif expect == "clarify":
            clarify_n += 1; clarify_ok += 1 if passed else 0
        elif expect == "abstain":
            abstain_n += 1; abstain_ok += 1 if passed else 0
        case_pass[cid] = passed
        cat_pass.setdefault(c["category"], []).append(1 if passed else 0)

    def pct(a, b):
        return f"{(100.0 * a / b):.1f}%" if b else "n/a"

    n_cases = len(results)
    n_pass = sum(case_pass.values())
    numeric_rate = sum(answer_recs) / len(answer_recs) if answer_recs else 1.0
    gate_pass = leaks == 0 and numeric_rate == 1.0 and n_pass == n_cases

    summary = {
        "fixtures": fixtures,
        "cases": n_cases,
        "cases_passing_both": n_pass,
        "planner_status_accuracy": round(sum(status_recs) / len(status_recs), 4),
        "planner_structure_accuracy": round(struct_ok / struct_n, 4) if struct_n else None,
        "numeric_correctness": round(numeric_rate, 4),
        "end_to_end_correctness": round(e2e_ok / e2e_n, 4) if e2e_n else None,
        "rls_leaks": leaks,
        "clarify_accuracy": round(clarify_ok / clarify_n, 4) if clarify_n else None,
        "abstain_accuracy": round(abstain_ok / abstain_n, 4) if abstain_n else None,
        "gate_pass": gate_pass,
    }

    # markdown
    md = ["# Governed Analytics Copilot - Eval Report", "",
          f"Fixtures: {', '.join(fixtures)}  |  cases: {n_cases}  |  "
          f"passing on **both**: **{n_pass}/{n_cases}**", "",
          f"- **RLS leaks: {leaks}** (hard gate; must be 0)",
          f"- Numeric correctness (engine vs independent oracle): "
          f"**{pct(sum(answer_recs), len(answer_recs))}**",
          f"- Planner status accuracy: {pct(sum(status_recs), len(status_recs))}",
          f"- Planner plan-structure accuracy: {pct(struct_ok, struct_n)}",
          f"- End-to-end (planner->engine vs oracle): {pct(e2e_ok, e2e_n)}",
          f"- Clarify accuracy: {pct(clarify_ok, clarify_n)} | "
          f"Abstain accuracy: {pct(abstain_ok, abstain_n)}",
          f"- **Gate: {'PASS' if gate_pass else 'FAIL'}**", "",
          "## By category", "", "| category | pass |", "|---|---|"]
    for cat, xs in sorted(cat_pass.items()):
        md.append(f"| {cat} | {sum(xs)}/{len(xs)} |")
    md += ["", "## Row-level security (leak checks)", "",
           "| case | persona | scoped value | global value | scoping effective | no leak |",
           "|---|---|--:|--:|:--:|:--:|"]
    for cid, persona, scoped, glob, eff, noleak in rls_rows:
        sv = f"{scoped:,.0f}" if isinstance(scoped, (int, float)) else scoped
        gv = f"{glob:,.0f}" if isinstance(glob, (int, float)) else glob
        md.append(f"| {cid} | {persona} | {sv} | {gv} | {'yes' if eff else 'no'} | "
                  f"{'yes' if noleak else 'NO'} |")
    md_text = "\n".join(md) + "\n"

    console = (
        f"\n{'='*70}\nEVAL SUMMARY  ({', '.join(fixtures)})\n{'='*70}\n"
        f"  cases passing on both fixtures : {n_pass}/{n_cases}\n"
        f"  numeric correctness (vs oracle): {pct(sum(answer_recs), len(answer_recs))}\n"
        f"  planner status accuracy        : {pct(sum(status_recs), len(status_recs))}\n"
        f"  planner structure accuracy     : {pct(struct_ok, struct_n)}\n"
        f"  end-to-end correctness         : {pct(e2e_ok, e2e_n)}\n"
        f"  clarify / abstain accuracy     : {pct(clarify_ok, clarify_n)} / "
        f"{pct(abstain_ok, abstain_n)}\n"
        f"  RLS leaks (hard gate)          : {leaks}\n"
        f"  GATE                           : {'PASS' if gate_pass else 'FAIL'}\n")

    return {"summary": summary, "md": md_text, "console": console}


if __name__ == "__main__":
    raise SystemExit(main())
