"""Independent gold-answer oracle.

This computes the "true" value for a plan a SECOND way -- hand-written SQL over the base
tables, with row-level security applied by a SEPARATE mechanism (compute the visible
account set from grants, then filter to it). It deliberately does NOT use the semantic
manifest's fact SQL or the compiler's injected predicate. When the engine and this oracle
agree across two independent fixtures, a query can't have been "right by luck" or right
because a single code path was self-consistent.
"""

from __future__ import annotations

from datetime import date

from copilot.compiler.compile import _resolve_window, _target_month
from copilot.compiler.plan import QueryPlan
from copilot.security.principal import Principal, Grant

_ACCOUNT_DIMS = {
    "region": "region", "segment": "segment", "country": "country",
    "sub_region": "sub_region", "account_tier": "account_tier", "industry": "industry",
}


def visible_accounts(con, principal: Principal, as_of: str) -> set[str]:
    """Resolve the set of account_ids a principal may see -- independent of rls.py."""
    if principal.is_unrestricted:
        return {r[0] for r in con.execute("SELECT account_id FROM accounts").fetchall()}
    ids: set[str] = set()
    for g in principal.grants:
        if g.scope_type == "all":
            return {r[0] for r in con.execute("SELECT account_id FROM accounts").fetchall()}
        if g.scope_type in _ACCOUNT_DIMS:
            q = f"SELECT account_id FROM accounts WHERE {_ACCOUNT_DIMS[g.scope_type]} = ?"
            rows = con.execute(q, [g.scope_value]).fetchall()
        elif g.scope_type == "account":
            rows = con.execute("SELECT account_id FROM accounts WHERE account_id = ?",
                               [g.scope_value]).fetchall()
        elif g.scope_type == "owned_accounts":
            rows = con.execute(
                "SELECT account_id FROM account_ownership WHERE owner_user_id = ? "
                "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)",
                [g.scope_value, as_of, as_of]).fetchall()
        else:
            rows = []
        ids.update(r[0] for r in rows)
    return ids


def _in_clause(alias: str, ids: set[str]) -> tuple[str, list]:
    if not ids:
        return "1=0", []              # visible nothing -> fail closed
    ph = ", ".join("?" for _ in ids)
    return f"{alias}.account_id IN ({ph})", list(ids)


def _filters(plan: QueryPlan, opp_alias: str | None = None) -> tuple[list[str], list]:
    clauses, params = [], []
    for f in plan.filters:
        if f.dimension in _ACCOUNT_DIMS:
            col = f"a.{_ACCOUNT_DIMS[f.dimension]}"
        elif f.dimension == "opp_type" and opp_alias:
            col = f"{opp_alias}.opp_type"
        else:
            continue
        if f.op == "eq":
            clauses.append(f"{col} = ?")
            params.append(f.values[0])
        else:
            ph = ", ".join("?" for _ in f.values)
            op = "NOT IN" if f.op == "not_in" else "IN"
            clauses.append(f"{col} {op} ({ph})")
            params.extend(f.values)
    return clauses, params


def compute_oracle(con, plan: QueryPlan, principal: Principal, as_of_str: str):
    """Return (value, value_kind) for a scalar plan (no group-by), computed independently."""
    as_of = date.fromisoformat(as_of_str)
    vis = visible_accounts(con, principal, as_of_str)  # ownership cols are ISO strings
    metric = plan.metric

    def scalar(sql: str, params: list):
        row = con.execute(sql, params).fetchone()
        return row[0] if row and row[0] is not None else 0

    # revenue_schedule point-in-time / range
    if metric in ("arr", "mrr", "active_customers", "recognized_revenue"):
        where = ["a.is_internal = FALSE"]
        params: list = []
        if metric == "recognized_revenue":
            s, e = _resolve_window(plan.time, as_of, "last_12_months")
            where.append("CAST(rs.month_start AS DATE) BETWEEN ? AND ?")
            params += [s, e]
        else:
            tm = _target_month(plan.time, as_of)
            where.append("date_trunc('month', CAST(rs.month_start AS DATE)) = ?")
            params.append(tm)
        inc, ip = _in_clause("a", vis); where.append(inc); params += ip
        fc, fp = _filters(plan); where += fc; params += fp
        frm = "FROM revenue_schedule rs JOIN accounts a USING(account_id)"
        w = " AND ".join(where)
        if metric == "arr":
            return round(float(scalar(f"SELECT SUM(rs.recognized_amount_usd)*12 {frm} WHERE {w}", params)), 2), "currency"
        if metric in ("mrr", "recognized_revenue"):
            return round(float(scalar(f"SELECT SUM(rs.recognized_amount_usd) {frm} WHERE {w}", params)), 2), "currency"
        return int(scalar(f"SELECT COUNT(DISTINCT rs.account_id) {frm} WHERE {w}", params)), "count"

    # subscription movement (range on event_date)
    movement = {"new_mrr": "'new'", "expansion_mrr": "'expansion'",
                "contraction_mrr": "'contraction'", "churned_mrr": "'churn'"}
    if metric in movement or metric == "net_new_mrr":
        s, e = _resolve_window(plan.time, as_of, "last_12_months")
        where = ["a.is_internal = FALSE", "CAST(se.event_date AS DATE) BETWEEN ? AND ?"]
        params = [s, e]
        if metric in movement:
            where.append(f"se.event_type = {movement[metric]}")
        else:
            where.append("se.event_type IN ('new','expansion','contraction','churn','reactivation')")
        inc, ip = _in_clause("a", vis); where.append(inc); params += ip
        fc, fp = _filters(plan); where += fc; params += fp
        w = " AND ".join(where)
        return round(float(scalar(
            f"SELECT SUM(se.mrr_delta_usd) FROM subscription_events se "
            f"JOIN accounts a USING(account_id) WHERE {w}", params)), 2), "currency"

    # bookings (range on close_date, won)
    if metric in ("bookings", "new_business_bookings", "average_acv"):
        s, e = _resolve_window(plan.time, as_of, "last_12_months")
        where = ["a.is_internal = FALSE", "o.is_won", "CAST(o.close_date AS DATE) BETWEEN ? AND ?"]
        params = [s, e]
        if metric != "bookings":
            where.append("o.opp_type = 'New Business'")
        inc, ip = _in_clause("a", vis); where.append(inc); params += ip
        fc, fp = _filters(plan, opp_alias="o"); where += fc; params += fp
        w = " AND ".join(where)
        agg = "AVG(o.amount_usd)" if metric == "average_acv" else "SUM(o.amount_usd)"
        return round(float(scalar(
            f"SELECT {agg} FROM opportunities o JOIN accounts a USING(account_id) WHERE {w}", params)), 2), "currency"

    # win rate (range on close_date)
    if metric == "win_rate":
        s, e = _resolve_window(plan.time, as_of, "last_12_months")
        base = ["a.is_internal = FALSE", "o.opp_type = 'New Business'",
                "CAST(o.close_date AS DATE) BETWEEN ? AND ?"]
        inc, ip = _in_clause("a", vis)
        fc, fp = _filters(plan, opp_alias="o")
        common = base + [inc] + fc
        cp = [s, e] + ip + fp
        w = " AND ".join(common)
        num = scalar(f"SELECT COUNT(*) FROM opportunities o JOIN accounts a USING(account_id) WHERE {w} AND o.is_won", cp)
        den = scalar(f"SELECT COUNT(*) FROM opportunities o JOIN accounts a USING(account_id) WHERE {w} AND o.is_closed", cp)
        return (round(num / den, 4) if den else 0.0), "ratio"

    # open pipeline (snapshot)
    if metric == "open_pipeline":
        where = ["a.is_internal = FALSE", "NOT o.is_closed"]
        params = []
        inc, ip = _in_clause("a", vis); where.append(inc); params += ip
        fc, fp = _filters(plan, opp_alias="o"); where += fc; params += fp
        w = " AND ".join(where)
        return round(float(scalar(
            f"SELECT SUM(o.amount_usd) FROM opportunities o JOIN accounts a USING(account_id) WHERE {w}", params)), 2), "currency"

    # billings / net revenue (range on invoice/refund date)
    if metric in ("gross_billings", "net_revenue"):
        s, e = _resolve_window(plan.time, as_of, "last_12_months")
        where = ["a.is_internal = FALSE", "i.status <> 'void'",
                 "CAST(i.invoice_date AS DATE) BETWEEN ? AND ?"]
        params = [s, e]
        inc, ip = _in_clause("a", vis); where.append(inc); params += ip
        fc, fp = _filters(plan); where += fc; params += fp
        w = " AND ".join(where)
        gross = float(scalar(
            f"SELECT SUM(i.amount_usd) FROM invoices i JOIN accounts a USING(account_id) WHERE {w}", params))
        if metric == "gross_billings":
            return round(gross, 2), "currency"
        rwhere = ["a.is_internal = FALSE", "CAST(r.refund_date AS DATE) BETWEEN ? AND ?"]
        rparams = [s, e]
        rinc, rip = _in_clause("a", vis); rwhere.append(rinc); rparams += rip
        rfc, rfp = _filters(plan); rwhere += rfc; rparams += rfp
        refunds = float(scalar(
            f"SELECT SUM(r.amount_usd) FROM refunds r JOIN accounts a USING(account_id) "
            f"WHERE {' AND '.join(rwhere)}", rparams))
        return round(gross - refunds, 2), "currency"

    # usage
    if metric in ("mau", "active_usage_accounts"):
        tm = _target_month(plan.time, as_of)
        where = ["a.is_internal = FALSE",
                 "date_trunc('month', CAST(ue.event_local_date AS DATE)) = ?"]
        params = [tm]
        inc, ip = _in_clause("a", vis); where.append(inc); params += ip
        fc, fp = _filters(plan); where += fc; params += fp
        w = " AND ".join(where)
        col = "ue.end_user_id" if metric == "mau" else "ue.account_id"
        return int(scalar(
            f"SELECT COUNT(DISTINCT {col}) FROM usage_events ue JOIN accounts a USING(account_id) WHERE {w}", params)), "count"

    raise ValueError(f"oracle has no independent computation for metric {metric!r}")


def unrestricted_principal() -> Principal:
    return Principal(user_id="__oracle_all__", label="oracle-all", grants=[Grant("all", "*")])
