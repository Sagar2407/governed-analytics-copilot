"""Product activity + support: end users, usage events, daily rollup, support tickets.

Usage volume is controlled by a two-pass budget/scale step so the raw event table honors
`cfg.max_usage_events` regardless of scale. Timestamps use fixed standard UTC offsets per
timezone (DST intentionally ignored for reproducibility) and are stored in BOTH local and
UTC form, so "group by local date" (daily_usage) vs "group by UTC date" (naive raw query)
diverge near midnight -- the documented timezone-boundary trap. Adoption is correlated with
subscription health, so churn and low usage move together.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import config as C
from .util import Ctx, chance, clamp, hash_id, make_ids, pick

# Fixed standard offsets (hours east of UTC). Kolkata is deliberately half-hour.
TZ_OFFSET_HOURS: dict[str, float] = {
    "America/Los_Angeles": -8, "America/New_York": -5, "America/Chicago": -6,
    "America/Toronto": -5, "Europe/London": 0, "Europe/Berlin": 1, "Europe/Paris": 1,
    "Europe/Amsterdam": 1, "Europe/Madrid": 1, "Australia/Sydney": 10,
    "Asia/Singapore": 8, "Asia/Tokyo": 9, "Asia/Kolkata": 5.5,
    "America/Sao_Paulo": -3, "America/Mexico_City": -6,
}

_EVENT_TYPES = list(C.USAGE_EVENT_TYPES.keys())
_EVENT_WEIGHTS = np.array([
    0.30,  # login
    0.20,  # dashboard_view
    0.05,  # dashboard_create
    0.14,  # report_run
    0.06,  # report_export
    0.05,  # report_share
    0.08,  # query_run
    0.03,  # alert_create
    0.04,  # integration_sync
    0.03,  # api_call
    0.02,  # invite_user
])
_EVENT_WEIGHTS = _EVENT_WEIGHTS / _EVENT_WEIGHTS.sum()


def gen_end_users(cfg: C.Config, ctx: Ctx, accounts: pd.DataFrame, subs: pd.DataFrame):
    seats_by_acc = subs.groupby("account_id")["seats"].sum().to_dict()
    tz_by_acc = accounts.set_index("account_id")["timezone"].to_dict()
    rows = []
    for acc_id, seats in seats_by_acc.items():
        n = int(clamp(round(seats * 0.3), 1, 40))
        for j in range(n):
            name = ctx.fake.name()
            handle = name.lower().replace(" ", ".").replace("'", "").replace(",", "")
            rows.append({
                "end_user_id": hash_id("EU", acc_id, j),
                "account_id": acc_id,
                "full_name": name,
                "email": f"{handle}.{j}@{cfg.customer_email_domain}",
                "role": pick(ctx, ["Admin", "Analyst", "Viewer", "Editor"],
                             [0.1, 0.35, 0.4, 0.15]),
                "timezone": tz_by_acc.get(acc_id, "America/New_York"),
                "is_active": chance(ctx, 0.9),
            })
    return pd.DataFrame(rows)


def gen_usage(cfg: C.Config, ctx: Ctx, accounts: pd.DataFrame, subs: pd.DataFrame,
              end_users: pd.DataFrame, schedule: pd.DataFrame):
    if schedule.empty or end_users.empty:
        return pd.DataFrame(), pd.DataFrame()

    tz_by_acc = accounts.set_index("account_id")["timezone"].to_dict()
    health_by_acc = subs.groupby("account_id")["health_score"].mean().to_dict()
    eu_by_acc = end_users.groupby("account_id")["end_user_id"].apply(list).to_dict()

    # active (account, month) pairs from the recognition schedule
    active = (schedule[["account_id", "month_start"]].drop_duplicates()
              .to_records(index=False))

    # Pass 1: budgets
    budgets = []
    total = 0.0
    for acc_id, month in active:
        eus = eu_by_acc.get(acc_id)
        if not eus:
            continue
        health = health_by_acc.get(acc_id, 0.5)
        adoption = float(clamp(0.25 + health + ctx.rng.normal(0, 0.15), 0.15, 1.6))
        active_users = int(clamp(round(len(eus) * clamp(adoption, 0.1, 1.0)), 1, len(eus)))
        budget = active_users * 22 * adoption
        budgets.append((acc_id, pd.Timestamp(month), eus, active_users, adoption))
        total += budget

    scale = min(1.0, cfg.max_usage_events / total) if total > 0 else 1.0

    # Pass 2: emit events
    recs = []
    for acc_id, month, eus, active_users, adoption in budgets:
        base_budget = active_users * 22 * adoption
        n_events = int(ctx.rng.poisson(base_budget * scale))
        if n_events <= 0:
            continue
        tz = tz_by_acc.get(acc_id, "America/New_York")
        offset = TZ_OFFSET_HOURS.get(tz, 0.0)
        days_in_month = month.days_in_month
        month_active = [eus[i] for i in ctx.rng.choice(len(eus), size=active_users,
                                                        replace=False)]

        day = ctx.rng.integers(1, days_in_month + 1, size=n_events)
        # business-hours-ish local times
        hour = np.clip(ctx.rng.normal(13, 3.2, size=n_events).round(), 5, 22).astype(int)
        minute = ctx.rng.integers(0, 60, size=n_events)
        # ~1.2% of events pinned near local quarter-end midnight -> UTC may cross the boundary
        et_idx = ctx.rng.choice(len(_EVENT_TYPES), size=n_events, p=_EVENT_WEIGHTS)
        user_idx = ctx.rng.integers(0, len(month_active), size=n_events)
        sess_idx = ctx.rng.integers(1, 4, size=n_events)

        is_quarter_end_month = month.month in (3, 6, 9, 12)
        for k in range(n_events):
            d = int(day[k])
            h, mi = int(hour[k]), int(minute[k])
            if is_quarter_end_month and chance(ctx, 0.012):
                d = days_in_month
                h, mi = 23, int(ctx.rng.integers(30, 60))
            local = month + pd.Timedelta(days=d - 1, hours=h, minutes=mi)
            utc = local - pd.Timedelta(hours=offset)
            eu = month_active[int(user_idx[k])]
            etype = _EVENT_TYPES[int(et_idx[k])]
            # a small fraction of events are back-dated in arrival (late-loaded)
            recs.append((
                acc_id, eu, etype, C.USAGE_EVENT_TYPES[etype],
                utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                local.strftime("%Y-%m-%dT%H:%M:%S"),
                local.date().isoformat(), utc.date().isoformat(), tz,
                hash_id("SES", eu, local.date().isoformat(), int(sess_idx[k])),
            ))

    cols = ["account_id", "end_user_id", "event_type", "feature",
            "event_timestamp_utc", "event_local_time", "event_local_date",
            "event_utc_date", "timezone", "session_id"]
    events = pd.DataFrame.from_records(recs, columns=cols)
    events.insert(0, "event_id", [hash_id("USE", i, r[4]) for i, r in enumerate(recs)])
    # loaded_at defaults to same-day; late arrivals patched later in quirks
    events["loaded_at"] = events["event_local_date"]

    # daily rollup by LOCAL date (documented grain)
    key_mask = events["event_type"].isin(C.KEY_ACTIONS)
    daily = (events.assign(is_key=key_mask.astype(int))
             .groupby(["account_id", "event_local_date"])
             .agg(active_users=("end_user_id", "nunique"),
                  sessions=("session_id", "nunique"),
                  events=("event_id", "count"),
                  key_actions=("is_key", "sum"))
             .reset_index()
             .rename(columns={"event_local_date": "date"}))
    return events, daily


def gen_support_tickets(cfg: C.Config, ctx: Ctx, accounts: pd.DataFrame, subs: pd.DataFrame,
                        users: pd.DataFrame):
    health_by_acc = subs.groupby("account_id")["health_score"].mean().to_dict()
    csms = users[users["role"] == "Customer Success Manager"]
    csm_by_region = {r: csms[csms["region"] == r]["user_id"].tolist()
                     for r in accounts["region"].unique()}
    all_csm = csms["user_id"].tolist()

    rows = []
    tid = iter(make_ids("TKT", cfg.n_accounts * 30, width=6))
    for _, acc in accounts.iterrows():
        acc_id = acc["account_id"]
        start = max(pd.Timestamp(acc["created_date"]), cfg.start_date)
        years = max((cfg.as_of_date - start).days / 365.25, 0.1)
        health = health_by_acc.get(acc_id, 0.5)
        lam = cfg.support_tickets_per_account_year * years * (1.6 - health)
        n = int(ctx.rng.poisson(lam))
        pool = csm_by_region.get(acc["region"]) or all_csm
        for _ in range(n):
            created = start + pd.Timedelta(days=int(ctx.rng.integers(
                0, max((cfg.as_of_date - start).days, 1))))
            priority = pick(ctx, C.TICKET_PRIORITIES, [0.4, 0.35, 0.18, 0.07])
            resolved = None
            csat = None
            first_response = round(float(ctx.rng.exponential(6)) + 0.5, 1)
            if chance(ctx, 0.9):  # most tickets resolved
                res_days = ctx.rng.exponential(4) + (3 if priority in ("Low", "Medium") else 1)
                resolved = created + pd.Timedelta(days=float(res_days))
                if resolved > cfg.as_of_date:
                    resolved = None
                else:
                    csat = int(clamp(round(ctx.rng.normal(3.5 + health, 1.0)), 1, 5))
            rows.append({
                "ticket_id": next(tid), "account_id": acc_id,
                "created_at": created.date().isoformat(),
                "resolved_at": resolved.date().isoformat() if resolved is not None else None,
                "status": "resolved" if resolved is not None else "open",
                "priority": priority,
                "category": pick(ctx, C.TICKET_CATEGORIES),
                "csat_score": csat,
                "first_response_hours": first_response,
                "assigned_user_id": pool[int(ctx.rng.integers(0, len(pool)))] if pool else None,
            })
    return pd.DataFrame(rows)
