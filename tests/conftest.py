"""Pytest fixtures. Builds a tiny, hermetic smoke fixture once per session so tests never
depend on a pre-generated data/ directory (and exercise the generator in passing)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generator.build import build_and_write


@pytest.fixture(scope="session")
def fixture_dir(tmp_path_factory) -> str:
    out = tmp_path_factory.mktemp("data")
    build_and_write("fixture_test", seed=7, out_dir=out, scale="smoke", verbose=False)
    return str(out / "fixture_test")
