"""Revenue engine: subscription lifecycle, recognition schedule, contracts, billing.

The monthly lifecycle simulation is the backbone of the dataset: it emits a coherent
stream of MRR movements (new / expansion / contraction / churn / reactivation / renewal)
from which point-in-time ARR and all retention metrics can be reconstructed exactly.

Three revenue concepts are kept deliberately distinct (a core metric-definition trap):
  * ARR / MRR         -> canonical USD recurring run-rate (from the event stream)
  * Bookings          -> won opportunity ACV (see pipeline module) + contract TCV
  * Recognized revenue-> straight-line monthly recognition (revenue_schedule)
Billing (invoices) is issued in each account's local currency, so amount_local must be
FX-converted to amount_usd to be summable across accounts.
"""

from __future__ import annotations

import pandas as pd

from . import config as C
from .entities import from_usd, to_usd
from .util import Ctx, add_months, chance, clamp, hash_id, make_ids, money, pick, randint, uniform


def _initial_plan_and_seats(cfg: C.Config, ctx: Ctx, segment: str, employees: int):
    seg = C.SEGMENTS[segment]
    plan_name = pick(ctx, seg["plans"], [0.65, 0.35] if len(seg["plans"]) == 2 else None)
    lo, hi = seg["seat_range"]
    # draw seats in-range with a mild tilt toward larger organizations
    tilt = clamp(0.6 + employees / 40000, 0.6, 1.4)
    seats = int(clamp(round(ctx.rng.uniform(lo, hi) * tilt), lo, hi))
    discount = uniform(ctx, *seg["discount_range"])
    return plan_name, seats, discount


def _simulate_subscription(cfg: C.Config, ctx: Ctx, acc: pd.Series, *, product_line: str,
                           is_primary: bool, sub_id: str, start: pd.Timestamp,
                           health: float):
    """Run the monthly state machine for one subscription. Returns (sub_row, events,
    schedule_rows, contract_rows)."""
    seg = C.SEGMENTS[acc["segment"]]
    plan_name, seats, discount = _initial_plan_and_seats(cfg, ctx, acc["segment"],
                                                         acc["employee_count"])
    if not is_primary:  # add-on line is smaller
        seats = max(3, int(seats * uniform(ctx, 0.15, 0.4)))
        plan_name = "Pro" if acc["segment"] == "SMB" else "Business"

    price = C.PLAN_NAME_TO_PRICE[plan_name]
    mrr = seats * price * (1 - discount)
    currency = acc["currency"]
    annual = chance(ctx, seg["annual_billing_p"])
    billing = "annual" if annual else "monthly"

    events, schedule, contracts = [], [], []
    win_start = cfg.start_date
    win_end = cfg.end_date

    def emit(ev_type, date, old_mrr, new_mrr, old_seats, new_seats, old_plan, new_plan, reason):
        events.append({
            "event_id": hash_id("SEV", sub_id, ev_type, date.isoformat(), round(new_mrr, 2)),
            "subscription_id": sub_id,
            "account_id": acc["account_id"],
            "event_type": ev_type,
            "event_date": date.date().isoformat(),
            "old_mrr_usd": money(old_mrr),
            "new_mrr_usd": money(new_mrr),
            "mrr_delta_usd": money(new_mrr - old_mrr),
            "old_seats": int(old_seats),
            "new_seats": int(new_seats),
            "old_plan": old_plan,
            "new_plan": new_plan,
            "reason": reason,
        })

    # New event at creation (may predate the window for installed base).
    emit("new", start, 0.0, mrr, 0, seats, None, plan_name, "new_business")
    status = "active"
    end_date = None
    contract_anchor = start

    months = pd.date_range(start.replace(day=1), win_end.replace(day=1), freq="MS")
    churned_until = None  # reactivation gap tracker
    for i, m in enumerate(months):
        # record recognition for active months inside the window
        if status == "active" and m >= win_start.replace(day=1):
            schedule.append({
                "subscription_id": sub_id, "account_id": acc["account_id"],
                "month_start": m, "currency": currency,
                "recognized_amount_usd": money(mrr),
                "recognized_amount_local": from_usd(mrr, currency, m,
                                                    _FX_HOLDER["fx"]),
                "plan_name": plan_name, "seats": int(seats),
                "billing_frequency": billing,
            })
        # contract creation at start and each 12-month renewal while active
        months_since = (m.year - contract_anchor.year) * 12 + (m.month - contract_anchor.month)
        if status == "active" and months_since >= 0 and months_since % 12 == 0:
            term = 12 if acc["segment"] != "Enterprise" else pick(ctx, [12, 24, 36], [0.5, 0.3, 0.2])
            contracts.append({
                "contract_id": hash_id("CON", sub_id, m.isoformat()),
                "account_id": acc["account_id"], "subscription_id": sub_id,
                "start_date": m.date().isoformat(),
                "end_date": add_months(m, term).date().isoformat(),
                "term_months": term,
                "committed_arr_usd": money(mrr * 12),
                "tcv_usd": money(mrr * 12 * term / 12),
                "currency": currency, "status": "active",
                "is_renewal": months_since > 0,
            })
            if months_since > 0:
                emit("renewal", m, mrr, mrr, seats, seats, plan_name, plan_name, "renewal")

        if i == 0:
            continue
        if status == "churned":
            if churned_until is not None and m >= churned_until:
                # reactivation
                new_mrr = mrr * uniform(ctx, 0.8, 1.15)
                emit("reactivation", m, 0.0, new_mrr, 0, seats, None, plan_name, "winback")
                mrr = new_mrr
                status = "active"
                end_date = None
                contract_anchor = m
                churned_until = None
            continue

        # probabilities (health raises/lowers rates; light seasonality)
        churn_p = seg["base_monthly_churn"] * (1.7 - health) * (1.25 if m.month == 1 else 1.0)
        expand_p = seg["expand_p"] * (0.5 + health) * (1.3 if m.month in (11, 12) else 1.0)
        contract_p = seg["contract_p"] * (1.6 - health)
        u = float(ctx.rng.random())
        if not is_primary:
            expand_p *= 0.4  # add-ons are steadier
            contract_p *= 0.5

        if u < churn_p:
            emit("churn", m, mrr, 0.0, seats, seats, plan_name, plan_name, "churn")
            status = "churned"
            end_date = m
            if chance(ctx, 0.12):
                churned_until = add_months(m, randint(ctx, 2, 6))
        elif u < churn_p + expand_p:
            old_mrr, old_seats, old_plan = mrr, seats, plan_name
            if chance(ctx, 0.35) and plan_name != "Enterprise":
                # tier upgrade
                tiers = [p["plan_name"] for p in sorted(C.PLANS, key=lambda x: x["tier"])]
                plan_name = tiers[min(tiers.index(plan_name) + 1, len(tiers) - 1)]
                price = C.PLAN_NAME_TO_PRICE[plan_name]
            else:
                seats = int(seats * uniform(ctx, 1.10, 1.5))
            mrr = seats * price * (1 - discount)
            emit("expansion", m, old_mrr, mrr, old_seats, seats, old_plan, plan_name,
                 "seat_expansion_or_upgrade")
        elif u < churn_p + expand_p + contract_p:
            old_mrr, old_seats = mrr, seats
            seats = max(2, int(seats * uniform(ctx, 0.6, 0.9)))
            mrr = seats * price * (1 - discount)
            emit("contraction", m, old_mrr, mrr, old_seats, seats, plan_name, plan_name,
                 "seat_reduction")

    current_mrr = mrr if status == "active" else 0.0
    sub_row = {
        "subscription_id": sub_id,
        "account_id": acc["account_id"],
        "product_line": product_line,
        "plan_id": C.PLAN_NAME_TO_ID[plan_name],
        "plan_name": plan_name,
        "status": status,
        "start_date": start.date().isoformat(),
        "end_date": end_date.date().isoformat() if end_date is not None else None,
        "billing_frequency": billing,
        "seats": int(seats),
        "currency": currency,
        "mrr_usd": money(current_mrr),
        "mrr_local": from_usd(current_mrr, currency, cfg.as_of_date, _FX_HOLDER["fx"]),
        "arr_usd": money(current_mrr * 12),
        "health_score": round(float(health), 3),
        "is_primary": is_primary,
    }
    return sub_row, events, schedule, contracts


# The FX lookup is process-global for the current fixture build (set by build.py) so the
# deep lifecycle loop doesn't have to thread it through every call signature.
_FX_HOLDER: dict = {"fx": {}}


def gen_subscriptions(cfg: C.Config, ctx: Ctx, accounts: pd.DataFrame, fx: dict):
    _FX_HOLDER["fx"] = fx
    subs, events, schedule, contracts = [], [], [], []
    sub_ids = iter(make_ids("SUB", cfg.n_accounts * 3, width=6))

    for _, acc in accounts.iterrows():
        health = float(ctx.rng.beta(2.2, 2.0))
        start = max(pd.Timestamp(acc["created_date"]), add_months(cfg.start_date, -24))
        s, e, sch, con = _simulate_subscription(
            cfg, ctx, acc, product_line="Core", is_primary=True,
            sub_id=next(sub_ids), start=start, health=health)
        subs.append(s); events += e; schedule += sch; contracts += con

        # ~12% of Mid-Market/Enterprise accounts also carry an add-on product line
        if acc["segment"] in ("Mid-Market", "Enterprise") and chance(ctx, 0.18):
            add_start = add_months(start, randint(ctx, 2, 14))
            if add_start < cfg.end_date:
                s2, e2, sch2, con2 = _simulate_subscription(
                    cfg, ctx, acc, product_line="Add-on: Advanced Analytics",
                    is_primary=False, sub_id=next(sub_ids), start=add_start,
                    health=min(1.0, health + 0.1))
                subs.append(s2); events += e2; schedule += sch2; contracts += con2

    subs_df = pd.DataFrame(subs)
    events_df = pd.DataFrame(events).sort_values(["account_id", "event_date"]).reset_index(drop=True)
    schedule_df = pd.DataFrame(schedule)
    contracts_df = pd.DataFrame(contracts)
    return subs_df, events_df, schedule_df, contracts_df


# --------------------------------------------------------------------------------------
# Billing: invoices, line items, payments, refunds
# --------------------------------------------------------------------------------------

def gen_billing(cfg: C.Config, ctx: Ctx, accounts: pd.DataFrame, subs: pd.DataFrame,
                schedule: pd.DataFrame, fx: dict):
    acc_by_id = accounts.set_index("account_id")
    sub_billing = subs.set_index("subscription_id")["billing_frequency"].to_dict()
    sub_start = {r["subscription_id"]: pd.Timestamp(r["start_date"]) for _, r in subs.iterrows()}

    invoices, line_items, payments = [], [], []
    inv_seq = iter(make_ids("INV", len(schedule) + cfg.n_accounts * 4 + 10, width=7))

    def add_invoice(account_id, subscription_id, date, amount_usd, inv_type, currency):
        amount_local = from_usd(amount_usd, currency, date, fx)
        amount_usd_reported = to_usd(amount_local, currency, date, fx)
        # status: recent invoices may still be pending; a few are voided
        age_days = (cfg.as_of_date - date).days
        if age_days < 25 and chance(ctx, 0.5):
            status = "pending"
        elif chance(ctx, 0.01):
            status = "void"
        else:
            status = "paid"
        inv_id = next(inv_seq)
        invoices.append({
            "invoice_id": inv_id, "account_id": account_id,
            "subscription_id": subscription_id,
            "invoice_date": date.date().isoformat(),
            "due_date": add_months(date, 1).date().isoformat(),
            "currency": currency,
            "amount_local": money(amount_local),
            "amount_usd": money(amount_usd_reported),
            "status": status, "invoice_type": inv_type,
        })
        # line items (fan-out): base + optional add-on + optional discount
        _add_line_items(inv_id, account_id, currency, amount_local, amount_usd_reported, date)
        if status == "paid":
            pay_date = date + pd.Timedelta(days=int(ctx.rng.integers(0, 46)))
            payments.append({
                "payment_id": hash_id("PAY", inv_id),
                "invoice_id": inv_id, "account_id": account_id,
                "payment_date": pay_date.date().isoformat(),
                "amount_local": money(amount_local),
                "amount_usd": money(amount_usd_reported),
                "method": pick(ctx, ["ACH", "Wire", "Credit Card", "Check"],
                               [0.4, 0.25, 0.3, 0.05]),
                "status": "settled",
            })
        return inv_id, status

    def _add_line_items(inv_id, account_id, currency, amount_local, amount_usd, date):
        parts = [("Subscription", 0.82)]
        if chance(ctx, 0.35):
            parts.append(("Add-on", 0.18))
        # normalize weights
        tot = sum(w for _, w in parts)
        running_local = running_usd = 0.0
        li_seq = 0
        for label, w in parts:
            share = w / tot
            li_local = money(amount_local * share)
            li_usd = money(amount_usd * share)
            running_local += li_local
            running_usd += li_usd
            li_seq += 1
            line_items.append({
                "line_item_id": hash_id("LIN", inv_id, li_seq),
                "invoice_id": inv_id, "account_id": account_id,
                "item_type": label, "description": f"{label} charge",
                "quantity": 1, "currency": currency,
                "amount_local": li_local, "amount_usd": li_usd,
            })
        # reconcile rounding onto the first line
        if line_items:
            line_items[-len(parts)]["amount_local"] = money(
                line_items[-len(parts)]["amount_local"] + (amount_local - running_local))
            line_items[-len(parts)]["amount_usd"] = money(
                line_items[-len(parts)]["amount_usd"] + (amount_usd - running_usd))

    # recurring invoices from the recognition schedule
    for sub_id, grp in schedule.groupby("subscription_id"):
        billing = sub_billing.get(sub_id, "monthly")
        start = sub_start.get(sub_id)
        grp = grp.sort_values("month_start")
        for _, row in grp.iterrows():
            m = pd.Timestamp(row["month_start"])
            currency = row["currency"]
            if billing == "monthly":
                add_invoice(row["account_id"], sub_id, m, row["recognized_amount_usd"],
                            "subscription", currency)
            else:  # annual: bill 12x at each anniversary month
                months_since = (m.year - start.year) * 12 + (m.month - start.month)
                if months_since % 12 == 0:
                    add_invoice(row["account_id"], sub_id, m,
                                money(row["recognized_amount_usd"] * 12),
                                "subscription", currency)

    # one-time services invoices near account start
    for _, acc in accounts.iterrows():
        if chance(ctx, 0.4):
            created = pd.Timestamp(acc["created_date"])
            d = max(created, cfg.start_date) + pd.Timedelta(days=int(ctx.rng.integers(0, 40)))
            if d <= cfg.end_date:
                amt = uniform(ctx, 1500, 30000)
                add_invoice(acc["account_id"], None, d, money(amt), "one_time",
                            acc["currency"])

    invoices_df = pd.DataFrame(invoices)
    line_items_df = pd.DataFrame(line_items)
    payments_df = pd.DataFrame(payments)

    # refunds / credits on a fraction of paid invoices -> net revenue != gross
    refunds = []
    paid = invoices_df[invoices_df["status"] == "paid"]
    for _, inv in paid.iterrows():
        if chance(ctx, cfg.refund_invoice_fraction):
            frac = uniform(ctx, 0.2, 1.0)
            rdate = pd.Timestamp(inv["invoice_date"]) + pd.Timedelta(
                days=int(ctx.rng.integers(5, 90)))
            if rdate > cfg.end_date:
                rdate = cfg.end_date
            refunds.append({
                "refund_id": hash_id("REF", inv["invoice_id"]),
                "invoice_id": inv["invoice_id"], "account_id": inv["account_id"],
                "refund_date": rdate.date().isoformat(),
                "currency": inv["currency"],
                "amount_local": money(inv["amount_local"] * frac),
                "amount_usd": money(inv["amount_usd"] * frac),
                "reason": pick(ctx, C.REFUND_REASONS),
            })
    refunds_df = pd.DataFrame(refunds)
    return invoices_df, line_items_df, payments_df, refunds_df
