"""Dimension entities: calendar, FX rates, plans, internal GTM org, accounts, ownership."""

from __future__ import annotations

import pandas as pd

from . import config as C
from .util import (
    Ctx, add_months, make_ids, pick, picks, randint, uniform, chance,
    random_date_between, weighted_keys, quarter_label,
)

SENTINEL_END = pd.Timestamp("2999-12-31")


# --------------------------------------------------------------------------------------
# Calendar + FX
# --------------------------------------------------------------------------------------

def gen_calendar(cfg: C.Config) -> pd.DataFrame:
    days = pd.date_range(cfg.start_date, cfg.end_date, freq="D")
    df = pd.DataFrame({"date": days})
    df["month_start"] = df["date"].values.astype("datetime64[M]")
    df["month"] = df["date"].dt.strftime("%Y-%m")
    df["quarter"] = df["date"].map(quarter_label)
    df["fiscal_quarter"] = df["quarter"]  # calendar == fiscal here
    df["year"] = df["date"].dt.year
    df["day_of_week"] = df["date"].dt.day_name()
    df["is_month_end"] = df["date"].dt.is_month_end
    df["is_quarter_end"] = df["date"].dt.is_quarter_end
    return df


def gen_fx_rates(cfg: C.Config, ctx: Ctx) -> tuple[pd.DataFrame, dict]:
    """Monthly rate_to_usd per currency with a gentle random walk. Covers 24 months
    before the window (for pre-window subscription history) through the window end."""
    months = pd.date_range(add_months(cfg.start_date, -24), cfg.end_date, freq="MS")
    rows = []
    lookup: dict[tuple[str, pd.Timestamp], float] = {}
    for cur, base in C.FX_BASE.items():
        rate = base
        for m in months:
            if cur != "USD":
                rate = rate * float(ctx.rng.normal(1.0, 0.012))  # ~1.2% monthly drift
            rate = round(rate, 6)
            rows.append({"currency": cur, "month_start": m, "rate_to_usd": rate})
            lookup[(cur, m)] = rate
    return pd.DataFrame(rows), lookup


def to_usd(amount_local: float, currency: str, when: pd.Timestamp, fx: dict) -> float:
    m = pd.Timestamp(when).normalize().replace(day=1)
    rate = fx.get((currency, m))
    if rate is None:  # clamp to nearest known month for this currency
        known = sorted(k[1] for k in fx if k[0] == currency)
        m = min(known, key=lambda x: abs((x - m).days)) if known else m
        rate = fx.get((currency, m), C.FX_BASE.get(currency, 1.0))
    return round(amount_local * rate, 2)


def from_usd(amount_usd: float, currency: str, when: pd.Timestamp, fx: dict) -> float:
    m = pd.Timestamp(when).normalize().replace(day=1)
    rate = fx.get((currency, m), C.FX_BASE.get(currency, 1.0)) or 1.0
    return round(amount_usd / rate, 2)


def gen_plans() -> pd.DataFrame:
    return pd.DataFrame(C.PLANS)


# --------------------------------------------------------------------------------------
# Internal GTM org (users)
# --------------------------------------------------------------------------------------

_ROLE_MIX = [
    ("Executive", 0.03),
    ("Sales Manager", 0.07),
    ("Account Executive", 0.40),
    ("Sales Development Rep", 0.10),
    ("Customer Success Manager", 0.18),
    ("CS Team Lead", 0.05),
    ("Finance Analyst", 0.05),
    ("Finance Lead", 0.02),
    ("RevOps Analyst", 0.04),
    ("Admin", 0.02),
]


def gen_users(cfg: C.Config, ctx: Ctx) -> pd.DataFrame:
    region_keys, region_w = weighted_keys(C.REGIONS)
    seg_keys, seg_w = weighted_keys(C.SEGMENTS)

    # Resolve counts per role.
    counts: dict[str, int] = {}
    for role, frac in _ROLE_MIX:
        counts[role] = max(1, round(cfg.n_users * frac))
    ids = iter(make_ids("USR", sum(counts.values()) + 5, width=4))

    rows = []
    for role, n in counts.items():
        for _ in range(n):
            region = pick(ctx, region_keys, region_w)
            sub = pick(ctx, C.REGIONS[region]["sub_regions"])
            seg_focus = pick(ctx, seg_keys, seg_w) if role in (
                "Account Executive", "Customer Success Manager", "CS Team Lead") else "All"
            name = ctx.fake.name()
            handle = name.lower().replace(" ", ".").replace("'", "").replace(",", "")
            rows.append({
                "user_id": next(ids),
                "full_name": name,
                "email": f"{handle}@{cfg.internal_email_domain}",
                "role": role,
                "region": region if role not in ("Executive", "Finance Analyst",
                                                 "Finance Lead", "RevOps Analyst",
                                                 "Admin") else "Global",
                "sub_region": sub if role in ("Account Executive",
                                              "Sales Development Rep",
                                              "Sales Manager") else None,
                "segment_focus": seg_focus,
                "hire_date": random_date_between(ctx, add_months(cfg.start_date, -36),
                                                 cfg.as_of_date).date().isoformat(),
                "is_active": chance(ctx, 0.94),
            })
    users = pd.DataFrame(rows)

    # Assign managers: ICs -> a lead/manager in the same region where possible.
    mgr_roles = {"Sales Manager", "CS Team Lead", "Finance Lead", "Executive"}
    managers = users[users["role"].isin(mgr_roles)]
    exec_ids = users.loc[users["role"] == "Executive", "user_id"].tolist()

    def choose_manager(row):
        if row["role"] in ("Executive",):
            return None
        if row["role"] in ("Account Executive", "Sales Development Rep", "Sales Manager"):
            pool = managers[(managers["role"] == "Sales Manager")
                            & (managers["region"] == row["region"])]
            if row["role"] == "Sales Manager":
                pool = users[users["role"] == "Executive"]
        elif row["role"] in ("Customer Success Manager", "CS Team Lead"):
            pool = managers[managers["role"] == "CS Team Lead"]
            if row["role"] == "CS Team Lead":
                pool = users[users["role"] == "Executive"]
        elif row["role"] in ("Finance Analyst", "Finance Lead"):
            pool = users[users["role"] == "Finance Lead"]
            if row["role"] == "Finance Lead":
                pool = users[users["role"] == "Executive"]
        else:
            pool = users[users["role"] == "Executive"]
        pool = pool[pool["user_id"] != row["user_id"]]
        if len(pool):
            return pool.iloc[int(ctx.rng.integers(0, len(pool)))]["user_id"]
        return exec_ids[0] if exec_ids else None

    users["manager_id"] = users.apply(choose_manager, axis=1)
    return users


# --------------------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------------------

def gen_accounts(cfg: C.Config, ctx: Ctx) -> pd.DataFrame:
    region_keys, region_w = weighted_keys(C.REGIONS)
    seg_keys, seg_w = weighted_keys(C.SEGMENTS)
    ids = make_ids("ACC", cfg.n_accounts, width=5)

    rows = []
    for acc_id in ids:
        segment = pick(ctx, seg_keys, seg_w)
        region = pick(ctx, region_keys, region_w)
        sub = pick(ctx, C.REGIONS[region]["sub_regions"])
        country = pick(ctx, C.REGIONS[region]["countries"])
        currency = C.COUNTRY_CURRENCY[country]
        tz = C.COUNTRY_TZ[country]

        if segment == "SMB":
            employees = randint(ctx, 10, 200)
        elif segment == "Mid-Market":
            employees = randint(ctx, 200, 2000)
        else:
            employees = randint(ctx, 2000, 50000)

        if chance(ctx, cfg.installed_base_fraction):
            created = random_date_between(ctx, add_months(cfg.start_date, -24),
                                          cfg.start_date - pd.Timedelta(days=1))
        else:
            created = random_date_between(ctx, cfg.start_date,
                                          cfg.as_of_date - pd.Timedelta(days=45))

        industry = None if chance(ctx, cfg.null_industry_fraction) else pick(ctx, C.INDUSTRIES)
        tier = ("Strategic" if segment == "Enterprise"
                else "Growth" if segment == "Mid-Market" else "Standard")

        rows.append({
            "account_id": acc_id,
            "account_name": ctx.fake.unique.company(),
            "segment": segment,
            "region": region,
            "sub_region": sub,
            "country": country,
            "currency": currency,
            "timezone": tz,
            "industry": industry,
            "employee_count": employees,
            "account_tier": tier,
            "created_date": created.date().isoformat(),
            "is_internal": chance(ctx, cfg.internal_account_fraction),
            "parent_account_id": None,
        })
    accounts = pd.DataFrame(rows)

    # Org hierarchy: ~8% of (non-Enterprise) accounts roll up to an Enterprise parent.
    ent_ids = accounts.loc[accounts["segment"] == "Enterprise", "account_id"].tolist()
    if ent_ids:
        for i, r in accounts.iterrows():
            if r["segment"] != "Enterprise" and chance(ctx, 0.08):
                parent = ent_ids[int(ctx.rng.integers(0, len(ent_ids)))]
                if parent != r["account_id"]:
                    accounts.at[i, "parent_account_id"] = parent
    return accounts


# --------------------------------------------------------------------------------------
# Account ownership (SCD2 with mid-life transfers => "as-of ownership" complexity)
# --------------------------------------------------------------------------------------

def gen_account_ownership(cfg: C.Config, ctx: Ctx, accounts: pd.DataFrame,
                          users: pd.DataFrame) -> pd.DataFrame:
    aes = users[(users["role"] == "Account Executive")]
    rows = []
    oid = iter(make_ids("OWN", cfg.n_accounts * 4, width=6))

    def ae_for(region, segment):
        pool = aes[(aes["region"] == region) & (aes["segment_focus"] == segment)]
        if not len(pool):
            pool = aes[aes["region"] == region]
        if not len(pool):
            pool = aes
        return pool.iloc[int(ctx.rng.integers(0, len(pool)))]["user_id"]

    for _, acc in accounts.iterrows():
        start = max(pd.Timestamp(acc["created_date"]), add_months(cfg.start_date, -24))
        owner = ae_for(acc["region"], acc["segment"])
        # decide transfers (only if there is room after a 60-day tenure)
        n_transfers = 0
        window_start = start + pd.Timedelta(days=60)
        if chance(ctx, cfg.ownership_transfer_fraction) and window_start < cfg.as_of_date:
            n_transfers = randint(ctx, 1, 2)
        transfer_dates = sorted(set(
            random_date_between(ctx, window_start, cfg.as_of_date)
            for _ in range(n_transfers)
        ))
        # keep strictly after start
        transfer_dates = [d for d in transfer_dates if d > start]
        boundaries = [start] + transfer_dates + [SENTINEL_END]
        current_owner = owner
        for i in range(len(boundaries) - 1):
            vf = boundaries[i]
            vt = boundaries[i + 1]
            if i > 0:  # a transfer -> new owner (different where possible)
                new_owner = ae_for(acc["region"], acc["segment"])
                tries = 0
                while new_owner == current_owner and tries < 5:
                    new_owner = ae_for(acc["region"], acc["segment"])
                    tries += 1
                current_owner = new_owner
            rows.append({
                "ownership_id": next(oid),
                "account_id": acc["account_id"],
                "owner_user_id": current_owner,
                "valid_from": pd.Timestamp(vf).date().isoformat(),
                "valid_to": (None if vt == SENTINEL_END
                             else pd.Timestamp(vt).date().isoformat()),
                "is_current": vt == SENTINEL_END,
            })
    return pd.DataFrame(rows)
