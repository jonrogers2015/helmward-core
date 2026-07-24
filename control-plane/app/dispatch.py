"""
The always-on dispatch loop + result/event consumers + timeout sweeper.

These run as asyncio background tasks started on app startup. This is the "Claude is not a
daemon" piece: the orchestration loop lives here, in the control plane.

State machine handled here (broker-side redelivery is handled by JetStream ack_wait):
    queued -> assigned -> running -> verifying -> done | failed
              ^                                      |
              +------ requeue (sweeper) -------------+   (attempts < max_attempts)
              attempts == max_attempts -> failed (dead-letter)

    'verifying' is a new state: an agent claimed done, but the task had a
    verification_spec, so a fresh probe task (see verification_sweeper) confirms
    the claim before it's allowed to become 'done'.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import traceback
from datetime import datetime, timezone

from . import db
from .verification import (  # pure verification logic; no I/O
    build_raw_command as _build_raw_command,
    evaluate_probe_result as _evaluate_probe_result,
)

DISPATCH_INTERVAL = float(os.environ.get("DISPATCH_INTERVAL", "2"))
TASK_TIMEOUT = float(os.environ.get("TASK_TIMEOUT", "120"))
SWEEP_INTERVAL = float(os.environ.get("SWEEP_INTERVAL", "10"))

_HEARTBEAT_STALE = 30.0  # seconds without a heartbeat => treat agent as offline

# Standard `ls -l` line: permission bits, link count, owner, group, SIZE, month, day...
# Captures the size as group 1, wherever this pattern appears in the string -- robust to
# agents that wrap raw command output in JSON/markdown despite being told not to (observed
# in practice: Apex/Hermes routinely narrates or reformats output rather than returning it
# verbatim, so a fixed word-position parse is too brittle).
_LS_LINE_RE = re.compile(
    r'^[-dlcbps][-rwxsStT]{9}\+?\s+\d+\s+\S+\s+\S+\s+(\d+)\s+\w+\s+\d+',
    re.MULTILINE,
)


def _parse_iso(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _age_seconds(ts: str | None) -> float:
    dt = _parse_iso(ts)
    if dt is None:
        return 0.0
    return (datetime.now(timezone.utc) - dt).total_seconds()


# ----------------------------------------------------------------------- sweeper
async def timeout_sweeper(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            # mark agents offline if their heartbeat is stale
            for agent in db.list_agents():
                if agent.get("status") == "online" and \
                        _age_seconds(agent.get("last_heartbeat")) > _HEARTBEAT_STALE:
                    db.set_agent_heartbeat(agent["agent_id"], status="offline")
                    db.add_event("control-plane", "agent_offline",
                                 f"{agent['agent_id']} heartbeat stale",
                                 ref=f"agent:{agent['agent_id']}")
            # requeue or dead-letter stuck tasks. This is transport-agnostic: a PULL
            # (apex-real) task stuck in 'running' is requeued the same way -- it just goes
            # back to 'queued' for re-claim via /api/work/claim (no NATS involved), since
            # the dispatch loop skips PULL_CAPABILITIES.
            for status in ("assigned", "running"):
                for task in db.list_tasks_by_status(status):
                    if _age_seconds(task.get("updated_at")) <= TASK_TIMEOUT:
                        continue
                    if (task.get("attempts") or 0) < (task.get("max_attempts") or 3):
                        db.requeue_task(task["id"])
                        db.add_event("control-plane", "retry",
                                     f"task {task['id']} timed out -> requeued "
                                     f"(attempt {task.get('attempts')})", task_id=task["id"])
                    else:
                        db.fail_task(task["id"], "max_attempts exceeded (dead-letter)")
                        db.add_event("control-plane", "dead_letter",
                                     f"task {task['id']} dead-lettered", task_id=task["id"])
        except Exception as exc:
            db.add_event("control-plane", "error", f"sweeper: {exc}")
        await _sleep_or_stop(stop, SWEEP_INTERVAL)


# ------------------------------------------------------------- verification sweeper
def _find_probe_task(parent_task_id: str) -> dict | None:
    children = db.get_tasks_by_parent(parent_task_id)
    return children[0] if children else None  # most recent first (created_at DESC)


async def verification_sweeper(stop: asyncio.Event) -> None:
    """
    Resolves tasks sitting in 'verifying' status. Design: reuse the existing
    task queue/claim/complete pipeline to run a fresh probe task rather than
    trusting the original claim. The probe is a completely separate task
    (linked via parent_id), dispatched through the normal pipeline, so it goes
    through the same claim/complete cycle as any other task.

    The probe's payload carries a literal `raw_command` rather than a `prompt` --
    the worker (see agentos-poller.sh) runs this directly via the shell instead
    of routing it through the LLM. This is what makes verification actually
    independent of the original agent's claim: there is no model call in the
    probe path at all, so there's nothing left to fabricate.
    """
    while not stop.is_set():
        try:
            for task in db.list_tasks_by_status("verifying"):
                try:
                    spec_raw = task.get("verification_spec")
                    if not spec_raw:
                        db.resolve_verification(task["id"], False,
                                                "task in verifying state with no verification_spec")
                        continue
                    try:
                        spec = json.loads(spec_raw)
                    except (ValueError, TypeError):
                        db.resolve_verification(task["id"], False,
                                                f"could not parse verification_spec: {spec_raw}")
                        continue

                    probe = _find_probe_task(task["id"])

                    if probe is None:
                        raw_cmd = _build_raw_command(spec)
                        db.create_task(
                            capability=task.get("capability") or "general",
                            payload={"raw_command": raw_cmd},
                            max_attempts=1,
                            parent_id=task["id"],
                        )
                        continue

                    if probe.get("status") in ("queued", "assigned", "running"):
                        if _age_seconds(task.get("updated_at")) > TASK_TIMEOUT:
                            db.resolve_verification(task["id"], False,
                                                    "verification probe did not complete in time")
                        continue

                    if probe.get("status") == "failed":
                        db.resolve_verification(task["id"], False,
                                                f"verification probe itself failed: {probe.get('error')}")
                        continue

                    passed, detail = _evaluate_probe_result(
                        spec, probe.get("result") or "", task.get("result") or ""
                    )
                    db.resolve_verification(task["id"], passed, detail)

                except Exception as task_exc:
                    print(f"verification_sweeper ERROR on task {task.get('id')}: {task_exc}\n{traceback.format_exc()}", flush=True)
                    db.add_event("control-plane", "error",
                                 f"verification_sweeper task {task.get('id')}: {task_exc}")

        except Exception as exc:  # never let the loop die
            print(f"verification_sweeper LOOP ERROR: {exc}\n{traceback.format_exc()}", flush=True)
            db.add_event("control-plane", "error", f"verification_sweeper: {exc}")
        await _sleep_or_stop(stop, SWEEP_INTERVAL)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


# --------------------------------------------------------------- lifecycle helpers
async def start_background_tasks() -> tuple[asyncio.Event, list]:
    """Launch the background loops. Returns (stop_event, tasks).

    Pull-only: no subscriptions, no dispatch loop. Tasks sit in 'queued' until
    an HTTP worker claims them via POST /api/work/claim.
    """
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(timeout_sweeper(stop)),
        asyncio.create_task(verification_sweeper(stop)),
    ]
    db.add_event("control-plane", "started", "sweepers online (pull-only)")
    return stop, tasks
