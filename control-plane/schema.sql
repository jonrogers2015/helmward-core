-- agent-os control plane schema (SQLite)
-- The system of record. NATS carries only in-flight messages; these tables are truth.
-- NOTE: agent identity (agent_id), host (host_identity/ipv4/ipv6) and inference endpoint
-- (inference_endpoint/inference_provider) are SEPARATE columns and must never be conflated.

CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    capability           TEXT,
    payload              TEXT,                 -- JSON string {prompt, context, ...}
    status               TEXT,                 -- queued|assigned|running|done|failed|cancelled|needs_approval
    assigned_agent       TEXT,
    attempts             INTEGER DEFAULT 0,
    max_attempts         INTEGER DEFAULT 3,
    result               TEXT,
    error                TEXT,
    parent_id            TEXT,
    idempotency_key      TEXT UNIQUE,
    created_at           TEXT,
    updated_at           TEXT,
    verification_spec    TEXT,             -- opaque JSON TEXT; see app/dispatch.py
    verification_result  TEXT,             -- opaque JSON TEXT; see app/db.py
    verified_at           TEXT,
    executed_model        TEXT
);

CREATE TABLE IF NOT EXISTS agents (
    agent_id           TEXT PRIMARY KEY,
    role_name          TEXT,
    host_identity      TEXT,              -- where the runner RUNS (machine/container)
    ipv4               TEXT,
    ipv6               TEXT,
    inference_endpoint TEXT,              -- where the runner SENDS inference (may be a different host)
    inference_provider TEXT,
    inference_models   TEXT,              -- comma-separated or JSON
    listening_port     INTEGER,           -- NULL = client-only worker (pull-based)
    worker_type        TEXT,              -- client-only | listening
    capabilities       TEXT,              -- JSON array string, e.g. '["general"]'
    status             TEXT,              -- online|idle|busy|offline
    last_heartbeat     TEXT,
    current_task       TEXT,
    concurrency_limit  INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS approvals (
    id          TEXT PRIMARY KEY,
    task_id     TEXT,
    action      TEXT,
    details     TEXT,
    status      TEXT,                     -- pending|approved|denied
    created_at  TEXT,
    resolved_at TEXT,
    decided_by  TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT,
    source   TEXT,                        -- control-plane | agent:<id>
    type     TEXT,
    task_id  TEXT,
    message  TEXT,
    ref      TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
