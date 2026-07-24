#!/usr/bin/env python3
"""helmward doctor -- diagnose a Helmward install.

Standalone by design: stdlib only, and it reads the SQLite DB directly as
well as probing the HTTP API, so it still reports something useful when the
control plane won't start at all.

Every check here corresponds to a failure that actually happened during the
CT100 clean-install bring-up (2026-07-21/22). The one that matters most is
`capability routing`: it catches the class of bug where an HTTP pull worker
advertises a capability that is NOT in PULL_CAPABILITIES, so the dispatch
loop pushes its tasks to a NATS subject nobody is subscribed to. Those tasks
leave 'queued', burn an attempt, become unclaimable, and dead-letter --
which, for verification probe children (max_attempts=1), silently and
falsely fails the parent task.

Usage:
    python3 tools/doctor.py
    DB_PATH=/path/to/agentos.db python3 tools/doctor.py

Exit code is 0 if nothing FAILed, 1 otherwise. WARNs do not fail the run.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

CP_URL = os.environ.get("HELMWARD_CP_URL", "http://127.0.0.1:8080").rstrip("/")
DB_PATH = os.environ.get("DB_PATH", "/opt/helmward/control-plane/data/agentos.db")
MCP_URL = os.environ.get("HELMWARD_MCP_URL", "http://127.0.0.1:8765").rstrip("/")
NATS_HOST = os.environ.get("NATS_HOST", "127.0.0.1")
NATS_PORT = int(os.environ.get("NATS_PORT", "4222"))
PULL_CAPABILITIES = {
    c.strip()
    for c in os.environ.get("PULL_CAPABILITIES", "apex-real").split(",")
    if c.strip()
}
HEARTBEAT_STALE = 30.0
TASK_TIMEOUT = float(os.environ.get("TASK_TIMEOUT", "120"))

_fails = 0
_warns = 0


def emit(level: str, check: str, detail: str = "") -> None:
    global _fails, _warns
    if level == "FAIL":
        _fails += 1
    elif level == "WARN":
        _warns += 1
    tag = {"PASS": "  OK  ", "WARN": " WARN ", "FAIL": " FAIL ", "INFO": " info "}[level]
    print("[%s] %s" % (tag, check))
    if detail:
        for line in str(detail).splitlines():
            print("         %s" % line)


def section(title: str) -> None:
    print()
    print("== %s" % title)


def http_json(url: str, timeout: float = 5.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def age_seconds(ts):
    if not ts:
        return None
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


def agent_caps(raw):
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else [str(val)]
    except (ValueError, TypeError):
        return [c.strip() for c in str(raw).split(",") if c.strip()]


def tcp_open(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------- config
section("config")
emit("INFO", "control plane URL", CP_URL)
emit("INFO", "database path", DB_PATH)
emit("INFO", "PULL_CAPABILITIES", ", ".join(sorted(PULL_CAPABILITIES)) or "(empty)")
if "PULL_CAPABILITIES" not in os.environ:
    emit(
        "INFO",
        "PULL_CAPABILITIES source",
        "not set in this shell's environment -- showing the code default.\n"
        "If the control plane runs under systemd with a different value, this\n"
        "check is comparing against the wrong set. Confirm with:\n"
        "  systemctl show helmward-control-plane -p Environment",
    )

# ---------------------------------------------------------------- control plane
section("control plane")
cp_up = False
try:
    health = http_json(CP_URL + "/healthz")
    cp_up = True
    emit("PASS", "HTTP reachable", "%s/healthz -> %s" % (CP_URL, health))
except urllib.error.URLError as exc:
    emit(
        "FAIL",
        "HTTP unreachable",
        "%s: %s\nIf the service reports active, it may still be binding --\n"
        "uvicorn accepts connections a beat after systemd returns." % (CP_URL, exc),
    )
except Exception as exc:
    emit("FAIL", "HTTP error", "%s: %s" % (CP_URL, exc))

# ------------------------------------------------------------------- database
section("database")
conn = None
if not os.path.exists(DB_PATH):
    emit("FAIL", "database missing", DB_PATH)
else:
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True)
        conn.row_factory = sqlite3.Row
        emit("PASS", "database readable", DB_PATH)
    except sqlite3.Error as exc:
        emit("FAIL", "database unreadable", "%s: %s" % (DB_PATH, exc))

if conn is not None:
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall()}
        if not cols:
            emit("FAIL", "agents table missing", "schema.sql may never have been applied")
        elif "transport" in cols:
            emit("PASS", "agents.transport present", "pull/push routing fix is in effect")
        else:
            emit(
                "WARN",
                "agents.transport absent",
                "Created lazily on the first /api/work/claim by a registered agent.\n"
                "Absent means either no pull worker has claimed yet, or this install\n"
                "predates the dispatch fix. If a pull worker HAS claimed and this is\n"
                "still missing, the dispatch race is live -- see tools/doctor.py header.",
            )
    except sqlite3.Error as exc:
        emit("FAIL", "schema check failed", str(exc))

# --------------------------------------------------------------------- agents
section("agents")
agents = []
if conn is not None:
    try:
        agents = [dict(r) for r in conn.execute("SELECT * FROM agents ORDER BY agent_id")]
    except sqlite3.Error as exc:
        emit("FAIL", "could not read agents", str(exc))

if not agents:
    emit("WARN", "no agents registered", "nothing will ever claim work")
else:
    for a in agents:
        aid = a.get("agent_id")
        caps = agent_caps(a.get("capabilities"))
        transport = a.get("transport") or "(unknown)"
        hb = age_seconds(a.get("last_heartbeat"))
        hb_txt = "never" if hb is None else "%.0fs ago" % hb
        emit(
            "INFO",
            aid,
            "status=%s transport=%s heartbeat=%s caps=%s"
            % (a.get("status"), transport, hb_txt, ",".join(caps) or "(none)"),
        )
        if a.get("status") == "online" and hb is not None and hb > HEARTBEAT_STALE:
            emit(
                "WARN",
                "%s: stale heartbeat while marked online" % aid,
                "%.0fs since last beat (stale threshold %.0fs). The sweeper should\n"
                "flip this to offline; if it does not, the sweeper may not be running."
                % (hb, HEARTBEAT_STALE),
            )

# --------------------------------------------------------- capability routing
section("capability routing")
if not agents:
    emit("INFO", "skipped", "no agents to check")
else:
    trouble = False
    for a in agents:
        aid = a.get("agent_id")
        transport = (a.get("transport") or "").lower()
        for cap in agent_caps(a.get("capabilities")):
            if transport == "pull" and cap not in PULL_CAPABILITIES:
                trouble = True
                emit(
                    "WARN",
                    "%s: pull worker on non-pull capability %r" % (aid, cap),
                    "The dispatch loop will consider this agent a NATS push target for\n"
                    "%r tasks. With the transport fix in place _pick_agent skips it, so\n"
                    "those tasks correctly stay queued -- but the capability is still\n"
                    "misconfigured. Add it to PULL_CAPABILITIES:\n"
                    "  PULL_CAPABILITIES=%s"
                    % (cap, ",".join(sorted(PULL_CAPABILITIES | {cap}))),
                )
    if not trouble:
        emit("PASS", "no pull/push capability mismatches")

# ---------------------------------------------------------------------- tasks
section("tasks")
if conn is not None:
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status ORDER BY status"
        ).fetchall()
        if rows:
            emit("INFO", "task counts", "  ".join("%s=%d" % (r[0], r[1]) for r in rows))
        else:
            emit("INFO", "task counts", "no tasks yet")

        stuck = []
        for r in conn.execute(
            "SELECT id, status, attempts, max_attempts, assigned_agent, updated_at "
            "FROM tasks WHERE status IN ('assigned','running','verifying')"
        ):
            age = age_seconds(r["updated_at"])
            if age is not None and age > TASK_TIMEOUT:
                stuck.append((r["id"], r["status"], age, r["assigned_agent"]))
        if stuck:
            for tid, st, age, who in stuck[:10]:
                emit(
                    "WARN",
                    "task stuck in %s" % st,
                    "%s  %.0fs since update (timeout %.0fs)  agent=%s"
                    % (tid, age, TASK_TIMEOUT, who),
                )
            if len(stuck) > 10:
                emit("INFO", "...and %d more stuck tasks" % (len(stuck) - 10))
        else:
            emit("PASS", "no tasks past the timeout threshold")

        orphan = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='failed' "
            "AND error LIKE '%dead-letter%'"
        ).fetchone()[0]
        if orphan:
            emit(
                "INFO",
                "dead-lettered tasks: %d" % orphan,
                "Historical. Probe children dead-lettering at exactly max_attempts with\n"
                "a null result is the signature of the pull/push dispatch bug.",
            )
    except sqlite3.Error as exc:
        emit("FAIL", "task query failed", str(exc))

# ------------------------------------------------------------------ transports
section("transports")
if tcp_open(NATS_HOST, NATS_PORT):
    emit("PASS", "NATS reachable", "%s:%d" % (NATS_HOST, NATS_PORT))
    subscribed = [
        a.get("agent_id")
        for a in agents
        if (a.get("transport") or "").lower() != "pull"
    ]
    if not subscribed:
        emit(
            "INFO",
            "no push-transport agents registered",
            "Every registered agent claims over HTTP. The NATS push path is running\n"
            "but nothing consumes it -- a candidate for removal.",
        )
else:
    emit(
        "WARN",
        "NATS not reachable",
        "%s:%d -- fine if every agent is an HTTP pull worker, fatal if any\n"
        "agent expects pushed work." % (NATS_HOST, NATS_PORT),
    )

try:
    req = urllib.request.Request(MCP_URL + "/mcp", method="GET")
    urllib.request.urlopen(req, timeout=5.0)
    emit("PASS", "MCP bridge responding", MCP_URL)
except urllib.error.HTTPError as exc:
    if exc.code in (400, 405, 406):
        emit(
            "PASS",
            "MCP bridge responding",
            "%s -- HTTP %d on a bare GET is expected; the endpoint wants a real\n"
            "MCP handshake, not a plain request." % (MCP_URL, exc.code),
        )
    else:
        emit("WARN", "MCP bridge unexpected status", "%s -> HTTP %d" % (MCP_URL, exc.code))
except Exception as exc:
    emit("WARN", "MCP bridge unreachable", "%s: %s" % (MCP_URL, exc))

# --------------------------------------------------------------------- summary
print()
print("=" * 60)
if _fails:
    print("RESULT: %d failure(s), %d warning(s)" % (_fails, _warns))
elif _warns:
    print("RESULT: healthy, with %d warning(s)" % _warns)
else:
    print("RESULT: healthy")
print("=" * 60)

if conn is not None:
    conn.close()

sys.exit(1 if _fails else 0)
