"""Standalone harness for the plugin router.

Hermes mounts dashboard/plugin_api.py at /api/plugins/watch-with-me/. This app
mounts the same file at the same prefix so the page, CSP, and polling contract
can be exercised without touching the user's Hermes home.

    python3 -m uvicorn scripts.serve_router:app --port 18099
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))

import plugin_api  # noqa: E402
from watch_with_me import store  # noqa: E402

_db_dir = Path(os.environ.get("WWM_HARNESS_DB_DIR") or tempfile.mkdtemp(prefix="wwm-harness-"))
plugin_api._conn = store.connect(_db_dir / "data.db")

app = FastAPI()
app.include_router(plugin_api.router, prefix="/api/plugins/watch-with-me")
