"""Inspect a generated fixture: coherence checks + a live demonstration of every trap.

Usage:
    python scripts/inspect.py [path-to-fixture-dir]   # default: data/fixture_a
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb


def hr(title: str):
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def main():
    fx = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/fixture_a")
    manifest = json.loads((fx / "manifest.json").read_text())
    as_of = manifest["config"]["as_of_date"]
    con = duckdb.connect(str(fx / "warehouse.duckdb"), read_only=True)
    q = lambda sql: con.execute(sql).fetchdf()

    hr(f"FIXTURE {manifest['fixture_name']}  (seed={manifest['seed']}, as_of={as_of})")
    print(f"{manifest['total_rows']:,} rows across {len(manifest['row_counts'])} tables")
    print("Headline KPIs (independent oracle):")
    for k, v in manifest["headline_kpis"].items():
        print(f"   {k:>18}: {v:,.2f}" if isinstance(v, float) else f"   {k:>18}: {v}")

    hr("COHERENCE: ARR reconciles across two independent computations")
    arr_subs = q(f"""
        SELECT round(sum(s.arr_usd)) arr
        FROM subscriptions s JOIN accounts a USING(account_id)
        WHERE s.status='active' AND NOT a.is_internal""").iloc[0]["arr"]
    arr_bridge = q("""
        SELECT round(sum(se.mrr_delta_usd)*12) arr
        FROM subscription_events se JOIN accounts a USING(account_id)
        WHERE NOT a.is_internal""").iloc[0]["arr"]
    print(f"  ARR from subscriptions (active, ex-internal): ${arr_subs:,.0f}")
    print(f"  ARR from cumulative MRR movement bridge:      ${arr_bridge:,.0f}")
    diff = abs(arr_subs - arr_bridge)
    print(f"  match: {diff <= max(2.0, 0.0001 * arr_subs)}  (diff=${diff:,.2f}, rounding)")

    hr("TRAP 1: multi-currency  (naive local sum is meaningless)")
    print(q("""SELECT currency, count(*) invoices, round(sum(amount_local)) local_sum,
                      round(sum(amount_usd)) usd_sum
               FROM invoices GROUP BY 1 ORDER BY 2 DESC""").to_string(index=False))
    naive = q("SELECT round(sum(amount_local)) v FROM invoices").iloc[0]["v"]
    right = q("SELECT round(sum(amount_usd)) v FROM invoices").iloc[0]["v"]
    print(f"  WRONG  sum(amount_local) mixing currencies: {naive:,.0f}")
    print(f"  RIGHT  sum(amount_usd):                     {right:,.0f}")

    hr("TRAP 2: internal/test accounts inflate metrics if not excluded")
    print(q("""SELECT is_internal, count(*) accounts,
                      round(sum(arr_usd)) active_arr
               FROM subscriptions s JOIN accounts USING(account_id)
               WHERE status='active' GROUP BY 1""").to_string(index=False))

    hr("TRAP 3: invoice -> line_item fan-out + accidental duplicate rows")
    li = q("""SELECT count(*) n_rows, count(DISTINCT line_item_id) distinct_ids
              FROM invoice_line_items""").iloc[0]
    print(f"  line_items rows={li['n_rows']:,} distinct_ids={li['distinct_ids']:,} "
          f"(={li['n_rows'] - li['distinct_ids']} exact duplicates)")
    inv_direct = q("SELECT round(sum(amount_usd)) v FROM invoices").iloc[0]["v"]
    via_li = q("SELECT round(sum(amount_usd)) v FROM invoice_line_items").iloc[0]["v"]
    via_li_dedup = q("""SELECT round(sum(amount_usd)) v FROM
                        (SELECT DISTINCT line_item_id, amount_usd FROM invoice_line_items)"""
                     ).iloc[0]["v"]
    print(f"  invoices.amount_usd (canonical):        {inv_direct:,.0f}")
    print(f"  line_items naive sum (double counts):   {via_li:,.0f}")
    print(f"  line_items deduped by id:               {via_li_dedup:,.0f}")

    hr("TRAP 4: as-of ownership (attribution changes over time)")
    acc = q("""SELECT account_id FROM account_ownership GROUP BY 1
               HAVING count(*)>1 ORDER BY 1 LIMIT 1""").iloc[0]["account_id"]
    print(f"  account {acc} ownership history:")
    print(q(f"""SELECT owner_user_id, valid_from, valid_to, is_current
                FROM account_ownership WHERE account_id='{acc}'
                ORDER BY valid_from""").to_string(index=False))

    hr("TRAP 5: timezone boundary (local vs UTC date differ)")
    n_tz = q("SELECT count(*) v FROM usage_events WHERE event_local_date<>event_utc_date").iloc[0]["v"]
    n_late = q("SELECT count(*) v FROM usage_events WHERE loaded_at<>event_local_date").iloc[0]["v"]
    print(f"  events where local date != UTC date: {n_tz:,}")
    print(f"  late-arriving events (loaded_at later): {n_late:,}")

    hr("GOVERNANCE: personas and the row-level scoping each one implies")
    personas = q("SELECT persona_id, label, user_id, scope_summary FROM personas")
    total_cust = q("SELECT count(*) v FROM accounts WHERE NOT is_internal").iloc[0]["v"]
    for _, p in personas.iterrows():
        uid = p["user_id"]
        grants = q(f"SELECT scope_type, scope_value FROM access_map WHERE user_id='{uid}'")
        # resolve visible account count per grant semantics
        visible = _visible_accounts(q, uid, grants, as_of)
        print(f"  {p['label']:<34} scope={p['scope_summary']:<22} "
              f"sees {visible:>4}/{total_cust} accounts")

    con.close()
    print("\nAll checks complete.")


def _visible_accounts(q, uid, grants, as_of) -> int:
    """Mimic what the copilot's compiler will do: union of grant scopes -> account count."""
    ids = set()
    all_scope = False
    for _, g in grants.iterrows():
        st, sv = g["scope_type"], g["scope_value"]
        if st == "all":
            all_scope = True
            break
        elif st == "region":
            df = q(f"SELECT account_id FROM accounts WHERE region='{sv}' AND NOT is_internal")
        elif st == "segment":
            df = q(f"SELECT account_id FROM accounts WHERE segment='{sv}' AND NOT is_internal")
        elif st == "account":
            df = q(f"SELECT account_id FROM accounts WHERE account_id='{sv}' AND NOT is_internal")
        elif st == "owned_accounts":
            df = q(f"""SELECT DISTINCT o.account_id
                       FROM account_ownership o JOIN accounts a USING(account_id)
                       WHERE o.owner_user_id='{uid}' AND NOT a.is_internal
                         AND o.valid_from <= '{as_of}'
                         AND (o.valid_to IS NULL OR o.valid_to > '{as_of}')""")
        else:
            continue
        ids.update(df["account_id"].tolist())
    if all_scope:
        return int(q("SELECT count(*) v FROM accounts WHERE NOT is_internal").iloc[0]["v"])
    return len(ids)


if __name__ == "__main__":
    main()
