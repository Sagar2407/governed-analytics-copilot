"""Deterministic helpers shared across generator modules.

Everything random flows through a single numpy Generator plus a single seeded Faker
instance, so a given (seed) reproduces byte-identical fixtures.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np
import pandas as pd
from faker import Faker


@dataclass
class Ctx:
    """Bundle of the seeded RNG + Faker instance passed to every generator function."""

    rng: np.random.Generator
    fake: Faker
    seed: int

    @classmethod
    def create(cls, seed: int) -> "Ctx":
        rng = np.random.default_rng(seed)
        fake = Faker()
        fake.seed_instance(seed)
        Faker.seed(seed)  # also seed the shared instance for any module-level use
        return cls(rng=rng, fake=fake, seed=seed)


# --- picking / sampling ---------------------------------------------------------------

def weighted_keys(d: dict) -> tuple[list, np.ndarray]:
    """Split a {key: weight} or {key: {'weight': w, ...}} dict into (keys, prob array)."""
    keys = list(d.keys())
    raw = []
    for k in keys:
        v = d[k]
        raw.append(v["weight"] if isinstance(v, dict) else float(v))
    w = np.asarray(raw, dtype=float)
    return keys, w / w.sum()


def pick(ctx: Ctx, options, weights=None):
    """Pick a single element (returns the element, not an index)."""
    idx = ctx.rng.choice(len(options), p=weights)
    return options[idx]


def picks(ctx: Ctx, options, size, weights=None):
    idx = ctx.rng.choice(len(options), size=size, p=weights, replace=True)
    return [options[i] for i in idx]


def uniform(ctx: Ctx, lo: float, hi: float) -> float:
    return float(ctx.rng.uniform(lo, hi))


def randint(ctx: Ctx, lo: int, hi: int) -> int:
    """Inclusive integer in [lo, hi]."""
    return int(ctx.rng.integers(lo, hi + 1))


def chance(ctx: Ctx, p: float) -> bool:
    return bool(ctx.rng.random() < p)


# --- ids ------------------------------------------------------------------------------

def make_ids(prefix: str, n: int, width: int = 6, start: int = 1) -> list[str]:
    return [f"{prefix}-{i:0{width}d}" for i in range(start, start + n)]


def hash_id(prefix: str, *parts) -> str:
    """Stable short id derived from parts (used for events, snapshots, etc.)."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12]
    return f"{prefix}-{h}"


# --- dates ----------------------------------------------------------------------------

def add_months(ts: pd.Timestamp, n: int) -> pd.Timestamp:
    return (ts + pd.DateOffset(months=n)).normalize()


def month_start(ts: pd.Timestamp) -> pd.Timestamp:
    return ts.normalize().replace(day=1)


def quarter_label(ts: pd.Timestamp) -> str:
    return f"{ts.year}-Q{(ts.month - 1) // 3 + 1}"


def random_date_between(ctx: Ctx, start: pd.Timestamp, end: pd.Timestamp) -> pd.Timestamp:
    """Uniform random *date* (normalized to midnight) in [start, end]."""
    start = pd.Timestamp(start).normalize()
    end = pd.Timestamp(end).normalize()
    span = max((end - start).days, 0)
    return start + pd.Timedelta(days=int(ctx.rng.integers(0, span + 1)))


def seasonal_multiplier(ts: pd.Timestamp, *, q4_boost: float = 0.0,
                        january_boost: float = 0.0) -> float:
    """Simple seasonality: optional lift in Q4 (deals) and January (churn)."""
    m = 1.0
    if q4_boost and ts.month in (10, 11, 12):
        m *= 1.0 + q4_boost * (ts.month - 9) / 3.0
    if january_boost and ts.month == 1:
        m *= 1.0 + january_boost
    return m


def money(x: float) -> float:
    return round(float(x), 2)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))
