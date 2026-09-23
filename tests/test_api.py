"""API regression tests: every result shape must be JSON-serializable.

Grouped-by-time results carry pandas Timestamps and counts carry numpy ints; the default
JSON encoder rejects both. These tests exercise the /api/ask path (which the CLI/engine
tests do not) so that regression can't come back.
"""

from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def _api(fixture_dir, monkeypatch):
    monkeypatch.setenv("COPILOT_FIXTURE", fixture_dir)
    import app.api as api
    importlib.reload(api)          # re-read COPILOT_FIXTURE at module import
    return api


def test_api_serializes_all_result_shapes(fixture_dir, monkeypatch):
    api = _api(fixture_dir, monkeypatch)
    with TestClient(api.app) as client:
        # scalar currency
        r = client.post("/api/ask", json={"question": "What is our ARR?", "persona": "PER-FINANCE"})
        assert r.status_code == 200 and r.json()["status"] == "ok"

        # scalar count (numpy int64)
        r = client.post("/api/ask", json={"question": "active customers", "persona": "PER-FINANCE"})
        assert r.status_code == 200 and r.json()["status"] == "ok"

        # grouped by quarter -> pandas Timestamps in rows (this shape returned 500 before)
        r = client.post("/api/ask", json={
            "question": "ARR by quarter over the last 12 months", "persona": "PER-FINANCE"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["rows"] and isinstance(body["rows"][0]["quarter"], str)  # Timestamp -> str

        # the exact question from the bug report
        r = client.post("/api/ask", json={
            "question": "bookings by quarter over the last 12 months", "persona": "PER-FINANCE"})
        assert r.status_code == 200 and r.json()["status"] == "ok"


def test_api_clarify_and_persona_allowlist(fixture_dir, monkeypatch):
    api = _api(fixture_dir, monkeypatch)
    with TestClient(api.app) as client:
        assert client.post("/api/ask", json={
            "question": "show me revenue", "persona": "PER-FINANCE"}).json()["status"] == "clarify"
        # deny-by-default: unknown persona is rejected
        assert client.post("/api/ask", json={
            "question": "ARR", "persona": "NOT-A-PERSONA"}).status_code == 400
        assert client.get("/api/health").json()["status"] == "ok"
