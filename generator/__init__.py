"""Seeded synthetic B2B-SaaS data generator for the Governed Analytics Copilot.

This package produces a fully synthetic, reproducible data warehouse that mimics a
mid-size B2B SaaS company's go-to-market and finance data. It is designed to back a
*correctness* benchmark for a natural-language analytics copilot, so the data:

  * is deterministic given a seed (two seeds => two independent "fixtures"),
  * is internally coherent (MRR movements reconstruct ARR, invoices tie to
    subscriptions, bookings tie to won opportunities, etc.),
  * carries governance structure (per-user entitlements + demo personas) so
    row-level security can be exercised, and
  * embeds *documented* data-quality traps (multi-currency, fan-out joins,
    as-of ownership, internal/test accounts, late-arriving events, timezone
    boundaries, NULLs) each with a stated "correct handling" rule.

No real people, companies, credentials, or PII are used. All names are Faker-generated
and all email addresses use the reserved `.example` TLD (RFC 2606).
"""

from .build import build_fixture, build_and_write  # noqa: F401

__all__ = ["build_fixture", "build_and_write"]
