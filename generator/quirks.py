"""Inject accidental data-quality issues and emit a documented quirks manifest.

Two categories:
  1. Issues injected here (exact-duplicate line items, late-arriving events).
  2. Issues baked into the data by design during generation (multi-currency, internal/
     test accounts, as-of ownership, timezone boundaries, NULLs, void/pending invoices,
     invoice->line_item fan-out).

Every trap is recorded with a `correct_handling` rule. This manifest is the single source
of truth the copilot's metric layer and the correctness eval must satisfy -- an answer is
"right" only if it handles these the documented way. Nothing here corrupts a metric's
ground truth: each trap has a well-defined correct treatment.
"""

from __future__ import annotations

import pandas as pd

from . import config as C
from .util import Ctx


def inject_quirks(cfg: C.Config, ctx: Ctx, tables: dict) -> list[dict]:
    manifest: list[dict] = []

    # 1. Accidental exact-duplicate invoice line items (same line_item_id) -------------
    li = tables.get("invoice_line_items")
    dup_ids = []
    if li is not None and len(li):
        n_dup = int(round(len(li) * cfg.duplicate_line_item_fraction))
        if n_dup > 0:
            dup_idx = ctx.rng.choice(len(li), size=n_dup, replace=False)
            dups = li.iloc[dup_idx].copy()
            dup_ids = dups["line_item_id"].tolist()
            tables["invoice_line_items"] = pd.concat([li, dups], ignore_index=True)
    manifest.append({
        "quirk": "duplicate_line_items",
        "table": "invoice_line_items",
        "description": f"{len(dup_ids)} exact-duplicate rows (identical line_item_id) were "
                       "appended, simulating a double-loaded ETL batch.",
        "correct_handling": "Compute revenue from `invoices`, not `invoice_line_items`. If "
                            "line items must be used, de-duplicate by `line_item_id` first.",
        "magnitude": len(dup_ids),
    })

    # 2. Late-arriving usage events ----------------------------------------------------
    ue = tables.get("usage_events")
    n_late = 0
    if ue is not None and len(ue):
        n_late = int(round(len(ue) * cfg.late_arrival_fraction))
        if n_late > 0:
            late_idx = ctx.rng.choice(len(ue), size=n_late, replace=False)
            delays = ctx.rng.integers(3, 31, size=n_late)
            loaded = (pd.to_datetime(ue.iloc[late_idx]["event_local_date"])
                      + pd.to_timedelta(delays, unit="D"))
            loaded = loaded.clip(upper=cfg.as_of_date)
            ue.loc[ue.index[late_idx], "loaded_at"] = loaded.dt.date.astype(str).values
    manifest.append({
        "quirk": "late_arriving_events",
        "table": "usage_events",
        "description": f"{n_late} events have `loaded_at` days-to-weeks after "
                       "`event_local_date` (late-arriving telemetry).",
        "correct_handling": "For recent-period activity metrics, note completeness is not "
                            "guaranteed until data has settled; use `loaded_at` to reason "
                            "about freshness. Historical periods are complete.",
        "magnitude": n_late,
    })

    # 3. Documented by-design traps ----------------------------------------------------
    acc = tables.get("accounts")
    n_internal = int(acc["is_internal"].sum()) if acc is not None else 0
    manifest.append({
        "quirk": "internal_test_accounts",
        "table": "accounts",
        "description": f"{n_internal} accounts are flagged `is_internal = true` "
                       "(employee/test tenants).",
        "correct_handling": "Exclude `is_internal = true` from all customer, revenue, and "
                            "retention metrics unless explicitly asked to include them.",
        "magnitude": n_internal,
    })

    inv = tables.get("invoices")
    currencies = sorted(inv["currency"].unique().tolist()) if inv is not None else []
    manifest.append({
        "quirk": "multi_currency",
        "table": "invoices / invoice_line_items / opportunities / subscriptions",
        "description": f"Amounts are stored in local currency ({', '.join(currencies)}) with "
                       "a parallel `amount_usd` FX-converted at the transaction date.",
        "correct_handling": "Never SUM `amount_local` across accounts of different "
                            "currencies. Use `amount_usd` for cross-account aggregation.",
        "magnitude": len(currencies),
    })

    if inv is not None:
        n_void = int((inv["status"] == "void").sum())
        n_pending = int((inv["status"] == "pending").sum())
    else:
        n_void = n_pending = 0
    manifest.append({
        "quirk": "void_and_pending_invoices",
        "table": "invoices",
        "description": f"{n_void} void and {n_pending} pending invoices exist.",
        "correct_handling": "Exclude `status = 'void'` from billings/revenue. `pending` is "
                            "billed-but-unpaid: include in billings, exclude from collected "
                            "cash / payments.",
        "magnitude": n_void + n_pending,
    })

    ref = tables.get("refunds")
    n_ref = len(ref) if ref is not None else 0
    manifest.append({
        "quirk": "refunds_credits",
        "table": "refunds",
        "description": f"{n_ref} invoices received partial/full credits.",
        "correct_handling": "Gross revenue excludes refunds; NET revenue = gross - refunds. "
                            "State which one is being reported.",
        "magnitude": n_ref,
    })

    own = tables.get("account_ownership")
    n_transfers = 0
    if own is not None:
        n_transfers = int(len(own) - own["account_id"].nunique())
    manifest.append({
        "quirk": "as_of_ownership",
        "table": "account_ownership",
        "description": f"{n_transfers} ownership transfers (SCD2). An account's owner "
                       "changes over time.",
        "correct_handling": "Attribute an opportunity/booking to the owner AS OF the "
                            "relevant date via valid_from/valid_to; use is_current only for "
                            "'today' questions.",
        "magnitude": n_transfers,
    })

    manifest.append({
        "quirk": "timezone_boundaries",
        "table": "usage_events",
        "description": "Events carry both local and UTC timestamps; some sit near local "
                       "midnight at quarter-end, so local vs UTC date grouping differ.",
        "correct_handling": "`daily_usage` is rolled up by LOCAL date. Group raw events by "
                            "`event_local_date` for activity metrics unless UTC is requested.",
        "magnitude": None,
    })

    opp = tables.get("opportunities")
    n_null_ls = int(opp["lead_source"].isna().sum()) if opp is not None else 0
    n_null_ind = int(acc["industry"].isna().sum()) if acc is not None else 0
    manifest.append({
        "quirk": "missing_values",
        "table": "accounts.industry, opportunities.lead_source",
        "description": f"{n_null_ind} accounts have NULL industry; {n_null_ls} opportunities "
                       "have NULL lead_source.",
        "correct_handling": "Treat NULL as 'Unknown'; do not silently drop rows when "
                            "segmenting by these dimensions (report an Unknown bucket).",
        "magnitude": n_null_ind + n_null_ls,
    })

    manifest.append({
        "quirk": "invoice_line_item_fanout",
        "table": "invoices -> invoice_line_items",
        "description": "One invoice has many line items; joining invoices to line items "
                       "multiplies invoice-grain rows.",
        "correct_handling": "Aggregate line items to invoice grain before joining, or sum "
                            "amounts at the correct grain to avoid double counting.",
        "magnitude": None,
    })

    manifest.append({
        "quirk": "metric_definition_separation",
        "table": "subscriptions / opportunities / revenue_schedule",
        "description": "ARR (run-rate), Bookings (won ACV), and Recognized Revenue "
                       "(monthly straight-line) are distinct and will not match.",
        "correct_handling": "Pick the correct measure for the question: run-rate=ARR/MRR, "
                            "signed=bookings, earned=recognized revenue.",
        "magnitude": None,
    })

    return manifest
