"""
agent-os control plane -- FastAPI app entrypoint.

Serves the JSON/HTTP API, /api/state, and the dashboard on :8080, and runs the
always-on timeout sweeper + verification sweeper as asyncio background tasks.

Pull-only as of 2026-07-23: work is handed out exclusively through
POST /api/work/claim. There is no message bus and no push path, so there is
also no startup dependency on one -- the previous version refused to boot if
NATS did not answer within 30 retries, despite nothing subscribing to it.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

load_dotenv()

from . import db                       # noqa: E402  (after load_dotenv so env is set)
from . import dispatch                 # noqa: E402
from .api import router                # noqa: E402

# Dashboard directory: env override, else repo-relative (agent-os/dashboard).
_DASHBOARD_DIR = os.environ.get(
    "DASHBOARD_DIR", str(Path(__file__).resolve().parents[2] / "dashboard")
)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    stop, bg_tasks = await dispatch.start_background_tasks()
    app.state.stop = stop
    app.state.bg_tasks = bg_tasks
    try:
        yield
    finally:
        stop.set()
        for t in bg_tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*bg_tasks, return_exceptions=True)


app = FastAPI(title="agent-os control plane", version="0.1.0", lifespan=lifespan)
app.include_router(router)


@app.get("/")
def index():
    return _serve_dashboard()


@app.get("/dashboard.html")
def dashboard():
    return _serve_dashboard()


def _serve_dashboard():
    path = Path(_DASHBOARD_DIR) / "dashboard.html"
    if path.exists():
        return FileResponse(str(path), media_type="text/html")
    return JSONResponse(
        status_code=404,
        content={"error": "dashboard.html not found", "looked_in": str(path)},
    )
