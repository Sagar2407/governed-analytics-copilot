"""Governance: per-user entitlements (entitlement map) + curated demo personas.

This is the backbone of the copilot's row-level security. Every internal user gets one or
more entitlement grants describing WHICH accounts they may see; the query compiler will
translate a caller's grants into injected row filters. Scope types:

  all            -> unrestricted (Finance / RevOps / Exec / Admin)
  region         -> accounts in one region (Sales Manager, SDR)
  segment        -> accounts in one segment (CSM, CS Team Lead)
  account        -> a single named account (embedded "account team" view)
  owned_accounts -> accounts the user currently owns, resolved via as-of ownership (AE)

Personas are a small, hand-picked set mapped to real users so the demo persona-switcher
visibly changes results and the RLS eval has concrete, non-empty cases.
"""

from __future__ import annotations

import pandas as pd

from . import config as C
from .util import Ctx, make_ids


def _grant(gid, user_id, scope_type, scope_value, granted_date):
    return {"grant_id": gid, "user_id": user_id, "scope_type": scope_type,
            "scope_value": scope_value, "granted_date": granted_date}


def gen_governance(cfg: C.Config, ctx: Ctx, accounts: pd.DataFrame, users: pd.DataFrame,
                   ownership: pd.DataFrame):
    # current ownership counts per AE -> pick a data-rich AE for the persona
    current = ownership[ownership["is_current"]]
    owned_counts = current.groupby("owner_user_id").size().sort_values(ascending=False)

    def first_user(role):
        pool = users[users["role"] == role]
        return pool.iloc[0]["user_id"] if len(pool) else None

    aes = users[users["role"] == "Account Executive"]["user_id"].tolist()
    persona_ae = owned_counts.index[0] if len(owned_counts) else (aes[0] if aes else None)
    # a different AE reserved for the single-account persona
    single_ae = next((u for u in owned_counts.index if u != persona_ae), None) \
        or (aes[1] if len(aes) > 1 else persona_ae)

    # choose a strategic enterprise account (prefer one owned by single_ae)
    strategic = accounts[(accounts["segment"] == "Enterprise") & (~accounts["is_internal"])]
    single_acct_rows = current[(current["owner_user_id"] == single_ae)
                               & (current["account_id"].isin(strategic["account_id"]))]
    if len(single_acct_rows):
        single_account_id = single_acct_rows.iloc[0]["account_id"]
    elif len(strategic):
        single_account_id = strategic.iloc[0]["account_id"]
    else:
        single_account_id = accounts.iloc[0]["account_id"]

    sales_mgr = first_user("Sales Manager")
    mgr_region = users.loc[users["user_id"] == sales_mgr, "region"].iloc[0] if sales_mgr else "NA"
    cs_lead = first_user("CS Team Lead")
    cs_lead_seg = None
    if cs_lead is not None:
        sf = users.loc[users["user_id"] == cs_lead, "segment_focus"].iloc[0]
        cs_lead_seg = sf if sf in C.SEGMENTS else "Enterprise"
    finance = first_user("Finance Lead") or first_user("Finance Analyst")
    revops = first_user("RevOps Analyst")

    # --- build access_map -------------------------------------------------------------
    gid = iter(make_ids("GRT", len(users) * 3 + 10, width=5))
    grants = []
    for _, u in users.iterrows():
        uid = u["user_id"]
        gd = u["hire_date"]
        if uid == single_ae:
            grants.append(_grant(next(gid), uid, "account", single_account_id, gd))
            continue
        role = u["role"]
        if role in ("Finance Analyst", "Finance Lead", "RevOps Analyst", "Executive", "Admin"):
            grants.append(_grant(next(gid), uid, "all", "*", gd))
        elif role == "Account Executive":
            grants.append(_grant(next(gid), uid, "owned_accounts", uid, gd))
        elif role in ("Sales Manager", "Sales Development Rep"):
            grants.append(_grant(next(gid), uid, "region", u["region"], gd))
        elif role in ("Customer Success Manager", "CS Team Lead"):
            sf = u["segment_focus"]
            if sf in C.SEGMENTS:
                grants.append(_grant(next(gid), uid, "segment", sf, gd))
            else:
                grants.append(_grant(next(gid), uid, "all" if role == "CS Team Lead"
                                     else "region", "*" if role == "CS Team Lead"
                                     else u["region"], gd))
        else:
            grants.append(_grant(next(gid), uid, "region", u.get("region", "NA"), gd))
    access_map = pd.DataFrame(grants)

    # --- personas ---------------------------------------------------------------------
    acc_name = accounts.set_index("account_id")["account_name"].to_dict()
    personas = []

    def add(pid, label, desc, user_id, scope_summary, is_default=False):
        personas.append({"persona_id": pid, "label": label, "description": desc,
                         "user_id": user_id, "scope_summary": scope_summary,
                         "is_default": is_default})

    if finance:
        add("PER-FINANCE", "Finance (Global)",
            "Global finance analyst; sees all accounts and all revenue.", finance,
            "all", is_default=True)
    if revops:
        add("PER-REVOPS", "RevOps Analyst (Global)",
            "Revenue operations; global read across pipeline and revenue.", revops, "all")
    if persona_ae:
        n_owned = int(owned_counts.get(persona_ae, 0))
        add("PER-AE", "Account Executive",
            f"Field AE; sees only their currently-owned book (~{n_owned} accounts), "
            "resolved via as-of ownership.", persona_ae, "owned_accounts")
    if sales_mgr:
        add("PER-SALESMGR", f"Regional Sales Manager - {mgr_region}",
            f"Second-line sales manager; sees all accounts in region {mgr_region}.",
            sales_mgr, f"region={mgr_region}")
    if cs_lead:
        add("PER-CSLEAD", f"CS Team Lead - {cs_lead_seg}",
            f"Customer success lead; sees all {cs_lead_seg} accounts.", cs_lead,
            f"segment={cs_lead_seg}")
    add("PER-ACCTTEAM", f"Account Team - {acc_name.get(single_account_id, single_account_id)}",
        "Embedded single-account view; sees exactly one account and nothing else.",
        single_ae, f"account={single_account_id}")

    personas_df = pd.DataFrame(personas)
    return access_map, personas_df
