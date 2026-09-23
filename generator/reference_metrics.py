"""Independent reference-metric computation over the raw tables.

These metrics are computed here straight from the generated tables (excluding internal
accounts, using USD, at the correct grain) with NO query compiler involved. They serve two
purposes: (1) a coherence check that the generator produced a sane business, and (2) the
seed for the eval's ground-truth oracle -- gold answers should be derivable the same way,
independent of whatever query the copilot writes.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _non_internal_accounts(accounts: pd.DataFrame) -> set:
    return set(accounts.loc[~accounts["is_internal"], "account_id"])


def compute_reference_metrics(tables: dict) -> tuple[dict, dict]:
    accounts = tables["accounts"]
    schedule = tables["revenue_schedule"].copy()
    events = tables["subscription_events"].copy()
    opps = tables["opportunities"].copy()
    usage = tables.get("usage_events")

    keep = _non_internal_accounts(accounts)
    schedule = schedule[schedule["account_id"].isin(keep)].copy()
    events = events[events["account_id"].isin(keep)].copy()
    opps = opps[opps["account_id"].isin(keep)].copy()

    schedule["month_start"] = pd.to_datetime(schedule["month_start"])
    out: dict[str, pd.DataFrame] = {}

    # --- ARR / MRR run-rate ------------------------------------------------------------
    arr = (schedule.groupby("month_start")["recognized_amount_usd"].sum()
           .reset_index().rename(columns={"recognized_amount_usd": "mrr_usd"}))
    arr["arr_usd"] = (arr["mrr_usd"] * 12).round(2)
    out["monthly_arr"] = arr

    out["monthly_recognized_revenue"] = (
        schedule.groupby("month_start")["recognized_amount_usd"].sum().reset_index()
        .rename(columns={"recognized_amount_usd": "recognized_revenue_usd"}))

    # --- MRR movement bridge -----------------------------------------------------------
    ev = events.copy()
    ev["month"] = pd.to_datetime(ev["event_date"]).values.astype("datetime64[M]")
    bridge = (ev.pivot_table(index="month", columns="event_type",
                             values="mrr_delta_usd", aggfunc="sum", fill_value=0.0)
              .reset_index())
    for col in ["new", "expansion", "contraction", "churn", "reactivation", "renewal"]:
        if col not in bridge.columns:
            bridge[col] = 0.0
    bridge["net_new_mrr"] = (bridge["new"] + bridge["expansion"] + bridge["reactivation"]
                             + bridge["contraction"] + bridge["churn"])
    out["mrr_movement"] = bridge

    # --- Logo movement (from monthly active presence) ----------------------------------
    months = sorted(schedule["month_start"].unique())
    present = {m: set(g["account_id"]) for m, g in schedule.groupby("month_start")}
    logo_rows = []
    prev = set()
    for m in months:
        cur = present[m]
        logo_rows.append({
            "month_start": m, "active_customers": len(cur),
            "new_logos": len(cur - prev), "churned_logos": len(prev - cur),
        })
        prev = cur
    out["logo_movement"] = pd.DataFrame(logo_rows)

    # --- Retention (trailing 12 months, cohort based) ----------------------------------
    pivot = schedule.pivot_table(index="account_id", columns="month_start",
                                 values="recognized_amount_usd", aggfunc="sum",
                                 fill_value=0.0)
    ret_rows = []
    cols = list(pivot.columns)
    for i, m in enumerate(cols):
        m12 = m - pd.DateOffset(months=12)
        if m12 not in pivot.columns:
            continue
        then = pivot[m12]
        now = pivot[m]
        cohort = then > 0
        base = then[cohort].sum()
        if base <= 0:
            continue
        nrr = now[cohort].sum() / base
        grr = np.minimum(now[cohort], then[cohort]).sum() / base
        ret_rows.append({"month_start": m, "nrr": round(float(nrr), 4),
                         "grr": round(float(grr), 4), "cohort_base_mrr_usd": round(float(base), 2)})
    out["retention_ltm"] = pd.DataFrame(ret_rows)

    # --- Bookings (won opportunity ACV) ------------------------------------------------
    won = opps[opps["is_won"]].copy()
    won["month"] = pd.to_datetime(won["close_date"]).values.astype("datetime64[M]")
    bookings = (won.pivot_table(index="month", columns="opp_type", values="amount_usd",
                                aggfunc="sum", fill_value=0.0).reset_index())
    bookings["total_bookings_usd"] = bookings.drop(columns=["month"]).sum(axis=1)
    out["bookings"] = bookings

    # --- Win rate (New Business, quarterly) --------------------------------------------
    nb = opps[(opps["opp_type"] == "New Business") & (opps["is_closed"])].copy()
    nb["quarter"] = pd.PeriodIndex(pd.to_datetime(nb["close_date"]), freq="Q").astype(str)
    wr = (nb.groupby("quarter")
          .agg(won=("is_won", "sum"), closed=("is_won", "count")).reset_index())
    wr["win_rate"] = (wr["won"] / wr["closed"]).round(4)
    out["win_rate"] = wr

    # --- Usage (monthly active users / accounts) ---------------------------------------
    if usage is not None and len(usage):
        u = usage[usage["account_id"].isin(keep)].copy()
        u["month"] = pd.to_datetime(u["event_local_date"]).values.astype("datetime64[M]")
        um = (u.groupby("month").agg(mau=("end_user_id", "nunique"),
                                     active_accounts=("account_id", "nunique"),
                                     events=("event_id", "count")).reset_index())
        out["usage_monthly"] = um

    # --- Headline KPIs at as_of --------------------------------------------------------
    as_of_m = arr["month_start"].max()
    headline = {
        "as_of_month": str(pd.Timestamp(as_of_m).date()),
        "arr_usd": float(arr.loc[arr["month_start"] == as_of_m, "arr_usd"].iloc[0]),
        "active_customers": int(out["logo_movement"]
                                .loc[out["logo_movement"]["month_start"] == as_of_m,
                                     "active_customers"].iloc[0]),
    }
    if len(out["retention_ltm"]):
        last_ret = out["retention_ltm"].iloc[-1]
        headline["nrr_ltm"] = float(last_ret["nrr"])
        headline["grr_ltm"] = float(last_ret["grr"])
    # trailing-12-month bookings
    b = out["bookings"].copy()
    b["month"] = pd.to_datetime(b["month"])
    ltm = b[b["month"] > (pd.Timestamp(as_of_m) - pd.DateOffset(months=12))]
    headline["ltm_bookings_usd"] = float(ltm["total_bookings_usd"].sum())
    if "usage_monthly" in out and len(out["usage_monthly"]):
        headline["mau_as_of"] = int(out["usage_monthly"].iloc[-1]["mau"])
    return out, headline
