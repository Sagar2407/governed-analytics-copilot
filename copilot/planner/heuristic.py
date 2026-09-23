"""A deterministic, offline planner (no API key required).

It maps a question to a plan using the manifest's metric/dimension synonyms plus a small
vocabulary of dimension *values* and time phrases. It is intentionally conservative: it
clarifies on ambiguous terms and abstains when no metric fits, so the "ask, don't guess"
and "don't answer what you can't" behaviors are exercised even without an LLM. The
LLMPlanner is the higher-accuracy path; this guarantees the product runs anywhere.
"""

from __future__ import annotations

import re

from ..semantic.model import Manifest, load_manifest
from ..compiler.plan import QueryPlan, PlanValidationError
from .base import Planner, PlanResult

# Dimension *values* the planner can recognize as filters: (phrase, dimension, canonical).
VALUE_VOCAB: list[tuple[str, str, str]] = [
    ("north america", "region", "NA"), ("na region", "region", "NA"),
    ("emea", "region", "EMEA"), ("apac", "region", "APAC"),
    ("latam", "region", "LATAM"), ("latin america", "region", "LATAM"),
    ("enterprise", "segment", "Enterprise"),
    ("mid market", "segment", "Mid-Market"), ("mid-market", "segment", "Mid-Market"),
    ("midmarket", "segment", "Mid-Market"), ("smb", "segment", "SMB"),
    ("small business", "segment", "SMB"),
    ("new business", "opp_type", "New Business"), ("new logo", "opp_type", "New Business"),
    ("expansion", "opp_type", "Expansion"), ("upsell", "opp_type", "Expansion"),
    ("renewal", "opp_type", "Renewal"),
]

# Words that are too ambiguous to resolve to one metric -> clarify with options.
AMBIGUOUS: dict[str, list[str]] = {
    "revenue": ["arr", "recognized_revenue", "net_revenue", "gross_billings"],
}

_GROUP_CUES = ("by", "per", "across")


def _norm(s: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", s.lower()).strip() + " "


class HeuristicPlanner(Planner):
    def __init__(self, manifest: Manifest | None = None):
        self.m = manifest or load_manifest()
        # metric synonyms sorted longest-first for greedy matching
        self._syn = []
        for name, met in self.m.metrics.items():
            for token in [met.name, met.label, *met.synonyms]:
                self._syn.append((_norm(token).strip(), name))
        self._syn.sort(key=lambda t: len(t[0]), reverse=True)
        self._dim_syn = self.m.dimension_synonym_index()

    # --- public -----------------------------------------------------------------------
    def plan(self, question: str) -> PlanResult:
        t = _norm(question)

        if t.strip().startswith("why"):
            return PlanResult(
                status="abstain",
                message="I can show what changed (a decomposition of the movement), but I "
                        "won't assert a cause from this data. Try, e.g., 'net new MRR by "
                        "month' or 'churned MRR by segment'.")

        metric, candidates = self._match_metric(t)
        if metric is None:
            if candidates:
                return PlanResult(status="clarify", candidates=candidates,
                                  message="Which did you mean? " +
                                          ", ".join(self.m.metric(c).label for c in candidates))
            return PlanResult(
                status="abstain",
                message="I don't have a metric for that. I can answer about revenue "
                        "(ARR, MRR, recognized revenue, billings), movement (new / "
                        "expansion / contraction / churn), pipeline (bookings, win rate, "
                        "open pipeline), and usage (MAU).")

        met = self.m.metric(metric)
        dims = self._match_dimensions(t, met.dimensions)
        filters = self._match_filters(t, met.dimensions)
        time = self._match_time(t)

        plan = QueryPlan.from_dict({
            "metric": metric, "dimensions": dims, "filters": filters, "time": time,
        })
        try:
            plan.validate(self.m)
        except PlanValidationError as e:  # pragma: no cover - pruning should prevent this
            return PlanResult(status="abstain", message=f"Could not build a valid plan: {e}")

        rationale = (f"metric={metric}"
                     + (f", by {'+'.join(dims)}" if dims else "")
                     + (f", filters={[f['dimension']+'='+','.join(f['values']) for f in filters]}"
                        if filters else "")
                     + f", time={time.get('type')}")
        return PlanResult(status="ok", plan=plan, rationale=rationale)

    # --- matchers ---------------------------------------------------------------------
    def _match_metric(self, t: str):
        # ambiguous bare terms first
        for term, options in AMBIGUOUS.items():
            if f" {term} " in t and not self._has_specific(t, options):
                return None, options
        for syn, name in self._syn:
            if f" {syn} " in t:
                return name, []
        return None, []

    def _has_specific(self, t: str, options: list[str]) -> bool:
        """True if a more specific synonym for one of the options is present."""
        for name in options:
            met = self.m.metric(name)
            for token in [met.label, *met.synonyms]:
                nt = _norm(token).strip()
                if nt != "revenue" and f" {nt} " in t:
                    return True
        return False

    def _match_dimensions(self, t: str, allowed: set[str]) -> list[str]:
        dims: list[str] = []
        # time grains via cue words
        if any(w in t for w in (" over time ", " trend ", " monthly ", " by month ")):
            dims.append("month")
        if any(w in t for w in (" quarterly ", " by quarter ", " qoq ")):
            dims.append("quarter")
        if any(w in t for w in (" yearly ", " annually ", " by year ")):
            dims.append("year")
        # explicit "by/per/across <dim synonym>"
        for syn, dim in self._dim_syn.items():
            for cue in _GROUP_CUES:
                if f" {cue} {syn} " in t and dim not in dims:
                    dims.append(dim)
        return [d for d in dict.fromkeys(dims) if d in allowed]

    def _match_filters(self, t: str, allowed: set[str]) -> list[dict]:
        by_dim: dict[str, list[str]] = {}
        for phrase, dim, value in VALUE_VOCAB:
            if dim in allowed and f" {phrase} " in t:
                by_dim.setdefault(dim, [])
                if value not in by_dim[dim]:
                    by_dim[dim].append(value)
        filters = []
        for dim, values in by_dim.items():
            op = "in" if len(values) > 1 else "eq"
            filters.append({"dimension": dim, "op": op, "values": values})
        return filters

    def _match_time(self, t: str) -> dict:
        m = re.search(r" last (\d+) months?", t)
        if m:
            return {"type": "last_n_months", "n": int(m.group(1))}
        if " last month " in t or " past month " in t:
            return {"type": "last_n_months", "n": 1}
        if " last quarter " in t or " previous quarter " in t or " prior quarter " in t:
            return {"type": "last_n_months", "n": 3}
        if any(w in t for w in (" ltm ", " last year ", " past year ", " last 12 months ",
                                " last twelve months ", " trailing twelve months ",
                                " trailing 12 months ")):
            return {"type": "last_n_months", "n": 12}
        # Q# YYYY  or  YYYY Q#
        m = re.search(r" q([1-4])\s*(20\d\d) ", t) or re.search(r" (20\d\d)\s*q([1-4]) ", t)
        if m:
            g = m.groups()
            q, y = (g[0], g[1]) if len(g[0]) == 1 else (g[1], g[0])
            return {"type": "quarter", "value": f"{y}-Q{q}"}
        # Month YYYY
        months = ("january february march april may june july august september october "
                  "november december").split()
        mm = re.search(r" (" + "|".join(m[:3] + "[a-z]*" for m in months) + r") (20\d\d) ", t)
        if mm:
            mon = mm.group(1)[:3]
            idx = [m[:3] for m in months].index(mon) + 1
            return {"type": "month", "value": f"{mm.group(2)}-{idx:02d}"}
        m = re.search(r" (20\d\d) ", t)
        if m:
            return {"type": "year", "value": m.group(1)}
        return {"type": "as_of_latest"}
