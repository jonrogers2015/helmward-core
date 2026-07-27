"""
SQLite persistence for the agent-os control plane.

Deliberately simple and deterministic: one shared connection in WAL mode, a single
module-level lock around writes (operations are short). The DB is the system of record;
NATS only carries in-flight messages.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_DB_PATH = os.environ.get("DB_PATH", "./data/agentos.db")
_SCHEMA_PATH = os.environ.get("SCHEMA_PATH", Path(__file__).resolve().parent.parent / "schema.sql")

_conn: Optional[sqlite3.Connection] = None
_write_lock = threading.Lock()


# --------------------------------------------------------------------------- helpers
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str = "task") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _connect() -> sqlite3.Connection:
    Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db() -> None:
    """Open the connection and apply schema.sql (idempotent)."""
    global _conn
    if _conn is None:
        _conn = _connect()
    schema = Path(_SCHEMA_PATH).read_text(encoding="utf-8")
    with _write_lock:
        _conn.executescript(schema)
        _conn.commit()


def _c() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("db not initialised; call init_db() first")
    return _conn


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


# ----------------------------------------------------------------------------- events
def add_event(source: str, type: str, message: str = "",
              task_id: Optional[str] = None, ref: Optional[str] = None) -> None:
    with _write_lock:
        _c().execute(
            "INSERT INTO events (ts, source, type, task_id, message, ref) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now_iso(), source, type, task_id, message, ref),
        )
        _c().commit()


def list_events(limit: int = 50) -> list[dict]:
    cur = _c().execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
    )
    rows = [_row_to_dict(r) for r in cur.fetchall()]
    rows.reverse()  # oldest-first for the feed
    return rows


# ------------------------------------------------------------------------------ tasks
_VALID_INITIAL_STATUSES = {"queued", "needs_approval"}


def create_task(capability: str, payload: Any, max_attempts: int = 3,
                parent_id: Optional[str] = None,
                idempotency_key: Optional[str] = None,
                verification_spec: Optional[Any] = None,
                initial_status: str = "queued") -> dict:
    """Insert a task, atomically, in the given initial_status (default 'queued').
    If idempotency_key already exists, return the existing task.

    initial_status exists so a task that must NEVER be briefly claimable
    -- e.g. one gated behind a human approval -- can be born directly in
    'needs_approval' rather than created as 'queued' and updated a
    moment later. That two-step pattern is a real race: claim_task()
    only checks `WHERE status='queued'`, so any task sitting there,
    however briefly, is fair game for an agent to grab and actually
    execute -- regardless of what it gets set to microseconds
    afterward. Confirmed in practice 2026-07-27: this exact gap let a
    denied blog-post publish task run anyway and go live without
    authorization. Only 'queued' and 'needs_approval' are accepted;
    anything else raises before any write happens.
    """
    if initial_status not in _VALID_INITIAL_STATUSES:
        raise ValueError(
            f"initial_status must be one of {sorted(_VALID_INITIAL_STATUSES)}, "
            f"got {initial_status!r}"
        )
    if idempotency_key:
        existing = get_task_by_idempotency(idempotency_key)
        if existing:
            return existing
    task_id = new_id("task")
    ts = now_iso()
    payload_str = payload if isinstance(payload, str) else json.dumps(payload)
    verification_spec_str = (
        None if verification_spec is None
        else verification_spec if isinstance(verification_spec, str)
        else json.dumps(verification_spec)
    )
    with _write_lock:
        try:
            _c().execute(
                "INSERT INTO tasks (id, capability, payload, status, assigned_agent, "
                "attempts, max_attempts, result, error, parent_id, idempotency_key, "
                "created_at, updated_at, verification_spec) "
                "VALUES (?, ?, ?, ?, NULL, 0, ?, NULL, NULL, ?, ?, ?, ?, ?)",
                (task_id, capability, payload_str, initial_status, max_attempts, parent_id,
                 idempotency_key, ts, ts, verification_spec_str),
            )
            _c().commit()
        except sqlite3.IntegrityError:
            # raced on idempotency_key -- return the winner
            existing = get_task_by_idempotency(idempotency_key) if idempotency_key else None
            if existing:
                return existing
            raise
    add_event("control-plane", "task_created", f"task {task_id} ({capability}, {initial_status})",
              task_id=task_id)
    return get_task(task_id)


def get_task(task_id: str) -> Optional[dict]:
    cur = _c().execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


def get_task_by_idempotency(key: str) -> Optional[dict]:
    cur = _c().execute("SELECT * FROM tasks WHERE idempotency_key = ?", (key,))
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


def get_tasks_by_parent(parent_id: str) -> list[dict]:
    cur = _c().execute(
        "SELECT * FROM tasks WHERE parent_id = ? ORDER BY created_at DESC", (parent_id,)
    )
    return [_row_to_dict(r) for r in cur.fetchall()]


def list_tasks(limit: int = 100) -> list[dict]:
    cur = _c().execute(
        "SELECT * FROM tasks ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
    )
    return [_row_to_dict(r) for r in cur.fetchall()]


def list_tasks_by_status(status: str) -> list[dict]:
    cur = _c().execute(
        "SELECT * FROM tasks WHERE status = ? ORDER BY created_at ASC", (status,)
    )
    return [_row_to_dict(r) for r in cur.fetchall()]


def assign_task(task_id: str, agent_id: str) -> Optional[dict]:
    """
    Compare-and-swap: queued -> assigned, set agent, bump attempts. Only transitions if
    the task is STILL 'queued' (WHERE ... AND status='queued'). Returns the updated task,
    or None if it was already claimed/assigned by someone else (e.g. an HTTP pull worker).
    This is what keeps the NATS push path and the HTTP pull path from both grabbing the
    same task.
    """
    with _write_lock:
        cur = _c().execute(
            "UPDATE tasks SET status='assigned', assigned_agent=?, "
            "attempts = attempts + 1, updated_at=? WHERE id=? AND status='queued'",
            (agent_id, now_iso(), task_id),
        )
        _c().commit()
        changed = cur.rowcount
    if not changed:
        return None
    return get_task(task_id)


def set_task_status(task_id: str, status: str) -> None:
    with _write_lock:
        _c().execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
            (status, now_iso(), task_id),
        )
        _c().commit()


def complete_task(task_id: str, result: Any) -> None:
    result_str = result if isinstance(result, str) else json.dumps(result)
    with _write_lock:
        _c().execute(
            "UPDATE tasks SET status='done', result=?, error=NULL, updated_at=? WHERE id=?",
            (result_str, now_iso(), task_id),
        )
        _c().commit()


def fail_task(task_id: str, error: str) -> None:
    """
    Mark a task failed. Also clears the assigned agent's current_task, if it had one --
    this was a real bug found 2026-07-13: only the normal work_complete() flow used to
    clear this (it explicitly passes current_task=None to set_agent_heartbeat), so any
    task failed another way -- manual DB fixes, or the timeout sweeper's dead-letter path
    in dispatch.py (which calls fail_task() directly) -- left the agent showing a stale
    current_task in the dashboard/API indefinitely, looking "busy" with a task that had
    already resolved. Read the task BEFORE updating it (need its assigned_agent), and
    call set_agent_heartbeat() OUTSIDE the write-lock block below to avoid re-entering
    the same non-reentrant lock (set_agent_heartbeat acquires it itself).
    """
    task = get_task(task_id)
    with _write_lock:
        _c().execute(
            "UPDATE tasks SET status='failed', error=?, updated_at=? WHERE id=?",
            (error, now_iso(), task_id),
        )
        _c().commit()
    if task and task.get("assigned_agent"):
        set_agent_heartbeat(task["assigned_agent"], current_task=None)


def requeue_task(task_id: str) -> None:
    """
    Requeue a task (typically from the timeout sweeper, when a task's still under its
    retry budget). Also clears the assigned agent's current_task -- same bug class as
    fail_task() above, found while fixing that one: this function already cleared the
    TASK's own assigned_agent column, but never touched the AGENT's current_task field,
    which would otherwise keep showing the agent as "busy" with a task that's already
    been requeued for someone else to pick up. Same before/after-lock pattern as
    fail_task() for the same reason (set_agent_heartbeat isn't reentrant-safe to call
    from inside the write lock).
    """
    task = get_task(task_id)
    with _write_lock:
        _c().execute(
            "UPDATE tasks SET status='queued', assigned_agent=NULL, updated_at=? WHERE id=?",
            (now_iso(), task_id),
        )
        _c().commit()
    if task and task.get("assigned_agent"):
        set_agent_heartbeat(task["assigned_agent"], current_task=None)


def set_task_verifying(task_id: str, claimed_result: Any) -> None:
    """
    Agent reported success on a task that has a verification_spec. Don't call
    complete_task() yet -- park the claimed result and move to 'verifying'.
    The dispatch loop's sweep picks up 'verifying' tasks and runs the actual check
    (see dispatch.py). This is the line that replaces trusting the agent outright.
    """
    result_str = claimed_result if isinstance(claimed_result, str) else json.dumps(claimed_result)
    with _write_lock:
        _c().execute(
            "UPDATE tasks SET status='verifying', result=?, error=NULL, updated_at=? WHERE id=?",
            (result_str, now_iso(), task_id),
        )
        _c().commit()
    add_event("control-plane", "verifying", f"task {task_id} claimed done, verification pending",
              task_id=task_id)


def resolve_verification(task_id: str, passed: bool, detail: str) -> None:
    """
    Transition a 'verifying' task to 'done' (check passed) or 'failed' (check did not
    confirm the agent's claim). Records the verification outcome so the dashboard can
    show it distinctly from a normal agent-side error.

    Also clears the assigned agent's current_task -- a third instance of the same bug
    class fixed in fail_task() and requeue_task() above, found while checking whether
    this function had it too (it did). This function is called exclusively from the
    verification_sweeper background loop in dispatch.py, a completely separate code
    path from the normal work_complete() flow that clears current_task itself -- so
    NEITHER outcome (passed or failed) was clearing it here, for any task that ever
    went through verification. Same before/after-lock pattern as the other two fixes,
    for the same reason (set_agent_heartbeat isn't reentrant-safe to call from inside
    the write lock).
    """
    task = get_task(task_id)
    verification_result_str = json.dumps({"passed": passed, "detail": detail})
    ts = now_iso()
    with _write_lock:
        if passed:
            _c().execute(
                "UPDATE tasks SET status='done', verification_result=?, verified_at=?, "
                "updated_at=? WHERE id=?",
                (verification_result_str, ts, ts, task_id),
            )
        else:
            _c().execute(
                "UPDATE tasks SET status='failed', "
                "error=?, verification_result=?, verified_at=?, updated_at=? WHERE id=?",
                (f"Agent reported success but verification failed: {detail}",
                 verification_result_str, ts, ts, task_id),
            )
        _c().commit()
    if task and task.get("assigned_agent"):
        set_agent_heartbeat(task["assigned_agent"], current_task=None)
    add_event("control-plane",
              "verification_passed" if passed else "verification_failed",
              detail, task_id=task_id)


def claim_task(agent_id: str, capabilities: list[str],
               running_status: str = "running") -> Optional[dict]:
    """
    Atomically claim the OLDEST queued task whose capability is in `capabilities`.
    The select + update happen under one write lock so two concurrent polls can never
    grab the same task. Returns the claimed task (now 'running'/'assigned') or None.

    Used by the HTTP pull worker path (POST /api/work/claim). NATS is not involved.

    Also snapshots the agent's CURRENT inference_models value onto the task's
    executed_model column. This is deliberate: agents.inference_models can change
    over time (e.g. swapping the active model on llama-swap), so reading it later
    (at reliability-scorecard time) would misattribute old tasks to whatever model
    is active *now*. Snapshotting at claim time freezes the true answer.
    """
    caps = [c for c in (capabilities or []) if c]
    if not caps:
        return None
    placeholders = ",".join("?" for _ in caps)
    claimed_id = None
    with _write_lock:
        cur = _c().execute(
            f"SELECT id FROM tasks WHERE status='queued' AND capability IN ({placeholders}) "
            "ORDER BY created_at ASC, id ASC LIMIT 1",
            tuple(caps),
        )
        row = cur.fetchone()
        if row is not None:
            claimed_id = row["id"]
            agent_row = _c().execute(
                "SELECT inference_models FROM agents WHERE agent_id=?", (agent_id,)
            ).fetchone()
            executed_model = agent_row["inference_models"] if agent_row else None
            _c().execute(
                "UPDATE tasks SET status=?, assigned_agent=?, attempts = attempts + 1, "
                "updated_at=?, executed_model=? WHERE id=?",
                (running_status, agent_id, now_iso(), executed_model, claimed_id),
            )
            _c().commit()
    if claimed_id is None:
        return None
    # Side effects outside the lock (these helpers take the lock themselves).
    add_event(f"agent:{agent_id}", "claimed", f"task {claimed_id} claimed",
              task_id=claimed_id, ref=f"agent:{agent_id}")
    return get_task(claimed_id)


# ------------------------------------------------------------------ reliability scorecard
def get_model_reliability_stats() -> list[dict]:
    """
    Aggregate verification pass/fail counts grouped by (executed_model, verification
    type). Only considers tasks that actually went through the verification gate
    (verification_result IS NOT NULL) -- tasks with no verification_spec are not
    counted, since there's nothing to have passed or failed.

    Aggregation happens in Python, not SQL, deliberately -- verification_result and
    verification_spec are both opaque JSON TEXT columns (see schema.sql), and parsing
    JSON in Python keeps this readable without depending on SQLite's JSON1 extension
    being compiled into every deployment target.

    Returns a list of dicts, one per (model, verification_type) pair:
        {"model": str, "verification_type": str, "passed": int, "failed": int,
         "total": int, "pass_rate": float}
    Rows with a NULL executed_model (predates the migration, or agent had no model
    info at claim time) are grouped under the literal string "unknown" rather than
    silently dropped, so the total counts still reconcile.
    """
    cur = _c().execute(
        "SELECT executed_model, verification_spec, verification_result, status "
        "FROM tasks WHERE verification_result IS NOT NULL"
    )
    stats: dict[tuple[str, str], dict[str, int]] = {}
    for row in cur.fetchall():
        model = row["executed_model"] or "unknown"
        try:
            spec = json.loads(row["verification_spec"]) if row["verification_spec"] else {}
            vtype = spec.get("type", "unknown")
        except (ValueError, TypeError):
            vtype = "unknown"
        try:
            result = json.loads(row["verification_result"]) if row["verification_result"] else {}
            passed = bool(result.get("passed"))
        except (ValueError, TypeError):
            passed = False
        key = (model, vtype)
        bucket = stats.setdefault(key, {"passed": 0, "failed": 0})
        if passed:
            bucket["passed"] += 1
        else:
            bucket["failed"] += 1

    out = []
    for (model, vtype), counts in sorted(stats.items()):
        total = counts["passed"] + counts["failed"]
        out.append({
            "model": model,
            "verification_type": vtype,
            "passed": counts["passed"],
            "failed": counts["failed"],
            "total": total,
            "pass_rate": round(counts["passed"] / total, 4) if total else 0.0,
        })
    return out


# ---------------------------------------------------------------------------- agents
def upsert_agent(agent: dict) -> dict:
    """Insert or update an agent by agent_id. Unspecified columns keep their values."""
    a = dict(agent)
    aid = a["agent_id"]
    if isinstance(a.get("capabilities"), (list, dict)):
        a["capabilities"] = json.dumps(a["capabilities"])
    if isinstance(a.get("inference_models"), (list, dict)):
        a["inference_models"] = json.dumps(a["inference_models"])
    existing = get_agent(aid)
    with _write_lock:
        if existing is None:
            _c().execute(
                "INSERT INTO agents (agent_id, role_name, host_identity, ipv4, ipv6, "
                "inference_endpoint, inference_provider, inference_models, listening_port, "
                "worker_type, capabilities, status, last_heartbeat, current_task, "
                "concurrency_limit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (aid, a.get("role_name"), a.get("host_identity"), a.get("ipv4"),
                 a.get("ipv6"), a.get("inference_endpoint"), a.get("inference_provider"),
                 a.get("inference_models"), a.get("listening_port"), a.get("worker_type"),
                 a.get("capabilities"), a.get("status", "online"), now_iso(),
                 a.get("current_task"), a.get("concurrency_limit", 1)),
            )
        else:
            merged = {**existing, **{k: v for k, v in a.items() if v is not None}}
            _c().execute(
                "UPDATE agents SET role_name=?, host_identity=?, ipv4=?, ipv6=?, "
                "inference_endpoint=?, inference_provider=?, inference_models=?, "
                "listening_port=?, worker_type=?, capabilities=?, status=?, "
                "last_heartbeat=?, current_task=?, concurrency_limit=? WHERE agent_id=?",
                (merged.get("role_name"), merged.get("host_identity"), merged.get("ipv4"),
                 merged.get("ipv6"), merged.get("inference_endpoint"),
                 merged.get("inference_provider"), merged.get("inference_models"),
                 merged.get("listening_port"), merged.get("worker_type"),
                 merged.get("capabilities"), merged.get("status", "online"), now_iso(),
                 merged.get("current_task"), merged.get("concurrency_limit", 1), aid),
            )
        _c().commit()
    return get_agent(aid)


def get_agent(agent_id: str) -> Optional[dict]:
    cur = _c().execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,))
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


def list_agents() -> list[dict]:
    cur = _c().execute("SELECT * FROM agents ORDER BY agent_id")
    return [_row_to_dict(r) for r in cur.fetchall()]


def delete_agent(agent_id: str) -> bool:
    """Delete an agent row. Returns True if a row was removed, False if not found."""
    with _write_lock:
        cur = _c().execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))
        _c().commit()
        removed = cur.rowcount
    if removed:
        add_event("control-plane", "agent_retired", f"{agent_id} removed from registry",
                  ref=f"agent:{agent_id}")
    return bool(removed)


def set_agent_heartbeat(agent_id: str, status: Optional[str] = None,
                        current_task: Optional[str] = "__keep__") -> None:
    """Refresh heartbeat; optionally update status and current_task."""
    sets = ["last_heartbeat=?"]
    params: list[Any] = [now_iso()]
    if status is not None:
        sets.append("status=?")
        params.append(status)
    if current_task != "__keep__":
        sets.append("current_task=?")
        params.append(current_task)
    params.append(agent_id)
    with _write_lock:
        _c().execute(f"UPDATE agents SET {', '.join(sets)} WHERE agent_id=?", params)
        _c().commit()


def count_agent_active_tasks(agent_id: str) -> int:
    cur = _c().execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE assigned_agent=? "
        "AND status IN ('assigned','running')",
        (agent_id,),
    )
    return cur.fetchone()["n"]


# -------------------------------------------------------------------------- approvals
def create_approval(task_id: str, action: str, details: str = "") -> dict:
    aid = new_id("apr")
    with _write_lock:
        _c().execute(
            "INSERT INTO approvals (id, task_id, action, details, status, created_at, "
            "resolved_at, decided_by) VALUES (?, ?, ?, ?, 'pending', ?, NULL, NULL)",
            (aid, task_id, action, details, now_iso()),
        )
        _c().commit()
    add_event("control-plane", "approval_requested", action, task_id=task_id,
              ref=f"approval:{aid}")
    return get_approval(aid)


def get_approval(approval_id: str) -> Optional[dict]:
    cur = _c().execute("SELECT * FROM approvals WHERE id = ?", (approval_id,))
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


def list_approvals() -> list[dict]:
    cur = _c().execute("SELECT * FROM approvals ORDER BY created_at DESC")
    return [_row_to_dict(r) for r in cur.fetchall()]


def resolve_approval(approval_id: str, decision: str,
                     decided_by: str = "jon") -> Optional[dict]:
    if decision not in ("approved", "denied"):
        raise ValueError("decision must be 'approved' or 'denied'")
    ap = get_approval(approval_id)
    if ap is None:
        return None
    with _write_lock:
        _c().execute(
            "UPDATE approvals SET status=?, resolved_at=?, decided_by=? WHERE id=?",
            (decision, now_iso(), decided_by, approval_id),
        )
        _c().commit()
    add_event("control-plane", "approval_resolved",
              f"{approval_id} {decision}", task_id=ap.get("task_id"),
              ref=f"approval:{approval_id}")
    return get_approval(approval_id)


_transport_col_ready = False


def _ensure_transport_column() -> None:
    """Idempotent lazy migration: agents.transport column."""
    global _transport_col_ready
    if _transport_col_ready:
        return
    cols = {row[1] for row in _c().execute("PRAGMA table_info(agents)").fetchall()}
    if "transport" not in cols:
        with _write_lock:
            _c().execute("ALTER TABLE agents ADD COLUMN transport TEXT")
            _c().commit()
    _transport_col_ready = True


def mark_agent_transport_pull(agent_id: str) -> None:
    """Record that this agent fetches work via HTTP pull (/api/work/claim).

    Pull workers do not subscribe to NATS, so the push dispatch loop must
    never select them as push targets: assign_task would burn an attempt on
    a delivery the agent can never hear, making the task invisible to
    claim_task (status no longer 'queued') until the sweeper requeues or
    dead-letters it. Found in practice on the CT100 clean install: probe
    child tasks (max_attempts=1) sniped this way dead-lettered at exactly
    +2:00 and falsely failed their parent's verification.
    """
    _ensure_transport_column()
    with _write_lock:
        _c().execute(
            "UPDATE agents SET transport='pull' "
            "WHERE agent_id=? AND (transport IS NULL OR transport != 'pull')",
            (agent_id,),
        )
        _c().commit()
