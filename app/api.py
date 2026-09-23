"""FastAPI service for the Governed Analytics Copilot.

Security posture (see also the deterministic RLS in copilot/security):
  * Inputs are schema-validated (Pydantic, extra fields forbidden, length-capped).
  * `persona` is validated against a strict allow-list resolved from the data (deny by
    default). NOTE: choosing a persona is a DEMO affordance to showcase RLS. In production
    the principal is derived from the authenticated session, never from a client field.
  * All SQL is parameterized in the compiler; the DB is opened READ-ONLY (least privilege).
  * Restrictive CORS (explicit localhost origins, never '*') + security headers + a simple
    in-memory per-IP rate limit on the ask endpoint.
  * Errors return a generic message to the client; details are logged server-side.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from copilot.service import Copilot

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("copilot.api")

FIXTURE = os.environ.get("COPILOT_FIXTURE", "data/fixture_a")
ALLOWED_ORIGINS = os.environ.get(
    "COPILOT_ALLOWED_ORIGINS",
    "http://localhost:8000,http://127.0.0.1:8000").split(",")
STATIC_DIR = Path(__file__).with_name("static")

# One copilot + a lock (DuckDB connection is shared; serialize access for the demo).
_copilot: Copilot | None = None
_lock = threading.Lock()
_personas: set[str] = set()


@asynccontextmanager
async def lifespan(_app):
    global _copilot, _personas
    _copilot = Copilot(FIXTURE)
    _personas = {p["persona_id"] for p in _copilot.personas()}
    log.info("copilot ready on fixture=%s as_of=%s personas=%d",
             FIXTURE, _copilot.engine.as_of, len(_personas))
    yield
    _copilot.close()


app = FastAPI(title="Governed Analytics Copilot", version="1.0",
              docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"])


# --- security headers + rate limit ----------------------------------------------------
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'")
    return resp


_hits: dict[str, deque] = defaultdict(deque)
_RATE, _WINDOW = 60, 60.0   # 60 requests / 60s per IP on /api/ask


def _rate_limited(ip: str) -> bool:
    now = time.time()
    dq = _hits[ip]
    while dq and now - dq[0] > _WINDOW:
        dq.popleft()
    if len(dq) >= _RATE:
        return True
    dq.append(now)
    return False


# --- schema ---------------------------------------------------------------------------
class AskRequest(BaseModel):
    model_config = {"extra": "forbid"}
    question: str = Field(min_length=1, max_length=500)
    persona: str = Field(min_length=1, max_length=64)


# --- routes ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"status": "ok", "fixture": FIXTURE,
            "as_of": str(_copilot.engine.as_of) if _copilot else None}


@app.get("/api/personas")
def personas():
    return {"personas": _copilot.personas()}


@app.get("/api/metrics")
def metrics():
    m = _copilot.engine.manifest
    return {"metrics": [
        {"name": mt.name, "label": mt.label, "category": mt.category,
         "description": mt.description, "dimensions": sorted(mt.dimensions)}
        for mt in m.metrics.values()]}


@app.post("/api/ask")
def ask(req: AskRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    if _rate_limited(ip):
        raise HTTPException(status_code=429, detail="Too many requests. Please slow down.")
    if req.persona not in _personas:            # deny-by-default allow-list
        raise HTTPException(status_code=400, detail="Unknown persona.")
    try:
        with _lock:
            principal = _copilot.engine.principal_for_persona(req.persona)
            resp = _copilot.ask(req.question, principal)
    except Exception:                            # never leak internals to the client
        log.exception("ask failed")
        raise HTTPException(status_code=500, detail="Could not process the request.")

    if resp.status == "error":                   # generic to client; detail already logged
        log.warning("engine error: %s", resp.message)
        resp.message = "Could not answer that question."
    return JSONResponse(resp.__dict__)


# --- static UI ------------------------------------------------------------------------
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    idx = STATIC_DIR / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    return JSONResponse({"status": "ok", "hint": "UI not built; see /api/docs"})
