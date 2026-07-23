#!/usr/bin/env python3
import httpx
import json
import uvicorn
from fastmcp import FastMCP
from typing import Optional
HELMWARD_BASE = "http://127.0.0.1:8080"
HEADERS = {"X-Worker-Token": "09121973", "Content-Type": "application/json"}
mcp = FastMCP("Helmward")
@mcp.tool()
def create_task(prompt: str, capability: str = "apex-real",
                 verification_spec: Optional[dict] = None) -> str:
    """Fire a task at a Helmward agent.
    verification_spec (optional): if set, the task is NOT marked done on the
    agent's word alone — the control plane independently checks this spec
    before finalizing status. Use this for anything that changes real state
    (writes a file, restarts a service, etc).
    Supported types:
      {"type": "file_exists", "path": "...", "min_bytes": 100}   (min_bytes optional)
      {"type": "command_output_contains", "command": "...", "expected": "..."}
      {"type": "command_exit_code", "command": "...", "expected_exit_code": 0}
      {"type": "file_checksum", "path": "...", "sha256": "..."}
    For placing exact file content reliably (not a prompt an agent has to
    interpret and reproduce), use write_file instead of create_task -- it
    has no LLM in the write path at all.
    """
    body = {"payload": {"prompt": prompt}, "capability": capability}
    if verification_spec is not None:
        body["verification_spec"] = verification_spec
    r = httpx.post(f"{HELMWARD_BASE}/api/tasks", headers=HEADERS, json=body)
    return json.dumps(r.json(), indent=2)
@mcp.tool()
def write_file(path: str, content: str, capability: str = "apex-real") -> str:
    """Write exact file content to a path on the agent's host, with
    independent byte-for-byte verification -- no LLM in the write path at
    all, so there's nothing to paraphrase, truncate, or drift.

    How it works: the actual write is a literal shell heredoc
    (cat > path << 'UNIQUE_DELIMITER' ... DELIMITER) sent as a
    raw_command -- the worker executes this directly via the shell, the
    same zero-model mechanism verification probes already use elsewhere in
    Helmward. The heredoc delimiter is quoted, so the shell does no
    variable expansion, command substitution, or escaping on the content --
    it's written completely literally, including quotes, backticks, and
    dollar signs. A file_checksum verification_spec is attached
    automatically, so the control plane independently re-hashes the actual
    written file afterward and the task will not resolve as done unless
    that hash matches the one computed here.

    Note: the written file will have exactly one trailing newline,
    regardless of how many (or how few) `content` had -- this matches what
    a shell heredoc always produces and is normalized for automatically,
    both in the hash computed here and in what actually gets written. This
    was found and fixed via local testing before ever being used against a
    real agent: an earlier version hashed `content` as-is, which mismatched
    the heredoc's real output on every single call.

    Use this instead of create_task's prompt-based writes for anything
    where exact content matters (docs, config files, code) -- especially
    anything too large or precise to trust a model to faithfully reproduce
    from a natural-language instruction.

    Returns the task_id and the expected hash. Call wait_for_task on the
    task_id to get the final, independently-verified result.
    """
    import hashlib
    import uuid
    normalized = content.rstrip("\n") + "\n"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    delimiter = f"HELMWARD_EOF_{uuid.uuid4().hex}"
    body = content.rstrip("\n")
    raw_command = (
        f"mkdir -p \"$(dirname '{path}')\" && cat > '{path}' << '{delimiter}'\n"
        f"{body}\n"
        f"{delimiter}"
    )
    body_dict = {
        "payload": {"raw_command": raw_command},
        "capability": capability,
        "verification_spec": {"type": "file_checksum", "path": path, "sha256": digest},
    }
    r = httpx.post(f"{HELMWARD_BASE}/api/tasks", headers=HEADERS, json=body_dict)
    result = r.json()
    result["_expected_sha256"] = digest
    return json.dumps(result, indent=2)
@mcp.tool()
def get_task(task_id: str) -> str:
    """Get the status and result of a task by ID."""
    r = httpx.get(f"{HELMWARD_BASE}/api/tasks/{task_id}", headers=HEADERS)
    return json.dumps(r.json(), indent=2)
@mcp.tool()
def list_tasks() -> str:
    """List recent Helmward tasks."""
    r = httpx.get(f"{HELMWARD_BASE}/api/tasks", headers=HEADERS)
    return json.dumps(r.json()[:10], indent=2)
@mcp.tool()
def list_agents() -> str:
    """List all registered agents and their status."""
    r = httpx.get(f"{HELMWARD_BASE}/api/agents", headers=HEADERS)
    return json.dumps(r.json(), indent=2)
@mcp.tool()
def get_wiki_page(path: str) -> str:
    """Read a wiki page. Example: projects/learnfast-lms.md"""
    r = httpx.get(f"{HELMWARD_BASE}/api/wiki/page", headers=HEADERS, params={"path": path})
    return json.dumps(r.json(), indent=2)
@mcp.tool()
def list_wiki_pages() -> str:
    """List all wiki pages."""
    r = httpx.get(f"{HELMWARD_BASE}/api/wiki/tree", headers=HEADERS)
    return json.dumps(r.json(), indent=2)
@mcp.tool()
def wait_for_task(task_id: str, timeout_seconds: int = 120) -> str:
    """Poll a Helmward task until it completes or times out. Returns the final result."""
    import time
    start = time.time()
    while time.time() - start < timeout_seconds:
        r = httpx.get(f"{HELMWARD_BASE}/api/tasks/{task_id}", headers=HEADERS)
        data = r.json()
        status = data.get("status", "unknown")
        if status in ("done", "failed", "error"):
            return json.dumps(data, indent=2)
        time.sleep(3)
    return json.dumps({"error": "timeout", "task_id": task_id, "timeout_seconds": timeout_seconds})
@mcp.tool()
def get_graph_data() -> str:
    """Get the Helmward knowledge graph data as JSON for visualization."""
    import subprocess
    result = subprocess.run(
        ['python3', '/root/helmward-mcp/graph_data.py'],
        capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else '{"error": "' + result.stderr + '"}'
if __name__ == "__main__":
    app = mcp.http_app(transport="streamable-http", stateless_http=True)
    uvicorn.run(app, host="0.0.0.0", port=8765, forwarded_allow_ips="*", proxy_headers=True)
