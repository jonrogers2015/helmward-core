"""
agent-os control plane — FastAPI app entrypoint.

Serves the JSON/HTTP API, /api/state, and the dashboard on :8080, and runs the always-on
dispatch loop + result/event consumers + timeout sweeper as asyncio background tasks.
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
from .nats_client import NatsClient    # noqa: E402

# Dashboard directory: env override, else repo-relative (agent-os/dashboard).
_DASHBOARD_DIR = os.environ.get(
    "DASHBOARD_DIR", str(Path(__file__).resolve().parents[2] / "dashboard")
)


async def _connect_with_retry(nc: NatsClient, attempts: int = 30, delay: float = 2.0):
    last = None
    for _ in range(attempts):
        try:
            await nc.connect()
            return
        except Exception as exc:  # NATS may still be starting up
            last = exc
            await asyncio.sleep(delay)
    raise RuntimeError(f"could not connect to NATS after {attempts} tries: {last}")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    nc = NatsClient()
    await _connect_with_retry(nc)
    stop, bg_tasks = await dispatch.start_background_tasks(nc)
    app.state.nats = nc
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
        await nc.close()


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
