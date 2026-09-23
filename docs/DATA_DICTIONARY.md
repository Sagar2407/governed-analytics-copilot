# Data Dictionary

Column-level reference for the synthetic warehouse. The machine-readable schema and exact
row counts for a given fixture are in `data/<fixture>/manifest.json`. All monetary `*_usd`
columns are the canonical, cross-account-summable amounts; `*_local` are in the account's
billing currency and must be FX-converted before aggregating across currencies.

Convention: **grain** = what one row represents.

---

## Entities

### `calendar` — grain: one day
`date`, `month_start`, `month`, `quarter`, `fiscal_quarter` (calendar == fiscal here),
`year`, `day_of_week`, `is_month_end`, `is_quarter_end`.

### `fx_rates` — grain: currency × month
`currency`, `month_start`, `rate_to_usd` (USD value of 1 unit of local currency; gentle
monthly random walk; covers 24 months before the window through the end).

### `plans` — grain: plan
`plan_id`, `plan_name` (Starter/Pro/Business/Enterprise), `tier` (1–4),
`list_price_monthly_usd` (per seat/month; effective price is lower after discount).

### `users` — grain: internal GTM employee
`user_id`, `full_name`, `email` (`@…​.example`), `role`, `region` ("Global" for
finance/exec/admin), `sub_region`, `segment_focus`, `hire_date`, `is_active`, `manager_id`.

### `accounts` — grain: customer account
`account_id`, `account_name`, `segment` (SMB/Mid-Market/Enterprise), `region`,
`sub_region`, `country`, `currency`, `timezone`, `industry` (**nullable**),
`employee_count`, `account_tier`, `created_date`, `is_internal` (**exclude when true**),
`parent_account_id` (**nullable**; org hierarchy — children roll up to an Enterprise parent).

### `account_ownership` — grain: account × ownership interval (SCD2)
`ownership_id`, `account_id`, `owner_user_id`, `valid_from`, `valid_to` (**null = current**),
`is_current`. **As-of attribution:** the owner on date *d* is the row with
`valid_from ≤ d < valid_to`. Use `is_current` only for "today" questions.

---

## Revenue

### `subscriptions` — grain: subscription (an account may have Core + an Add-on line)
`subscription_id`, `account_id`, `product_line`, `plan_id`, `plan_name`, `status`
(active/churned), `start_date`, `end_date` (**null if active**), `billing_frequency`
(monthly/annual), `seats` (current), `currency`, `mrr_usd` (current; 0 if churned),
`mrr_local`, `arr_usd` (current = mrr_usd × 12), `health_score` (0–1; drives churn/expansion
and correlates with usage), `is_primary`.

### `subscription_events` — grain: one MRR-changing event (the movement bridge)
`event_id`, `subscription_id`, `account_id`, `event_type`
(new/expansion/contraction/churn/reactivation/renewal), `event_date`, `old_mrr_usd`,
`new_mrr_usd`, `mrr_delta_usd`, `old_seats`, `new_seats`, `old_plan`, `new_plan`, `reason`.
**Cumulative `mrr_delta_usd` up to a date = point-in-time MRR.** `renewal` events have delta 0.

### `revenue_schedule` — grain: subscription × active month
`subscription_id`, `account_id`, `month_start`, `currency`, `recognized_amount_usd`
(= MRR recognized that month, straight-line), `recognized_amount_local`, `plan_name`,
`seats`, `billing_frequency`.

### `contracts` — grain: subscription × contract term
`contract_id`, `account_id`, `subscription_id`, `start_date`, `end_date`, `term_months`,
`committed_arr_usd`, `tcv_usd`, `currency`, `status`, `is_renewal`.

### `invoices` — grain: invoice
`invoice_id`, `account_id`, `subscription_id` (**null for one-time**), `invoice_date`,
`due_date`, `currency`, `amount_local`, `amount_usd`, `status` (paid/pending/void),
`invoice_type` (subscription/one_time). **Exclude `void`; `pending` = billed-not-collected.**

### `invoice_line_items` — grain: invoice × line (fan-out)
`line_item_id`, `invoice_id`, `account_id`, `item_type`, `description`, `quantity`,
`currency`, `amount_local`, `amount_usd`. **Contains a small number of injected exact
duplicates (same `line_item_id`).** Sum revenue from `invoices`, or dedupe by `line_item_id`.

### `payments` — grain: payment (paid invoices only)
`payment_id`, `invoice_id`, `account_id`, `payment_date`, `amount_local`, `amount_usd`,
`method`, `status`.

### `refunds` — grain: refund / credit
`refund_id`, `invoice_id`, `account_id`, `refund_date`, `currency`, `amount_local`,
`amount_usd`, `reason`. **Net revenue = gross − refunds.**

---

## Pipeline

### `opportunities` — grain: opportunity
`opportunity_id`, `account_id`, `owner_user_id` (**as-of owner at close**), `opp_type`
(New Business/Expansion/Renewal), `stage`, `amount_usd`, `amount_local`, `currency`,
`created_date`, `close_date`, `is_closed`, `is_won`, `forecast_category`, `probability`,
`lead_source` (**nullable**), `competitor`, `subscription_id` (**nullable**; links won opps
to the subscription event). **Bookings = `amount_usd` of `is_won` opps by `close_date`.**

### `opportunity_stage_history` — grain: opportunity × stage
`stage_history_id`, `opportunity_id`, `stage`, `entered_date`, `exited_date` (**null if
current**), `days_in_stage` (**null if open**).

### `opportunity_snapshots` — grain: opportunity × Friday while open
`snapshot_id`, `opportunity_id`, `snapshot_date`, `stage`, `amount_usd`, `close_date`,
`forecast_category`, `is_open`. **Leakage-free point-in-time pipeline** — stage reflects
what was known on `snapshot_date`.

---

## Activity

### `end_users` — grain: end user (customer's employee)
`end_user_id`, `account_id`, `full_name`, `email` (`@…​.example`), `role`, `timezone`,
`is_active`.

### `usage_events` — grain: product event
`event_id`, `account_id`, `end_user_id`, `event_type`, `feature`, `event_timestamp_utc`,
`event_local_time`, `event_local_date`, `event_utc_date`, `timezone`, `session_id`,
`loaded_at`. Fixed standard UTC offsets (DST ignored for reproducibility). **`event_local_date`
≠ `event_utc_date` near midnight; `loaded_at` > `event_local_date` marks late arrivals.**

### `daily_usage` — grain: account × local date
`account_id`, `date`, `active_users`, `sessions`, `events`, `key_actions`. **Rolled up by
LOCAL date.**

### `support_tickets` — grain: ticket
`ticket_id`, `account_id`, `created_at`, `resolved_at` (**null if open**), `status`,
`priority`, `category`, `csat_score` (**null if open**), `first_response_hours`,
`assigned_user_id`.

---

## Governance

### `access_map` — grain: entitlement grant (a user may have several)
`grant_id`, `user_id`, `scope_type` (all/region/segment/account/owned_accounts),
`scope_value`, `granted_date`. A caller sees the **union** of their grants.
`owned_accounts` resolves against `account_ownership` **as-of** the query date.

### `personas` — grain: demo persona
`persona_id`, `label`, `description`, `user_id` (the internal user whose grants apply),
`scope_summary`, `is_default`.

---

## Metric definitions (what "correct" means)

All customer/revenue metrics **exclude `accounts.is_internal = true`** and aggregate in **USD**.

| Metric | Definition |
|--------|-----------|
| **MRR** | Point-in-time monthly recurring revenue = cumulative `subscription_events.mrr_delta_usd` ≤ date (or `revenue_schedule` monthly sum). |
| **ARR** | MRR × 12 (run-rate). |
| **Bookings** | `opportunities.amount_usd` where `is_won`, by `close_date` (New/Expansion/Renewal). |
| **Recognized revenue** | `revenue_schedule.recognized_amount_usd` summed by month (straight-line). |
| **New / Expansion / Contraction / Churn / Reactivation MRR** | `sum(mrr_delta_usd)` by `event_type` by month. |
| **NRR (LTM)** | Cohort active 12 months ago: `sum(MRR now) / sum(MRR then)`. |
| **GRR (LTM)** | Cohort active 12 months ago: `sum(min(MRR now, MRR then)) / sum(MRR then)`. |
| **New / churned logos** | Accounts entering / leaving the monthly active set. |
| **Win rate** | Won ÷ closed New Business opportunities. |
| **MAU** | Distinct `end_user_id` per month by `event_local_date`. |

ARR, bookings, and recognized revenue are **deliberately distinct** and will not match —
choose the measure the question calls for (run-rate vs signed vs earned).

Gold values for every metric are recomputed independently in
`generator/reference_metrics.py` and written to `data/<fixture>/reference_metrics/`.
