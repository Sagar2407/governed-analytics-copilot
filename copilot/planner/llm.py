"""LLM planner: natural language -> structured plan via a model, provider-agnostic.

The model NEVER writes SQL and never sees data. It calls a single `respond` tool whose
schema mirrors the QueryPlan (or signals clarify/abstain). The returned plan is validated
against the manifest exactly like the heuristic planner's output, so the trust boundary is
identical no matter who proposes the plan.

Providers:
  * anthropic     (default) -- Claude via the official `anthropic` SDK, model claude-opus-5
  * azure_openai            -- Azure OpenAI via the `openai` SDK (mirrors a common enterprise stack)

SDKs and credentials are resolved lazily from the environment; importing this module does
not require either SDK to be installed. No API keys are read from or written to source.
"""

from __future__ import annotations

import json
import os

from ..semantic.model import Manifest, load_manifest
from ..compiler.plan import QueryPlan, PlanValidationError
from .base import Planner, PlanResult

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"

# JSON schema for a QueryPlan (used as the tool input schema).
_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "metric": {"type": "string", "description": "exact metric name from the catalog"},
        "dimensions": {"type": "array", "items": {"type": "string"},
                       "description": "group-by dimensions (incl. month/quarter/year)"},
        "filters": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "dimension": {"type": "string"},
                "op": {"type": "string", "enum": ["in", "eq", "not_in"]},
                "values": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["dimension", "values"],
        }},
        "time": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": [
                    "all", "last_n_months", "quarter", "year", "between", "month",
                    "as_of_latest"]},
                "n": {"type": "integer"},
                "value": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
            },
        },
    },
    "required": ["metric"],
}

_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["answer", "clarify", "abstain"]},
        "plan": _PLAN_SCHEMA,
        "message": {"type": "string",
                    "description": "clarification question (clarify) or reason (abstain)"},
    },
    "required": ["action"],
}


class LLMPlanner(Planner):
    def __init__(self, provider: str | None = None, model: str | None = None,
                 manifest: Manifest | None = None):
        self.m = manifest or load_manifest()
        self.provider = provider or os.environ.get("COPILOT_LLM_PROVIDER", "anthropic")
        self.model = model or os.environ.get("COPILOT_LLM_MODEL", DEFAULT_ANTHROPIC_MODEL)
        self.system_prompt = self._build_system_prompt()

    # --- prompt / schema (offline-testable) -------------------------------------------
    def _build_system_prompt(self) -> str:
        lines = [
            "You translate a business question into a STRUCTURED QUERY PLAN for a governed "
            "analytics engine. You never write SQL and never see data. Choose exactly one "
            "metric from the catalog and only dimensions that metric supports.",
            "",
            "Call the `respond` tool with action:",
            "  answer  -> provide `plan` (metric, dimensions, filters, time)",
            "  clarify -> the question is ambiguous; ask a short question in `message`",
            "  abstain -> no catalog metric answers this (incl. causal 'why' questions); "
            "explain briefly in `message`. Prefer a decomposition over asserting cause.",
            "",
            "TIME: use time.type one of all | last_n_months(n) | quarter(value='YYYY-Qn') | "
            "year(value='YYYY') | between(start,end ISO dates) | month(value='YYYY-MM') | "
            "as_of_latest. Point-in-time metrics (ARR, MRR, active customers) default to the "
            "latest period; range metrics default to trailing 12 months.",
            "",
            "METRIC CATALOG:",
        ]
        for name in self.m.metric_names():
            met = self.m.metric(name)
            dims = ", ".join(sorted(met.dimensions)) or "(none)"
            lines.append(f"- {name} [{met.category}]: {met.description} "
                         f"(dimensions: {dims})")
        lines += [
            "",
            "DIMENSION VALUES (for filters):",
            "  region: NA, EMEA, APAC, LATAM",
            "  segment: SMB, Mid-Market, Enterprise",
            "  opp_type: New Business, Expansion, Renewal",
            "  stage: Prospecting, Qualification, Proposal, Negotiation",
            "Use the exact metric/dimension names and values above; do not invent fields.",
        ]
        return "\n".join(lines)

    def tool_definition(self) -> dict:
        return {"name": "respond",
                "description": "Respond with a query plan, a clarification, or an abstention.",
                "input_schema": _TOOL_SCHEMA}

    # --- public -----------------------------------------------------------------------
    def plan(self, question: str) -> PlanResult:
        raw = self._call_model(question)
        return self._interpret(raw)

    def _interpret(self, raw: dict) -> PlanResult:
        action = (raw or {}).get("action")
        if action == "clarify":
            return PlanResult(status="clarify", message=raw.get("message", "Please clarify."))
        if action == "abstain":
            return PlanResult(status="abstain",
                              message=raw.get("message", "I can't answer that from this data."))
        plan_dict = raw.get("plan") or {}
        plan = QueryPlan.from_dict(plan_dict)
        try:
            plan.validate(self.m)
        except PlanValidationError as e:
            return PlanResult(status="abstain",
                              message=f"Proposed plan was invalid ({e}).")
        return PlanResult(status="ok", plan=plan,
                          rationale=f"llm:{self.provider}:{self.model} -> {plan.to_dict()}")

    # --- providers --------------------------------------------------------------------
    def _call_model(self, question: str) -> dict:
        if self.provider == "anthropic":
            return self._call_anthropic(question)
        if self.provider == "azure_openai":
            return self._call_azure_openai(question)
        raise ValueError(f"unknown provider {self.provider!r}")

    def _call_anthropic(self, question: str) -> dict:
        import anthropic  # lazy: only needed when this provider is used

        client = anthropic.Anthropic()  # resolves key/profile from environment
        tool = self.tool_definition()
        resp = client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=self.system_prompt,
            output_config={"effort": "low"},   # a small structured-extraction task
            tools=[tool],
            tool_choice={"type": "auto"},       # + instruction; avoids forced-tool 400s
            messages=[{"role": "user", "content": question}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "respond":
                return block.input  # already a dict; never string-match tool inputs
        # model answered in text without the tool -> treat as abstain
        return {"action": "abstain", "message": "No structured plan was produced."}

    def _call_azure_openai(self, question: str) -> dict:
        from openai import AzureOpenAI  # lazy

        client = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ.get("AZURE_OPENAI_API_KEY"),
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        )
        deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", self.model)
        resp = client.chat.completions.create(
            model=deployment,
            messages=[{"role": "system", "content": self.system_prompt},
                      {"role": "user", "content": question}],
            tools=[{"type": "function", "function": {
                "name": "respond", "parameters": _TOOL_SCHEMA}}],
            tool_choice={"type": "function", "function": {"name": "respond"}},
            temperature=0,
        )
        call = resp.choices[0].message.tool_calls[0]
        return json.loads(call.function.arguments)


def make_planner(provider: str | None = None, model: str | None = None,
                 manifest: Manifest | None = None) -> LLMPlanner:
    return LLMPlanner(provider=provider, model=model, manifest=manifest)


if __name__ == "__main__":  # offline dry run: print the prompt + tool schema, no API call
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.parse_args()
    p = LLMPlanner()
    print(p.system_prompt)
    print("\n--- tool schema ---")
    print(json.dumps(p.tool_definition(), indent=2)[:800], "...")
