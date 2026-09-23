"""Pipeline / CRM: opportunities, stage history, and weekly point-in-time snapshots.

Bookings are defined here as won opportunity ACV. Won New Business / Expansion / Renewal
opportunities are tied back to the corresponding subscription events so bookings reconcile
with the revenue engine. Lost and open opportunities are added so win-rate, pipeline
creation, conversion, and forecast-category metrics are all exercisable. Opportunity
owner is resolved via *as-of* account ownership, so attribution after a rep transfer is a
genuine (well-defined) trap.
"""

from __future__ import annotations

import pandas as pd

from . import config as C
from .entities import from_usd
from .util import Ctx, add_months, chance, hash_id, make_ids, money, pick, uniform, randint


def _ownership_index(ownership: pd.DataFrame) -> dict:
    idx: dict[str, list] = {}
    for _, r in ownership.iterrows():
        vf = pd.Timestamp(r["valid_from"])
        vt = pd.Timestamp(r["valid_to"]) if r["valid_to"] else pd.Timestamp("2999-12-31")
        idx.setdefault(r["account_id"], []).append((vf, vt, r["owner_user_id"]))
    for k in idx:
        idx[k].sort()
    return idx


def owner_as_of(idx: dict, account_id: str, when: pd.Timestamp):
    for vf, vt, owner in idx.get(account_id, []):
        if vf <= when < vt:
            return owner
    segs = idx.get(account_id)
    return segs[-1][2] if segs else None


def _stage_path(is_won: bool, is_open: bool, ctx: Ctx, open_stage: str | None = None):
    order = ["Prospecting", "Qualification", "Proposal", "Negotiation"]
    if is_won:
        return order + ["Closed Won"]
    if is_open:
        upto = order.index(open_stage) + 1
        return order[:upto]
    # lost somewhere along the way
    depth = randint(ctx, 1, 4)
    return order[:depth] + ["Closed Lost"]


def _build_stage_history(ctx: Ctx, opp_id: str, path: list[str], created: pd.Timestamp,
                         close: pd.Timestamp, is_open: bool):
    rows = []
    n = len(path)
    # monotonically increasing entered dates between created and close
    span = max((close - created).days, n)
    cuts = sorted(int(ctx.rng.integers(0, span + 1)) for _ in range(n - 1))
    entered = [created] + [created + pd.Timedelta(days=c) for c in cuts]
    for i, stage in enumerate(path):
        ent = entered[i]
        if i < n - 1:
            ext = entered[i + 1]
        else:
            ext = None if is_open else close
        rows.append({
            "stage_history_id": hash_id("STG", opp_id, i),
            "opportunity_id": opp_id, "stage": stage,
            "entered_date": ent.date().isoformat(),
            "exited_date": ext.date().isoformat() if ext is not None else None,
            "days_in_stage": (None if ext is None else max((ext - ent).days, 0)),
        })
    return rows


def gen_pipeline(cfg: C.Config, ctx: Ctx, accounts: pd.DataFrame, subs: pd.DataFrame,
                 events: pd.DataFrame, ownership: pd.DataFrame, users: pd.DataFrame,
                 fx: dict):
    idx = _ownership_index(ownership)
    acc_cur = accounts.set_index("account_id")["currency"].to_dict()
    sub_by_acc = subs.groupby("account_id")["subscription_id"].first().to_dict()
    win = cfg.start_date

    opps, stage_hist, snapshots = [], [], []
    oid = iter(make_ids("OPP", cfg.n_accounts * 12, width=6))

    def new_opp(account_id, opp_type, amount_usd, created, close, *, is_won, is_open,
                open_stage=None, subscription_id=None):
        opp_id = next(oid)
        currency = acc_cur.get(account_id, "USD")
        owner = owner_as_of(idx, account_id, close if not is_open else cfg.as_of_date)
        path = _stage_path(is_won, is_open, ctx, open_stage)
        final_stage = path[-1]
        lead_source = (None if chance(ctx, cfg.null_lead_source_fraction)
                       else pick(ctx, C.LEAD_SOURCES))
        opps.append({
            "opportunity_id": opp_id, "account_id": account_id,
            "owner_user_id": owner, "opp_type": opp_type,
            "stage": final_stage,
            "amount_usd": money(amount_usd),
            "amount_local": from_usd(amount_usd, currency, created, fx),
            "currency": currency,
            "created_date": created.date().isoformat(),
            "close_date": close.date().isoformat(),
            "is_closed": not is_open,
            "is_won": bool(is_won),
            "forecast_category": C.STAGE_FORECAST_CATEGORY[final_stage],
            "probability": C.STAGE_PROBABILITY[final_stage],
            "lead_source": lead_source,
            "competitor": pick(ctx, C.COMPETITORS),
            "subscription_id": subscription_id,
        })
        stage_hist.extend(_build_stage_history(ctx, opp_id, path, created, close, is_open))
        # weekly snapshots while open
        snap_end = min(close, cfg.as_of_date) if not is_open else cfg.as_of_date
        _snapshot(opp_id, created, snap_end, path, stage_hist, amount_usd, close, is_open)

    def _snapshot(opp_id, created, snap_end, path, hist, amount_usd, close, is_open):
        # intervals for this opp only
        my = [h for h in hist if h["opportunity_id"] == opp_id]
        fridays = pd.date_range(created, snap_end, freq="W-FRI")
        for d in fridays:
            stage = path[0]
            for h in my:
                ent = pd.Timestamp(h["entered_date"])
                ext = pd.Timestamp(h["exited_date"]) if h["exited_date"] else pd.Timestamp("2999-12-31")
                if ent <= d < ext:
                    stage = h["stage"]
                    break
            snapshots.append({
                "snapshot_id": hash_id("SNP", opp_id, d.date().isoformat()),
                "opportunity_id": opp_id, "snapshot_date": d.date().isoformat(),
                "stage": stage, "amount_usd": money(amount_usd),
                "close_date": close.date().isoformat(),
                "forecast_category": C.STAGE_FORECAST_CATEGORY[stage],
                "is_open": stage in C.OPEN_STAGES,
            })

    # 1) Won opps tied to subscription events -------------------------------------------
    for _, ev in events.iterrows():
        edate = pd.Timestamp(ev["event_date"])
        if edate < win or edate > cfg.end_date:
            continue
        acc = ev["account_id"]
        sub = ev["subscription_id"]
        if ev["event_type"] == "new":
            amt = ev["new_mrr_usd"] * 12
            created = max(edate - pd.Timedelta(days=randint(ctx, 20, 120)), win)
            new_opp(acc, "New Business", amt, created, edate, is_won=True, is_open=False,
                    subscription_id=sub)
        elif ev["event_type"] == "expansion" and chance(ctx, 0.6):
            amt = ev["mrr_delta_usd"] * 12
            created = max(edate - pd.Timedelta(days=randint(ctx, 15, 75)), win)
            new_opp(acc, "Expansion", amt, created, edate, is_won=True, is_open=False,
                    subscription_id=sub)
        elif ev["event_type"] == "renewal" and chance(ctx, 0.85):
            amt = ev["new_mrr_usd"] * 12
            created = max(edate - pd.Timedelta(days=randint(ctx, 30, 90)), win)
            new_opp(acc, "Renewal", amt, created, edate, is_won=True, is_open=False,
                    subscription_id=sub)

    # 2) Lost New Business opps to reach the target win rate ----------------------------
    won_nb = sum(1 for o in opps if o["opp_type"] == "New Business" and o["is_won"])
    target_lost = int(won_nb * (1 / cfg.win_rate_target - 1))
    acc_ids = accounts["account_id"].tolist()
    for _ in range(target_lost):
        acc = acc_ids[int(ctx.rng.integers(0, len(acc_ids)))]
        created = win + pd.Timedelta(days=int(ctx.rng.integers(
            0, max((cfg.as_of_date - win).days - 30, 1))))
        close = created + pd.Timedelta(days=randint(ctx, 20, 160))
        if close > cfg.as_of_date:
            close = cfg.as_of_date
        amt = uniform(ctx, 5_000, 250_000)
        new_opp(acc, "New Business", amt, created, close, is_won=False, is_open=False)

    # 3) Open pipeline as of the snapshot date ------------------------------------------
    n_open = int(won_nb * cfg.open_pipeline_multiplier * 0.5) + 10
    for _ in range(n_open):
        acc = acc_ids[int(ctx.rng.integers(0, len(acc_ids)))]
        created = cfg.as_of_date - pd.Timedelta(days=randint(ctx, 5, 180))
        close = cfg.as_of_date + pd.Timedelta(days=randint(ctx, 10, 150))
        stage = pick(ctx, C.OPEN_STAGES, [0.35, 0.30, 0.22, 0.13])
        opp_type = pick(ctx, C.OPP_TYPES, [0.55, 0.30, 0.15])
        amt = uniform(ctx, 8_000, 400_000)
        new_opp(acc, opp_type, amt, created, close, is_won=False, is_open=True,
                open_stage=stage)

    opps_df = pd.DataFrame(opps)
    stage_hist_df = pd.DataFrame(stage_hist)
    snapshots_df = pd.DataFrame(snapshots)
    return opps_df, stage_hist_df, snapshots_df
