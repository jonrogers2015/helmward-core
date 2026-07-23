"""
Async NATS / JetStream wrapper for the control plane.

Conventions (shared across the whole repo — keep these names in sync with the runner,
fake worker, and dispatch loop):

  Stream  : "TASKS"        subjects "tasks.>"
  Tasks   : published to   "tasks.<capability>"   (JetStream, durable, pull-consumed by workers)
  Results : published to   "results"              (core NATS; workers -> control plane)
  Events  : published to   "events"               (core NATS; heartbeats + agent events)

The control plane PUBLISHES tasks and CONSUMES results + events. Workers do the inverse.
Acks in this system are JetStream task-message acks done by the worker after it records a
result; results/events are fire-and-forget core NATS for MVP simplicity.
"""
from __future__ import annotations

import json
import os
from typing import Awaitable, Callable, Optional

import nats
from nats.js.api import StreamConfig

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
# Optional token auth. Empty/unset => no token (current local-loop behavior, unchanged).
NATS_TOKEN = os.environ.get("NATS_TOKEN", "") or None

TASKS_STREAM = "TASKS"
TASKS_SUBJECT_PREFIX = "tasks"          # tasks.<capability>
TASKS_WILDCARD = "tasks.>"
RESULTS_SUBJECT = "results"
EVENTS_SUBJECT = "events"


class NatsClient:
    def __init__(self, url: str = NATS_URL, token: str | None = NATS_TOKEN):
        self.url = url
        self.token = token
        self.nc = None
        self.js = None

    async def connect(self) -> None:
        # Pass token=... only when set, so unauthenticated local NATS still works.
        kwargs = {"max_reconnect_attempts": -1}
        if self.token:
            kwargs["token"] = self.token
        self.nc = await nats.connect(self.url, **kwargs)
        self.js = self.nc.jetstream()
        await self.ensure_stream()

    async def ensure_stream(self) -> None:
        """Create the TASKS JetStream stream if it doesn't exist."""
        try:
            await self.js.stream_info(TASKS_STREAM)
        except Exception:
            await self.js.add_stream(
                StreamConfig(name=TASKS_STREAM, subjects=[TASKS_WILDCARD])
            )

    async def publish_task(self, capability: str, task: dict) -> None:
        """Publish a task to tasks.<capability> on the JetStream stream."""
        subject = f"{TASKS_SUBJECT_PREFIX}.{capability}"
        await self.js.publish(subject, json.dumps(task).encode("utf-8"))

    async def subscribe_results(self, handler: Callable[[dict], Awaitable[None]]):
        """Core-NATS subscribe to results; handler receives the decoded dict."""
        async def _cb(msg):
            try:
                data = json.loads(msg.data.decode("utf-8"))
            except Exception:
                return
            await handler(data)
        return await self.nc.subscribe(RESULTS_SUBJECT, cb=_cb)

    async def subscribe_events(self, handler: Callable[[dict], Awaitable[None]]):
        """Core-NATS subscribe to events/heartbeats; handler receives the decoded dict."""
        async def _cb(msg):
            try:
                data = json.loads(msg.data.decode("utf-8"))
            except Exception:
                return
            await handler(data)
        return await self.nc.subscribe(EVENTS_SUBJECT, cb=_cb)

    async def close(self) -> None:
        if self.nc is not None:
            try:
                await self.nc.drain()
            except Exception:
                pass
            self.nc = None
            self.js = None
