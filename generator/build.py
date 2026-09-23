"""Orchestrator: build one fixture end-to-end and (optionally) persist it."""

from __future__ import annotations

import time
from pathlib import Path

from . import config as C
from .util import Ctx
from . import entities, revenue, pipeline, activity, governance
from .quirks import inject_quirks
from .reference_metrics import compute_reference_metrics
from .writers import write_fixture


def build_fixture(fixture_name: str, seed: int, scale: str = "medium",
                  overrides: dict | None = None, verbose: bool = True):
    cfg = C.scale_config(scale, seed=seed, fixture_name=fixture_name)
    for k, v in (overrides or {}).items():
        setattr(cfg, k, v)
    ctx = Ctx.create(seed)

    def log(msg):
        if verbose:
            print(f"  [{fixture_name}] {msg}")

    t0 = time.time()
    calendar = entities.gen_calendar(cfg)
    fx_df, fx = entities.gen_fx_rates(cfg, ctx)
    plans = entities.gen_plans()
    users = entities.gen_users(cfg, ctx)
    accounts = entities.gen_accounts(cfg, ctx)
    ownership = entities.gen_account_ownership(cfg, ctx, accounts, users)
    log(f"entities: {len(accounts)} accounts, {len(users)} users, "
        f"{len(ownership)} ownership rows")

    subs, sub_events, schedule, contracts = revenue.gen_subscriptions(cfg, ctx, accounts, fx)
    invoices, line_items, payments, refunds = revenue.gen_billing(
        cfg, ctx, accounts, subs, schedule, fx)
    log(f"revenue: {len(subs)} subs, {len(sub_events)} events, {len(invoices)} invoices")

    opps, stage_hist, snapshots = pipeline.gen_pipeline(
        cfg, ctx, accounts, subs, sub_events, ownership, users, fx)
    log(f"pipeline: {len(opps)} opps, {len(snapshots)} snapshots")

    end_users = activity.gen_end_users(cfg, ctx, accounts, subs)
    usage_events, daily_usage = activity.gen_usage(
        cfg, ctx, accounts, subs, end_users, schedule)
    tickets = activity.gen_support_tickets(cfg, ctx, accounts, subs, users)
    log(f"activity: {len(end_users)} end-users, {len(usage_events)} usage events, "
        f"{len(tickets)} tickets")

    access_map, personas = governance.gen_governance(cfg, ctx, accounts, users, ownership)
    log(f"governance: {len(access_map)} grants, {len(personas)} personas")

    tables = {
        "calendar": calendar,
        "fx_rates": fx_df,
        "plans": plans,
        "users": users,
        "accounts": accounts,
        "account_ownership": ownership,
        "subscriptions": subs,
        "subscription_events": sub_events,
        "revenue_schedule": schedule,
        "contracts": contracts,
        "invoices": invoices,
        "invoice_line_items": line_items,
        "payments": payments,
        "refunds": refunds,
        "opportunities": opps,
        "opportunity_stage_history": stage_hist,
        "opportunity_snapshots": snapshots,
        "end_users": end_users,
        "usage_events": usage_events,
        "daily_usage": daily_usage,
        "support_tickets": tickets,
        "access_map": access_map,
        "personas": personas,
    }

    quirks_manifest = inject_quirks(cfg, ctx, tables)
    reference_metrics, headline = compute_reference_metrics(tables)
    log(f"done in {time.time() - t0:.1f}s | headline: "
        f"ARR=${headline.get('arr_usd', 0):,.0f}, "
        f"customers={headline.get('active_customers', 0)}, "
        f"NRR={headline.get('nrr_ltm', float('nan')):.1%}")
    return cfg, tables, reference_metrics, headline, quirks_manifest


def build_and_write(fixture_name: str, seed: int, out_dir: str | Path,
                    scale: str = "medium", overrides: dict | None = None,
                    verbose: bool = True):
    cfg, tables, ref, headline, quirks = build_fixture(
        fixture_name, seed, scale, overrides, verbose)
    manifest = write_fixture(cfg, tables, ref, headline, quirks, out_dir)
    if verbose:
        print(f"  [{fixture_name}] wrote {manifest['total_rows']:,} rows across "
              f"{len(manifest['row_counts'])} tables -> {Path(out_dir) / fixture_name}")
    return manifest
