"""The structured query plan the LLM emits (instead of SQL) and its validation.

A plan is intentionally small and closed: a metric, some dimensions to group by, some
filters, and a time spec. Validation against the manifest is a hard gate -- a plan that
references an unknown metric/dimension, or filters on something the metric doesn't support,
is rejected before any SQL is built. That is what stops a hallucinated field from ever
reaching the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..semantic.model import Manifest

VALID_FILTER_OPS = {"in", "eq", "not_in"}
VALID_TIME_TYPES = {
    "all", "last_n_months", "quarter", "year", "between", "month", "as_of_latest",
}


class PlanValidationError(ValueError):
    """Raised when a plan cannot be validated against the manifest."""


@dataclass
class Filter:
    dimension: str
    op: str
    values: list[str]

    @classmethod
    def from_dict(cls, d: dict) -> "Filter":
        return cls(dimension=d["dimension"], op=d.get("op", "in"),
                   values=[str(v) for v in (d.get("values") or [])])


@dataclass
class TimeSpec:
    type: str = "as_of_latest"
    n: int | None = None
    value: str | None = None
    start: str | None = None
    end: str | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> "TimeSpec":
        if not d:
            return cls(type="as_of_latest")
        return cls(type=d.get("type", "as_of_latest"), n=d.get("n"),
                   value=(str(d["value"]) if d.get("value") is not None else None),
                   start=d.get("start"), end=d.get("end"))


@dataclass
class QueryPlan:
    metric: str
    dimensions: list[str] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)
    time: TimeSpec = field(default_factory=TimeSpec)
    limit: int | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "QueryPlan":
        return cls(
            metric=d.get("metric", ""),
            dimensions=list(d.get("dimensions", [])),
            filters=[Filter.from_dict(f) for f in d.get("filters", [])],
            time=TimeSpec.from_dict(d.get("time")),
            limit=d.get("limit"),
        )

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "dimensions": self.dimensions,
            "filters": [f.__dict__ for f in self.filters],
            "time": {k: v for k, v in self.time.__dict__.items() if v is not None},
            "limit": self.limit,
        }

    # --- validation -------------------------------------------------------------------
    def validate(self, manifest: Manifest) -> None:
        met = manifest.metric(self.metric)
        if met is None:
            near = _suggest(self.metric, manifest.metric_names())
            raise PlanValidationError(
                f"Unknown metric {self.metric!r}."
                + (f" Did you mean {near!r}?" if near else ""))

        for dim in self.dimensions:
            if dim not in manifest.dimensions:
                raise PlanValidationError(f"Unknown dimension {dim!r}.")
            if dim not in met.dimensions:
                raise PlanValidationError(
                    f"Dimension {dim!r} is not available for metric {self.metric!r}. "
                    f"Available: {sorted(met.dimensions)}")

        for f in self.filters:
            if f.dimension not in manifest.dimensions:
                raise PlanValidationError(f"Unknown filter dimension {f.dimension!r}.")
            if f.dimension not in met.dimensions:
                raise PlanValidationError(
                    f"Cannot filter metric {self.metric!r} by {f.dimension!r}.")
            if manifest.dimensions[f.dimension].source == "time":
                raise PlanValidationError(
                    f"Use the `time` spec, not a filter, for {f.dimension!r}.")
            if f.op not in VALID_FILTER_OPS:
                raise PlanValidationError(f"Unknown filter op {f.op!r}.")
            if not f.values:
                raise PlanValidationError(f"Filter on {f.dimension!r} has no values.")

        if self.time.type not in VALID_TIME_TYPES:
            raise PlanValidationError(f"Unknown time type {self.time.type!r}.")
        if self.time.type == "last_n_months" and not self.time.n:
            raise PlanValidationError("time.last_n_months requires n.")
        if self.time.type == "between" and not (self.time.start and self.time.end):
            raise PlanValidationError("time.between requires start and end.")
        if self.time.type in ("quarter", "year", "month") and not self.time.value:
            raise PlanValidationError(f"time.{self.time.type} requires a value.")
        # snapshot metrics ignore time; that's fine (compiler drops it).


def _suggest(word: str, options: list[str]) -> str | None:
    """Cheap nearest-name suggestion (shared prefix / substring)."""
    word = word.lower()
    for o in options:
        if word and (word in o or o in word):
            return o
    return None
