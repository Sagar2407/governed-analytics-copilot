"""CLI entry point for building synthetic fixtures.

Examples:
    python run.py                      # build fixture_a (42) + fixture_b (1337), medium
    python run.py --scale smoke        # fast tiny build for testing
    python run.py --scale large --fixtures a=42
    python run.py --out ./data --scale medium
"""

from __future__ import annotations

import argparse
from pathlib import Path

from generator.build import build_and_write

# Default fixtures: two independent seeds so a query that is "right by luck" on one fails
# on the other (a core property of the correctness benchmark).
DEFAULT_FIXTURES = {"fixture_a": 42, "fixture_b": 1337}


def parse_fixtures(spec: list[str] | None) -> dict[str, int]:
    if not spec:
        return DEFAULT_FIXTURES
    out = {}
    for item in spec:
        name, _, seed = item.partition("=")
        name = name if name.startswith("fixture_") else f"fixture_{name}"
        out[name] = int(seed) if seed else 42
    return out


def main():
    ap = argparse.ArgumentParser(description="Generate synthetic Governed Analytics data.")
    ap.add_argument("--out", default="data", help="output directory (default: ./data)")
    ap.add_argument("--scale", default="medium",
                    choices=["smoke", "small", "medium", "large"])
    ap.add_argument("--fixtures", nargs="*", default=None,
                    help="fixture specs like a=42 b=1337 (default: both)")
    args = ap.parse_args()

    fixtures = parse_fixtures(args.fixtures)
    out = Path(args.out)
    print(f"Building {len(fixtures)} fixture(s) at scale={args.scale} into {out.resolve()}")
    for name, seed in fixtures.items():
        build_and_write(name, seed, out, scale=args.scale)
    print("All fixtures built.")


if __name__ == "__main__":
    main()
