#!/usr/bin/env python3
"""helmward doctor -- diagnose a Helmward install.

Standalone by design: stdlib only, and it reads the SQLite DB directly as
well as probing the HTTP API, so it still reports something useful when the
control plane won't start at all.

Every check here corresponds to a failure that actually happened during the
CT100 clean-install bring-up and the CT201 hardening that followed
(2026-07-21 .. 2026-07-24).

Architecture note: Helmward is PULL-ONLY as of 2026-07-24. Work is handed out
exclusively through POST /api/work/claim. The NATS push path -- dispatch_loop,
_pick_agent, PULL_CAPABILITIES, the results/events consumers -- was removed
after `ss -tnp | grep 4222` showed a single connection consisting of the
control plane talking to the bus with nothing subscribed on the far side.
That dual-path design was the sole cause of the dispatch race in which an HTTP
pull worker could be selected as a push target, taking its task out of
'queued', burning an attempt, and dead-lettering it unclaimed.

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
emit("INFO", "dispatch model", "pull-only (POST /api/work/claim); no message bus")

# ---------------------------------------------------------------- control plane
section("control plane")
try:
    health = http_json(CP_URL + "/healthz")
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
        else:
            emit("PASS", "agents table present", "%d columns" % len(cols))
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
    live = 0
    for a in agents:
        aid = a.get("agent_id")
        caps = agent_caps(a.get("capabilities"))
        hb = age_seconds(a.get("last_heartbeat"))
        hb_txt = "never" if hb is None else "%.0fs ago" % hb
        if a.get("status") == "online":
            live += 1
        emit(
            "INFO",
            aid,
            "status=%s heartbeat=%s caps=%s"
            % (a.get("status"), hb_txt, ",".join(caps) or "(none)"),
        )
        if a.get("status") == "online" and hb is not None and hb > HEARTBEAT_STALE:
            emit(
                "WARN",
                "%s: stale heartbeat while marked online" % aid,
                "%.0fs since last beat (stale threshold %.0fs). If your poller's\n"
                "interval is close to the threshold this will flap; widen\n"
                "_HEARTBEAT_STALE in dispatch.py rather than chasing it."
                % (hb, HEARTBEAT_STALE),
            )
    if live == 0:
        emit(
            "WARN",
            "no agents currently online",
            "Every registered agent is offline. Queued work will sit unclaimed\n"
            "until a poller comes back.",
        )
    stale_regs = [
        a.get("agent_id")
        for a in agents
        if a.get("status") != "online" and (age_seconds(a.get("last_heartbeat")) or 0) > 86400
    ]
    if stale_regs:
        emit(
            "INFO",
            "registrations idle >24h: %s" % ", ".join(stale_regs),
            "Left over from retired agents. Harmless, but they clutter the roster;\n"
            "DELETE /api/agents/<id> removes one.",
        )

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

        dl = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='failed' "
            "AND error LIKE '%dead-letter%'"
        ).fetchone()[0]
        if dl:
            emit(
                "INFO",
                "dead-lettered tasks: %d" % dl,
                "Mostly historical. Before the pull-only change, any capability not\n"
                "listed in PULL_CAPABILITIES was pushed to a NATS subject with no\n"
                "subscriber and died here. New dead-letters now mean a genuinely\n"
                "unclaimed or repeatedly failing task -- worth investigating.",
            )
    except sqlite3.Error as exc:
        emit("FAIL", "task query failed", str(exc))

# ------------------------------------------------------------------ transports
section("transports")
if tcp_open(NATS_HOST, NATS_PORT):
    emit(
        "WARN",
        "a message bus is listening on %s:%d" % (NATS_HOST, NATS_PORT),
        "Helmward is pull-only and does not use it. Nothing publishes or\n"
        "subscribes. Left running it is dead weight and a restart dependency\n"
        "waiting to be reintroduced:\n"
        "  systemctl stop helmward-nats && systemctl disable helmward-nats\n"
        "Also confirm the control-plane unit does not carry\n"
        "Requires=helmward-nats.service -- that both kills the control plane\n"
        "when the bus stops and pulls the bus back up at boot despite disable.",
    )
else:
    emit("PASS", "no message bus running", "expected -- Helmward is pull-only")

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
