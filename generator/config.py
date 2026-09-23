"""Configuration: tunable scale/behavior knobs plus fixed reference constants.

The `Config` dataclass holds everything the generator needs. Reference domains
(regions, segments, plans, ...) are module-level constants because they define the
*semantics* the copilot's metric layer will later be built against; the numeric knobs
on `Config` control volume and behaviour and can be scaled up or down per fixture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import pandas as pd

# --------------------------------------------------------------------------------------
# Reference domains (the "shape" of the business)
# --------------------------------------------------------------------------------------

# Segment behaviour. Monthly probabilities drive the subscription lifecycle simulation.
SEGMENTS: dict[str, dict] = {
    "SMB": {
        "weight": 0.55,
        "seat_range": (5, 40),
        "plans": ["Starter", "Pro"],
        "base_monthly_churn": 0.022,
        "expand_p": 0.030,
        "contract_p": 0.016,
        "discount_range": (0.00, 0.15),
        "annual_billing_p": 0.45,
    },
    "Mid-Market": {
        "weight": 0.30,
        "seat_range": (25, 150),
        "plans": ["Pro", "Business"],
        "base_monthly_churn": 0.011,
        "expand_p": 0.045,
        "contract_p": 0.012,
        "discount_range": (0.05, 0.25),
        "annual_billing_p": 0.70,
    },
    "Enterprise": {
        "weight": 0.15,
        "seat_range": (150, 1200),
        "plans": ["Business", "Enterprise"],
        "base_monthly_churn": 0.004,
        "expand_p": 0.050,
        "contract_p": 0.009,
        "discount_range": (0.10, 0.40),
        "annual_billing_p": 0.92,
    },
}

REGIONS: dict[str, dict] = {
    "NA": {
        "weight": 0.45,
        "sub_regions": ["NA-West", "NA-East", "NA-Central"],
        "countries": ["United States", "Canada"],
    },
    "EMEA": {
        "weight": 0.30,
        "sub_regions": ["UKI", "EMEA-DACH", "EMEA-North", "EMEA-South"],
        "countries": ["United Kingdom", "Germany", "France", "Netherlands", "Spain"],
    },
    "APAC": {
        "weight": 0.17,
        "sub_regions": ["APAC-ANZ", "APAC-SEA", "APAC-Japan", "APAC-India"],
        "countries": ["Australia", "Singapore", "Japan", "India"],
    },
    "LATAM": {
        "weight": 0.08,
        "sub_regions": ["LATAM-Brazil", "LATAM-Mexico"],
        "countries": ["Brazil", "Mexico"],
    },
}

# Country -> billing currency (some countries bill in USD).
COUNTRY_CURRENCY: dict[str, str] = {
    "United States": "USD",
    "Canada": "USD",
    "United Kingdom": "GBP",
    "Germany": "EUR",
    "France": "EUR",
    "Netherlands": "EUR",
    "Spain": "EUR",
    "Australia": "AUD",
    "Singapore": "USD",
    "Japan": "JPY",
    "India": "USD",
    "Brazil": "BRL",
    "Mexico": "USD",
}

COUNTRY_TZ: dict[str, str] = {
    "United States": "America/New_York",
    "Canada": "America/Toronto",
    "United Kingdom": "Europe/London",
    "Germany": "Europe/Berlin",
    "France": "Europe/Paris",
    "Netherlands": "Europe/Amsterdam",
    "Spain": "Europe/Madrid",
    "Australia": "Australia/Sydney",
    "Singapore": "Asia/Singapore",
    "Japan": "Asia/Tokyo",
    "India": "Asia/Kolkata",
    "Brazil": "America/Sao_Paulo",
    "Mexico": "America/Mexico_City",
}

# Base FX rate = value of 1 unit of local currency in USD. A gentle monthly random walk
# is applied around these anchors so multi-currency conversion is a real (but well
# defined) requirement rather than a corrupting bug.
FX_BASE: dict[str, float] = {
    "USD": 1.00,
    "EUR": 1.08,
    "GBP": 1.27,
    "AUD": 0.66,
    "JPY": 0.0067,
    "BRL": 0.20,
}

# Per-seat monthly list prices (USD). Effective price is lower after segment discounts.
PLANS: list[dict] = [
    {"plan_id": "PLN-STARTER", "plan_name": "Starter", "tier": 1, "list_price_monthly_usd": 12},
    {"plan_id": "PLN-PRO", "plan_name": "Pro", "tier": 2, "list_price_monthly_usd": 30},
    {"plan_id": "PLN-BUSINESS", "plan_name": "Business", "tier": 3, "list_price_monthly_usd": 60},
    {"plan_id": "PLN-ENTERPRISE", "plan_name": "Enterprise", "tier": 4, "list_price_monthly_usd": 100},
]
PLAN_NAME_TO_ID: dict[str, str] = {p["plan_name"]: p["plan_id"] for p in PLANS}
PLAN_ID_TO_TIER: dict[str, int] = {p["plan_id"]: p["tier"] for p in PLANS}
PLAN_NAME_TO_PRICE: dict[str, float] = {p["plan_name"]: p["list_price_monthly_usd"] for p in PLANS}

INDUSTRIES: list[str] = [
    "Technology", "Financial Services", "Healthcare", "Retail & eCommerce",
    "Manufacturing", "Media & Entertainment", "Education", "Government",
    "Professional Services", "Energy & Utilities", "Travel & Hospitality",
    "Telecommunications",
]

# Internal GTM org roles.
ROLES: list[str] = [
    "Account Executive", "Sales Development Rep", "Customer Success Manager",
    "CS Team Lead", "Sales Manager", "Finance Analyst", "Finance Lead",
    "RevOps Analyst", "Executive", "Admin",
]

USAGE_EVENT_TYPES: dict[str, str] = {
    # event_type -> feature it belongs to
    "login": "Core",
    "dashboard_view": "Dashboards",
    "dashboard_create": "Dashboards",
    "report_run": "Reports",
    "report_export": "Reports",
    "report_share": "Sharing",
    "query_run": "Explore",
    "alert_create": "Alerts",
    "integration_sync": "Integrations",
    "api_call": "API",
    "invite_user": "Admin",
}
# Events that count as "key actions" for activation / adoption metrics.
KEY_ACTIONS: set[str] = {"dashboard_create", "report_run", "query_run", "alert_create"}

OPP_STAGES: list[str] = [
    "Prospecting", "Qualification", "Proposal", "Negotiation",
    "Closed Won", "Closed Lost",
]
OPEN_STAGES: list[str] = ["Prospecting", "Qualification", "Proposal", "Negotiation"]
STAGE_PROBABILITY: dict[str, float] = {
    "Prospecting": 0.10,
    "Qualification": 0.25,
    "Proposal": 0.50,
    "Negotiation": 0.75,
    "Closed Won": 1.0,
    "Closed Lost": 0.0,
}
STAGE_FORECAST_CATEGORY: dict[str, str] = {
    "Prospecting": "Pipeline",
    "Qualification": "Pipeline",
    "Proposal": "Best Case",
    "Negotiation": "Commit",
    "Closed Won": "Closed",
    "Closed Lost": "Omitted",
}
OPP_TYPES: list[str] = ["New Business", "Expansion", "Renewal"]
LEAD_SOURCES: list[str] = [
    "Inbound", "Outbound", "Partner", "Event", "Referral", "Web", "Marketing Campaign",
]
COMPETITORS: list[str] = [
    "Incumbent BI", "Spreadsheet Status Quo", "In-house Build", "RivalCo", "None",
]

TICKET_PRIORITIES: list[str] = ["Low", "Medium", "High", "Urgent"]
TICKET_CATEGORIES: list[str] = [
    "Bug", "How-To", "Billing", "Feature Request", "Outage", "Onboarding",
]
REFUND_REASONS: list[str] = [
    "Downgrade credit", "Billing error", "Goodwill", "Cancellation", "Overpayment",
]

# --------------------------------------------------------------------------------------
# Tunable config
# --------------------------------------------------------------------------------------


@dataclass
class Config:
    """All numeric / temporal knobs for one fixture build."""

    seed: int = 42
    fixture_name: str = "fixture_a"

    # Volume
    n_accounts: int = 400
    n_users: int = 90               # internal GTM org headcount
    end_users_per_seat: float = 0.05  # end-user rows created relative to seats (capped)
    max_usage_events: int = 600_000   # hard cap; events are down-sampled if exceeded

    # Time window (calendar == fiscal for simplicity)
    start_date: pd.Timestamp = field(default_factory=lambda: pd.Timestamp("2023-01-01"))
    end_date: pd.Timestamp = field(default_factory=lambda: pd.Timestamp("2026-06-30"))
    as_of_date: pd.Timestamp = field(default_factory=lambda: pd.Timestamp("2026-06-30"))
    # Fraction of accounts that already existed at start_date (installed base).
    installed_base_fraction: float = 0.35

    # Pipeline
    pipeline_snapshot_weekday: int = 4        # Friday snapshots of open opps
    open_pipeline_multiplier: float = 1.4     # how much open pipeline to carry at as_of
    win_rate_target: float = 0.34             # baseline new-business win rate

    # Activity
    support_tickets_per_account_year: float = 6.0

    # Governance / quirks
    ownership_transfer_fraction: float = 0.25   # accounts that change owner at least once
    internal_account_fraction: float = 0.02     # test/internal accounts to be excluded
    duplicate_line_item_fraction: float = 0.010 # accidental exact-duplicate line items
    late_arrival_fraction: float = 0.015        # usage events that arrived late
    refund_invoice_fraction: float = 0.06       # invoices that receive a credit/refund
    null_industry_fraction: float = 0.04        # accounts with missing industry
    null_lead_source_fraction: float = 0.08     # opps with missing lead source

    # Email domain (reserved .example TLD => guaranteed non-routable, no real PII)
    internal_email_domain: str = "acme-analytics.example"
    customer_email_domain: str = "customer.example"

    def month_starts(self) -> list[pd.Timestamp]:
        return list(pd.date_range(self.start_date, self.end_date, freq="MS"))


# Scale presets ------------------------------------------------------------------------

def scale_config(scale: str, seed: int, fixture_name: str) -> Config:
    """Return a Config tuned to a named scale. 'smoke' is for fast tests."""
    presets = {
        "smoke": dict(n_accounts=25, n_users=20, max_usage_events=20_000,
                      start_date=pd.Timestamp("2024-01-01"),
                      end_date=pd.Timestamp("2025-06-30"),
                      as_of_date=pd.Timestamp("2025-06-30")),
        "small": dict(n_accounts=120, n_users=40, max_usage_events=150_000),
        "medium": dict(n_accounts=400, n_users=90, max_usage_events=600_000),
        "large": dict(n_accounts=1500, n_users=220, max_usage_events=2_500_000),
    }
    if scale not in presets:
        raise ValueError(f"unknown scale {scale!r}; choose from {sorted(presets)}")
    return Config(seed=seed, fixture_name=fixture_name, **presets[scale])
