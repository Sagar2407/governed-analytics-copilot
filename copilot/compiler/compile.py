"""Compile a validated QueryPlan into parameterized DuckDB SQL.

Only manifest-defined identifiers (fact SQL, dimension columns, aggregations) are ever
interpolated into the string; everything that originates from the plan (filter values, time
bounds) and the principal (RLS values) is bound as a parameter. Combined with plan
validation, that makes the plan->SQL path injection-safe by construction.

Uniform shape:

    SELECT <dims>, <aggregate> AS value
    FROM ( <fact sql> ) f
    JOIN accounts a ON f.account_id = a.account_id
    WHERE a.is_internal = FALSE      -- eligibility
      [AND <metric fact filter>]     -- authored
      [AND <time predicate>]         -- parameterized
      [AND <plan filters>]           -- parameterized
      AND <rls predicate>            -- parameterized, injected per caller
    [GROUP BY <dims>] [ORDER BY <dims>] [LIMIT n]
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date

from ..semantic.model import Manifest, Metric
from .plan import QueryPlan, Filter


@dataclass
class CompiledQuery:
    sql: str
    params: list
    value_kind: str            # currency | count | ratio | number
    grouped_by: list[str]
    lineage: dict = field(default_factory=dict)


# --- date helpers ---------------------------------------------------------------------

def _month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def _add_months(d: date, k: int) -> date:
    m = d.month - 1 + k
    return date(d.year + m // 12, m % 12 + 1, 1)


def _month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _parse_month(s: str) -> date:
    y, m = s.split("-")[:2]
    return date(int(y), int(m), 1)


def _quarter_range(s: str) -> tuple[date, date]:
    y, q = s.upper().split("-Q")
    y, q = int(y), int(q)
    return date(y, 3 * (q - 1) + 1, 1), _month_end(date(y, 3 * q, 1))


def _year_range(v) -> tuple[date, date]:
    y = int(v)
    return date(y, 1, 1), date(y, 12, 31)


def _resolve_window(time, as_of: date, default: str | None) -> tuple[date, date] | None:
    t = time.type
    if t == "as_of_latest":
        t = default
    if t in (None, "all"):
        return None
    if t == "last_n_months":
        n = int(time.n)
        return _add_months(_month_start(as_of), -(n - 1)), _month_end(as_of)
    if t == "last_12_months":
        return _add_months(_month_start(as_of), -11), _month_end(as_of)
    if t == "between":
        return date.fromisoformat(time.start), date.fromisoformat(time.end)
    if t == "quarter":
        return _quarter_range(time.value)
    if t == "year":
        return _year_range(time.value)
    if t == "month":
        d = _parse_month(time.value)
        return _month_start(d), _month_end(d)
    return None


def _target_month(time, as_of: date) -> date:
    t = time.type
    if t in ("as_of_latest", "all", "last_n_months"):
        return _month_start(as_of)
    if t == "month":
        return _parse_month(time.value)
    if t == "quarter":
        _, end = _quarter_range(time.value)
        return _month_start(end)
    if t == "year":
        return date(int(time.value), 12, 1)
    if t == "between":
        return _month_start(date.fromisoformat(time.end))
    return _month_start(as_of)


def _time_clause(met: Metric, time, as_of: date, has_time_dim: bool):
    """Return (params, sql_clause, human_description)."""
    if met.time_mode == "snapshot":
        return [], "", "current snapshot (open state)"
    if has_time_dim:
        window = _resolve_window(time, as_of, default="last_12_months")
        if window is None:
            return [], "", "all time, by period"
        s, e = window
        return [s, e], "f.period_date BETWEEN ? AND ?", f"{s} to {e}, by period"
    if met.time_mode == "point_in_time":
        tm = _target_month(time, as_of)
        return [tm], "date_trunc('month', f.period_date) = ?", f"as of {tm:%Y-%m}"
    window = _resolve_window(time, as_of, default="last_12_months")
    if window is None:
        return [], "", "all time"
    s, e = window
    return [s, e], "f.period_date BETWEEN ? AND ?", f"{s} to {e}"


# --- aggregate + filter builders ------------------------------------------------------

def _agg_select(met: Metric) -> tuple[str, str]:
    if met.kind == "ratio":
        num = f"SUM(CASE WHEN {met.numerator_filter} THEN 1 ELSE 0 END)"
        den = f"SUM(CASE WHEN {met.denominator_filter} THEN 1 ELSE 0 END)"
        sel = (f"{num} AS numerator, {den} AS denominator, "
               f"ROUND({num} * 1.0 / NULLIF({den}, 0), 4) AS value")
        return sel, "ratio"
    vc = f"f.{met.value_column}"
    if met.agg == "sum":
        inner = f"SUM({vc}) * {met.value_multiplier}" if met.value_multiplier != 1.0 else f"SUM({vc})"
        return f"ROUND({inner}, 2) AS value", "currency"
    if met.agg == "avg":
        return f"ROUND(AVG({vc}), 2) AS value", "currency"
    if met.agg == "count_rows":
        return "COUNT(*) AS value", "count"
    if met.agg == "count_distinct_account":
        return "COUNT(DISTINCT f.account_id) AS value", "count"
    if met.agg == "count_distinct_expr":
        return f"COUNT(DISTINCT f.{met.count_expr}) AS value", "count"
    raise ValueError(f"unsupported agg {met.agg!r}")


def _dim_expr(manifest: Manifest, dim: str) -> str:
    d = manifest.dimensions[dim]
    if d.source == "account":
        return d.column if d.is_expression else f"a.{d.column}"
    if d.source == "fact":
        return f"f.{d.column}"
    return f"date_trunc('{d.grain}', f.period_date)"  # time


def _filter_clause(expr: str, f: Filter) -> tuple[str, list]:
    if f.op == "eq":
        return f"{expr} = ?", [f.values[0]]
    placeholders = ", ".join("?" for _ in f.values)
    op = "NOT IN" if f.op == "not_in" else "IN"
    return f"{expr} {op} ({placeholders})", list(f.values)


# --- main -----------------------------------------------------------------------------

def compile_plan(manifest: Manifest, plan: QueryPlan, rls_predicate: str,
                 rls_params: list, as_of: date, currency: str = "USD") -> CompiledQuery:
    met = manifest.metric(plan.metric)
    fact_sql = manifest.facts[met.fact]

    dim_selects, group_exprs = [], []
    has_time_dim = False
    for dim in plan.dimensions:
        expr = _dim_expr(manifest, dim)
        if manifest.dimensions[dim].source == "time":
            has_time_dim = True
        dim_selects.append(f"{expr} AS {dim}")
        group_exprs.append(expr)

    where: list[str] = ["a.is_internal = FALSE"]
    params: list = []

    if met.kind == "simple" and met.fact_filter:
        where.append(f"({met.fact_filter})")

    tparams, tclause, tdesc = _time_clause(met, plan.time, as_of, has_time_dim)
    if tclause:
        where.append(tclause)
        params.extend(tparams)

    filt_desc = []
    for f in plan.filters:
        expr = _dim_expr(manifest, f.dimension)
        clause, fp = _filter_clause(expr, f)
        where.append(clause)
        params.extend(fp)
        filt_desc.append(f"{f.dimension} {f.op} {f.values}")

    where.append(rls_predicate)
    params.extend(rls_params)

    agg_select, value_kind = _agg_select(met)
    select_parts = dim_selects + [agg_select]

    sql = (f"SELECT {', '.join(select_parts)}\n"
           f"FROM (\n{fact_sql.strip()}\n) f\n"
           f"JOIN accounts a ON f.account_id = a.account_id\n"
           f"WHERE {' AND '.join(where)}")
    if group_exprs:
        sql += f"\nGROUP BY {', '.join(group_exprs)}"
        sql += f"\nORDER BY {', '.join(group_exprs)}"
    if plan.limit:
        sql += f"\nLIMIT {int(plan.limit)}"

    lineage = {
        "metric": met.name,
        "metric_label": met.label,
        "fact": met.fact,
        "aggregation": met.agg if met.kind == "simple" else "ratio",
        "value_kind": value_kind,
        "currency": currency if value_kind == "currency" else None,
        "time_window": tdesc,
        "grouped_by": list(plan.dimensions),
        "filters": filt_desc,
        "row_grain": manifest.grain_entity,
        "eligibility": "excludes internal/test accounts (is_internal = FALSE)",
    }
    return CompiledQuery(sql=sql, params=params, value_kind=value_kind,
                         grouped_by=list(plan.dimensions), lineage=lineage)
