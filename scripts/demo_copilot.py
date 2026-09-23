"""End-to-end NL demo: questions -> plan -> governed answer, incl. clarify/abstain/RLS."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from copilot.service import Copilot

cp = Copilot(sys.argv[1] if len(sys.argv) > 1 else "data/fixture_a")
fin = cp.engine.principal_for_persona("PER-FINANCE")
ae = cp.engine.principal_for_persona("PER-AE")


def ask(q, p=fin):
    r = cp.ask(q, p)
    print(f"\nQ [{p.label}]: {q}")
    print(f"  -> [{r.status}] {r.answer or r.message}")
    if r.rationale:
        print(f"     plan: {r.rationale}")
    if r.status == "ok" and len(r.rows) > 1:
        print(pd.DataFrame(r.rows).head(8).to_string(index=False).replace("\n", "\n     "))


print(f"as_of={cp.engine.as_of}")
for q in [
    "What is our ARR?",
    "ARR by segment",
    "bookings by quarter over the last 12 months",
    "new business win rate",
    "active customers in EMEA",
    "expansion mrr by segment last 12 months",
    "open pipeline by stage",
    "net revenue last quarter",
    "MAU by region",
]:
    ask(q)

print("\n--- clarify / abstain ---")
ask("show me revenue")            # ambiguous -> clarify
ask("why did churn go up?")       # causal -> abstain
ask("what's the weather today?")  # unsupported -> abstain

print("\n--- row-level security (same question, different caller) ---")
ask("What is our ARR?", fin)
ask("What is our ARR?", ae)

cp.close()
print("\nCopilot demo complete.")
