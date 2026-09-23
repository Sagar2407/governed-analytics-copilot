"""Load and validate the semantic manifest into typed objects.

The manifest is the allow-list the compiler builds against: a plan may only reference
metrics and dimensions defined here. Loading fails fast if the manifest is internally
inconsistent (unknown fact, unsupported dimension, bad aggregation), so a broken semantic
layer can never reach query time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID_AGGS = {"sum", "avg", "count_rows", "count_distinct_account", "count_distinct_expr"}
VALID_TIME_MODES = {"range", "point_in_time", "snapshot"}
VALID_SOURCES = {"account", "fact", "time"}


@dataclass
class Dimension:
    name: str
    source: str
    column: str | None = None
    is_expression: bool = False
    grain: str | None = None
    synonyms: list[str] = field(default_factory=list)


@dataclass
class Metric:
    name: str
    label: str
    category: str
    description: str
    synonyms: list[str] = field(default_factory=list)
    kind: str = "simple"                 # simple | ratio
    fact: str = ""
    fact_filter: str | None = None
    value_column: str = "value_usd"
    agg: str = "sum"
    value_multiplier: float = 1.0
    time_mode: str = "range"
    count_expr: str | None = None
    numerator_filter: str | None = None
    denominator_filter: str | None = None
    dimensions: set[str] = field(default_factory=set)


@dataclass
class Manifest:
    version: int
    default_currency: str
    grain_entity: str
    dimensions: dict[str, Dimension]
    facts: dict[str, str]
    metrics: dict[str, Metric]

    # --- lookups ----------------------------------------------------------------------
    def metric(self, name: str) -> Metric | None:
        return self.metrics.get(name)

    def dimension(self, name: str) -> Dimension | None:
        return self.dimensions.get(name)

    def metric_names(self) -> list[str]:
        return sorted(self.metrics)

    def synonym_index(self) -> dict[str, list[str]]:
        """Map every metric name + synonym (lowercased) -> metric name, for retrieval."""
        idx: dict[str, list[str]] = {}
        for m in self.metrics.values():
            for token in [m.name, m.label, *m.synonyms]:
                idx.setdefault(token.lower(), []).append(m.name)
        return idx

    def dimension_synonym_index(self) -> dict[str, str]:
        idx: dict[str, str] = {}
        for d in self.dimensions.values():
            for token in [d.name, *d.synonyms]:
                idx[token.lower()] = d.name
        return idx


def load_manifest(path: str | Path | None = None) -> Manifest:
    if path is None:
        path = Path(__file__).with_name("manifest.yaml")
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))

    dims: dict[str, Dimension] = {}
    for name, d in (raw.get("dimensions") or {}).items():
        dims[name] = Dimension(
            name=name,
            source=d["source"],
            column=d.get("column"),
            is_expression=bool(d.get("is_expression", False)),
            grain=d.get("grain"),
            synonyms=list(d.get("synonyms", [])),
        )

    facts: dict[str, str] = dict(raw.get("facts") or {})

    metrics: dict[str, Metric] = {}
    for name, m in (raw.get("metrics") or {}).items():
        metrics[name] = Metric(
            name=name,
            label=m.get("label", name),
            category=m.get("category", "other"),
            description=m.get("description", ""),
            synonyms=list(m.get("synonyms", [])),
            kind=m.get("kind", "simple"),
            fact=m.get("fact", ""),
            fact_filter=m.get("fact_filter"),
            value_column=m.get("value_column", "value_usd"),
            agg=m.get("agg", "sum"),
            value_multiplier=float(m.get("value_multiplier", 1.0)),
            time_mode=m.get("time_mode", "range"),
            count_expr=m.get("count_expr"),
            numerator_filter=m.get("numerator_filter"),
            denominator_filter=m.get("denominator_filter"),
            dimensions=set(m.get("dimensions", [])),
        )

    manifest = Manifest(
        version=int(raw.get("version", 1)),
        default_currency=raw.get("default_currency", "USD"),
        grain_entity=raw.get("grain_entity", "account"),
        dimensions=dims,
        facts=facts,
        metrics=metrics,
    )
    _validate(manifest)
    return manifest


def _validate(m: Manifest) -> None:
    errors: list[str] = []
    for dname, d in m.dimensions.items():
        if d.source not in VALID_SOURCES:
            errors.append(f"dimension {dname}: bad source {d.source!r}")
        if d.source in ("account", "fact") and not d.column:
            errors.append(f"dimension {dname}: missing column")
        if d.source == "time" and not d.grain:
            errors.append(f"dimension {dname}: time dimension needs a grain")
    for name, met in m.metrics.items():
        if met.kind not in ("simple", "ratio"):
            errors.append(f"metric {name}: bad kind {met.kind!r}")
        if met.fact not in m.facts:
            errors.append(f"metric {name}: unknown fact {met.fact!r}")
        if met.time_mode not in VALID_TIME_MODES:
            errors.append(f"metric {name}: bad time_mode {met.time_mode!r}")
        if met.kind == "simple" and met.agg not in VALID_AGGS:
            errors.append(f"metric {name}: bad agg {met.agg!r}")
        if met.agg == "count_distinct_expr" and not met.count_expr:
            errors.append(f"metric {name}: count_distinct_expr needs count_expr")
        if met.kind == "ratio" and not (met.numerator_filter and met.denominator_filter):
            errors.append(f"metric {name}: ratio needs numerator_filter + denominator_filter")
        for dim in met.dimensions:
            if dim not in m.dimensions:
                errors.append(f"metric {name}: unknown dimension {dim!r}")
    if errors:
        raise ValueError("Invalid semantic manifest:\n  - " + "\n  - ".join(errors))
