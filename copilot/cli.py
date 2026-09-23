"""Command-line copilot: ask governed questions in natural language.

    python -m copilot.cli --fixture data/fixture_a --persona PER-AE "ARR by segment"
    python -m copilot.cli                      # interactive, as Finance (global)
    python -m copilot.cli --list-personas
"""

from __future__ import annotations

import argparse

import pandas as pd

from .service import Copilot


def _print_response(r, show_sql: bool):
    if r.status == "ok":
        print(f"  answer: {r.answer}")
        print(f"  ({r.rationale})")
        if r.rows and (len(r.rows) > 1 or r.lineage.get("grouped_by")):
            print(pd.DataFrame(r.rows).to_string(index=False))
        scope = r.lineage.get("principal", {}).get("scopes", [])
        print(f"  scope: {r.persona} {scope}  |  time: {r.lineage.get('time_window')}")
        if show_sql:
            print("  --- SQL ---")
            print("  " + r.sql.replace("\n", "\n  "))
    elif r.status == "clarify":
        print(f"  clarify: {r.message}")
    elif r.status == "abstain":
        print(f"  (abstain) {r.message}")
    else:
        print(f"  error: {r.message}")


def main():
    ap = argparse.ArgumentParser(description="Governed Analytics Copilot (CLI)")
    ap.add_argument("question", nargs="*", help="question to ask (omit for interactive)")
    ap.add_argument("--fixture", default="data/fixture_a")
    ap.add_argument("--persona", default="PER-FINANCE")
    ap.add_argument("--show-sql", action="store_true")
    ap.add_argument("--list-personas", action="store_true")
    args = ap.parse_args()

    cp = Copilot(args.fixture)
    try:
        if args.list_personas:
            for p in cp.personas():
                print(f"  {p['persona_id']:<14} {p['label']:<34} scope={p['scope_summary']}")
            return

        principal = cp.engine.principal_for_persona(args.persona)
        print(f"Fixture {args.fixture} | persona: {principal.label} "
              f"({args.persona}) | as_of {cp.engine.as_of}")

        if args.question:
            q = " ".join(args.question)
            print(f"\nQ: {q}")
            _print_response(cp.ask(q, principal), args.show_sql)
            return

        print("Ask a question (blank line or Ctrl-C to quit).")
        while True:
            try:
                q = input("\nQ: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q:
                break
            _print_response(cp.ask(q, principal), args.show_sql)
    finally:
        cp.close()


if __name__ == "__main__":
    main()
