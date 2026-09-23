"""Run the copilot web app.

Sets the working directory to the repo root so `app.api` imports and the default
`data/fixture_a` fixture path resolve, regardless of where the process is launched from.

    python serve.py                 # http://127.0.0.1:8000
    PORT=8080 python serve.py
    COPILOT_FIXTURE=data/fixture_b python serve.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("app.api:app",
                host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8000")))
