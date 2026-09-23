"""Persist a built fixture to CSV + Parquet + a DuckDB warehouse + manifest.json."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd

from . import config as C


def _cfg_to_dict(cfg: C.Config) -> dict:
    return {
        "seed": cfg.seed,
        "fixture_name": cfg.fixture_name,
        "n_accounts": cfg.n_accounts,
        "n_users": cfg.n_users,
        "start_date": str(cfg.start_date.date()),
        "end_date": str(cfg.end_date.date()),
        "as_of_date": str(cfg.as_of_date.date()),
        "installed_base_fraction": cfg.installed_base_fraction,
        "max_usage_events": cfg.max_usage_events,
        "internal_account_fraction": cfg.internal_account_fraction,
        "ownership_transfer_fraction": cfg.ownership_transfer_fraction,
    }


def write_fixture(cfg: C.Config, tables: dict, reference_metrics: dict, headline: dict,
                  quirks_manifest: list, out_dir: str | Path) -> dict:
    root = Path(out_dir) / cfg.fixture_name
    tdir = root / "tables"
    mdir = root / "reference_metrics"
    for d in (tdir, mdir):
        d.mkdir(parents=True, exist_ok=True)

    row_counts, schema = {}, {}
    for name, df in tables.items():
        if df is None:
            continue
        df.to_csv(tdir / f"{name}.csv", index=False)
        df.to_parquet(tdir / f"{name}.parquet", index=False)
        row_counts[name] = int(len(df))
        schema[name] = list(df.columns)

    for name, df in reference_metrics.items():
        if df is None or not len(df):
            continue
        df.to_csv(mdir / f"{name}.csv", index=False)

    # DuckDB warehouse ------------------------------------------------------------------
    db_path = root / "warehouse.duckdb"
    if db_path.exists():
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    try:
        for name, df in tables.items():
            if df is None or not len(df.columns):
                continue
            con.register("_tmp_view", df)
            con.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM _tmp_view')
            con.unregister("_tmp_view")
        # a convenience schema for reference metrics too
        con.execute("CREATE SCHEMA IF NOT EXISTS reference")
        for name, df in reference_metrics.items():
            if df is None or not len(df):
                continue
            con.register("_tmp_view", df)
            con.execute(f'CREATE OR REPLACE TABLE reference."{name}" AS SELECT * FROM _tmp_view')
            con.unregister("_tmp_view")
    finally:
        con.close()

    manifest = {
        "fixture_name": cfg.fixture_name,
        "seed": cfg.seed,
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "config": _cfg_to_dict(cfg),
        "row_counts": row_counts,
        "total_rows": int(sum(row_counts.values())),
        "schema": schema,
        "headline_kpis": headline,
        "quirks": quirks_manifest,
        "artifacts": {
            "warehouse_duckdb": "warehouse.duckdb",
            "tables_csv": "tables/*.csv",
            "tables_parquet": "tables/*.parquet",
            "reference_metrics_csv": "reference_metrics/*.csv",
        },
    }
    with open(root / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, default=str)
    return manifest
