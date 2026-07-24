"""FastAPI routes for the agent-os control plane."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from . import db
from . import notify
from .models import (
    AgentRegister,
    Approval,
    ApprovalCreate,
    ApprovalResolve,
    TaskIn,
    WorkerClaim,
    WorkerComplete,
    WorkerHeartbeat,
    WorkerRegister,
)

router = APIRouter()

WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "") or None
WIKI_DIR = os.environ.get("WIKI_DIR", "/app/wiki")

# ── Inference model switching config ──────────────────────────────────────
LLAMA_SWAP_URL = os.environ.get("LLAMA_SWAP_URL", "http://192.168.1.180:8081")
HERMES_WEBUI_URL = os.environ.get("HERMES_WEBUI_URL", "http://192.168.1.147:8787")

# Local record of the last model switched via THIS dashboard/API. Best-effort
# only -- it does not detect model changes made through switch-model.ps1
# directly, Hermes's own Settings panel, or any other path. It exists purely
# so the dashboard can show "what was last switched here", not claim to know
# the ground-truth active model system-wide.
_MODELS_STATE_FILE = Path(os.environ.get("DB_PATH", "/app/data/agentos.db")).parent / "last_model_switch.json"


def _read_last_switch() -> dict | None:
    try:
        if _MODELS_STATE_FILE.exists():
            return json.loads(_MODELS_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _write_last_switch(model: str) -> None:
    try:
        _MODELS_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MODELS_STATE_FILE.write_text(
            json.dumps({"model": model, "switched_at": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
    except Exception:
        pass  # non-fatal -- dashboard just won't show a highlighted model


def _llama_swap_host() -> str:
    """Hostname portion of LLAMA_SWAP_URL, used to find every agent that
    actually shares this llama-swap backend."""
    try:
        return urlparse(LLAMA_SWAP_URL).hostname or ""
    except Exception:
        return ""


def _update_agents_for_model_switch(model: str) -> list[str]:
    """Update agents.inference_models for every agent whose inference_endpoint
    points at the same llama-swap host as LLAMA_SWAP_URL.

    This matters because llama-swap serves exactly one active model at a
    time -- switching it changes the REAL active model for every agent that
    routes through this backend, not just whichever agent you happened to be
    looking at on the dashboard when you clicked switch. Confirmed in
    practice: apex-hermes and vibethinker both point at the same llama-swap
    instance (192.168.1.180:8081), so switching to Ornith makes it the real
    active model for both, even though vibethinker's stored inference_models
    still said "vibethinker-3b" until this function runs.

    Without this, agents.inference_models silently goes stale the moment you
    switch models via this endpoint or switch-model.ps1 -- which matters
    beyond just an inaccurate dashboard label, since the reliability
    scorecard's executed_model snapshot (see db.py claim_task()) reads
    exactly this field at claim time. A stale value here means every future
    task gets attributed to the wrong model in that scorecard too.

    Agents on unrelated backends (different host, OpenRouter, FCC SSH, etc.)
    are correctly left untouched -- matching on hostname only, not blanket-
    updating every agent regardless of what they actually connect to.
    """
    host = _llama_swap_host()
    if not host:
        return []
    updated = []
    for agent in db.list_agents():
        endpoint = agent.get("inference_endpoint") or ""
        if host in endpoint:
            db.upsert_agent({"agent_id": agent["agent_id"], "inference_models": model})
            updated.append(agent["agent_id"])
    return updated


# ── Task templates ──────────────────────────────────────────────────────────
_TASK_TEMPLATES_PATH = os.environ.get("TASK_TEMPLATES_PATH", "/app/task_templates.json")


def _load_task_templates() -> list[dict]:
    """Load the template library from disk. Re-read on every call rather than
    cached at import time, so editing the file (or a future admin UI for it)
    takes effect without a container restart. Returns an empty list if the
    file is missing or invalid, rather than raising -- templates are a
    convenience feature, not something that should break task creation if
    the file gets corrupted."""
    try:
        path = Path(_TASK_TEMPLATES_PATH)
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("templates", [])
    except Exception:
        return []


def _fill_template(template_str: str, params: dict) -> str:
    """Substitute {param_name} placeholders with their string form. Used for
    prompt text, where everything ends up as a string anyway. Deliberately
    simple find-replace (not str.format), since str.format chokes on any
    literal { or } elsewhere in a command (e.g. shell brace expansion)."""
    result = template_str
    for key, value in params.items():
        result = result.replace("{" + key + "}", str(value))
    return result


def _fill_template_obj(obj, params: dict):
    """Recursively apply parameter substitution to a dict/list/string
    structure (used for verification_spec_template, which can nest).

    Type-preserving for whole-value placeholders: if a string value is
    *exactly* "{param_name}" with nothing else around it, the raw typed
    param value is substituted directly (e.g. an int stays an int) rather
    than being stringified. This matters concretely for min_bytes: the
    file_exists verification check does an integer comparison downstream,
    and would raise a TypeError if min_bytes came through as the string
    "0" instead of the int 0. Placeholders embedded within a larger string
    (e.g. "df -h {path}") are still stringified via str(), since the
    surrounding text is a string regardless.
    """
    if isinstance(obj, str):
        if obj.startswith("{") and obj.endswith("}") and obj.count("{") == 1:
            key = obj[1:-1]
            if key in params:
                return params[key]
        result = obj
        for key, value in params.items():
            result = result.replace("{" + key + "}", str(value))
        return result
    if isinstance(obj, dict):
        return {k: _fill_template_obj(v, params) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill_template_obj(v, params) for v in obj]
    return obj


def _coerce_params(template: dict, raw_params: dict) -> dict:
    """Cast incoming param values to the type declared in the template's
    'params' list (currently just 'integer' vs default string), so
    type-preserving substitution in _fill_template_obj actually has a real
    int to work with, not a string that merely looks numeric."""
    coerced = {}
    param_defs = {p["name"]: p for p in template.get("params", [])}
    for name, pdef in param_defs.items():
        value = raw_params.get(name, pdef.get("default", ""))
        if pdef.get("type") == "integer":
            try:
                value = int(value)
            except (ValueError, TypeError):
                value = int(pdef.get("default", 0) or 0)
        coerced[name] = value
    return coerced


class TaskFromTemplateRequest(BaseModel):
    template_id: str
    params: dict = {}


class ModelSwitchRequest(BaseModel):
    model: str


def _check_worker_token(token: str | None) -> None:
    if WORKER_TOKEN is None:
        return
    if token != WORKER_TOKEN:
        raise HTTPException(status_code=401, detail="invalid or missing X-Worker-Token")


# ----------------------------------------------------------------------------- tasks
from .verification import (  # single source of truth for spec shape
    SUPPORTED_TYPES as _SUPPORTED_VERIFICATION_TYPES,
    validate_spec as _validate_verification_spec,
)

@router.post("/api/tasks")
def create_task(task_in: TaskIn):
    if task_in.verification_spec is not None:
        try:
            spec = task_in.verification_spec if isinstance(task_in.verification_spec, dict) \
                else json.loads(task_in.verification_spec)
            # Validate the whole spec, not just its type: required keys
            # present and non-empty, no unknown keys (a misspelled key would
            # silently change what gets checked), value types sane. SpecError
            # subclasses ValueError, so the handler below turns it into a 400.
            _validate_verification_spec(spec)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid verification_spec: {exc}")
    task = db.create_task(
        capability=task_in.capability,
        payload=task_in.payload,
        max_attempts=task_in.max_attempts,
        parent_id=task_in.parent_id,
        idempotency_key=task_in.idempotency_key,
        verification_spec=task_in.verification_spec,
    )
    return task


@router.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"no task {task_id}")
    return task


@router.get("/api/tasks")
def list_tasks(limit: int = 100):
    return db.list_tasks(limit=limit)


@router.get("/api/task-templates")
def list_task_templates():
    """List the bundled task-template library -- common verification_spec
    patterns buyers can use without hand-writing JSON. See task_templates.json."""
    return {"templates": _load_task_templates()}


@router.post("/api/tasks/from-template")
def create_task_from_template(body: TaskFromTemplateRequest):
    """Create a task by filling in a named template with the given params,
    rather than requiring the caller to construct prompt + verification_spec
    by hand. Goes through the same create_task() validation path as a
    hand-built task -- template output is just a regular TaskIn-shaped
    payload once filled in, nothing about verification_spec validation is
    bypassed or duplicated here."""
    templates = _load_task_templates()
    template = next((t for t in templates if t.get("id") == body.template_id), None)
    if template is None:
        raise HTTPException(
            status_code=404,
            detail=f"no template {body.template_id!r}; available: "
                   f"{sorted(t.get('id') for t in templates)}",
        )

    params = _coerce_params(template, body.params or {})
    prompt = _fill_template(template.get("prompt_template", ""), params)
    verification_spec = _fill_template_obj(
        template.get("verification_spec_template"), params
    )

    task_in = TaskIn(
        capability=template.get("capability", "general"),
        payload={"prompt": prompt},
        verification_spec=verification_spec,
    )
    return create_task(task_in)


# ---------------------------------------------------------------------------- agents
@router.get("/api/agents")
def list_agents():
    return db.list_agents()


@router.delete("/api/agents/{agent_id}")
def delete_agent(agent_id: str, x_worker_token: str | None = Header(default=None)):
    _check_worker_token(x_worker_token)
    if not db.delete_agent(agent_id):
        raise HTTPException(status_code=404, detail=f"no agent {agent_id}")
    return {"deleted": agent_id}


@router.post("/api/agents/register")
def register_agent(agent: AgentRegister):
    record = db.upsert_agent(agent.model_dump(mode="json"))
    db.add_event(f"agent:{agent.agent_id}", "registered",
                 agent.role_name or agent.agent_id, ref=f"agent:{agent.agent_id}")
    return record


# ------------------------------------------------------------------------- approvals
@router.get("/api/approvals")
def list_approvals():
    return db.list_approvals()


@router.post("/api/approvals/resolve")
def resolve_approval(body: ApprovalResolve):
    if body.decision not in ("approved", "denied"):
        raise HTTPException(status_code=400, detail="decision must be 'approved' or 'denied'")
    updated = db.resolve_approval(body.id, body.decision, decided_by=body.decided_by)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"no approval {body.id}")
    if body.decision == "approved" and updated.get("task_id"):
        task = db.get_task(updated["task_id"])
        if task and task.get("status") == "needs_approval":
            db.requeue_task(updated["task_id"])
    return {"ok": True, "approval": updated}


@router.post("/api/approvals", status_code=status.HTTP_201_CREATED, response_model=Approval)
def create_approval_route(body: ApprovalCreate):
    approval = db.create_approval(
        task_id=body.task_id,
        action=body.action,
        details=body.details or "",
    )
    notify.approval_pending(
        approval_id=approval["id"],
        task_id=approval.get("task_id"),
        action=approval["action"],
        details=approval.get("details", ""),
    )
    return approval


# ------------------------------------------------------- pull workers (HTTP claim/complete)
@router.post("/api/work/register")
def work_register(body: WorkerRegister,
                  x_worker_token: str | None = Header(default=None)):
    _check_worker_token(x_worker_token)
    agent = {
        "agent_id": body.agent_id,
        "role_name": body.role_name,
        "host_identity": body.host_identity,
        "ipv4": body.ipv4,
        "inference_endpoint": body.inference_endpoint,
        "inference_provider": body.inference_provider,
        "inference_models": body.inference_models,
        "capabilities": body.capabilities,
        "worker_type": "client-only",
        "status": "online",
    }
    record = db.upsert_agent(agent)
    db.add_event(f"agent:{body.agent_id}", "registered",
                 body.role_name or body.agent_id, ref=f"agent:{body.agent_id}")
    return record


@router.post("/api/work/claim")
def work_claim(body: WorkerClaim,
               x_worker_token: str | None = Header(default=None)):
    _check_worker_token(x_worker_token)
    if db.get_agent(body.agent_id) is not None:
        db.set_agent_heartbeat(body.agent_id, status="online")
        db.mark_agent_transport_pull(body.agent_id)
    task = db.claim_task(body.agent_id, body.capabilities)
    if task is not None and db.get_agent(body.agent_id) is not None:
        db.set_agent_heartbeat(body.agent_id, status="online", current_task=task["id"])
    return {"task": task}


@router.post("/api/work/complete")
def work_complete(body: WorkerComplete,
                  x_worker_token: str | None = Header(default=None)):
    _check_worker_token(x_worker_token)
    task = db.get_task(body.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"no task {body.task_id}")

    if body.error:
        db.fail_task(body.task_id, str(body.error))
        db.add_event(f"agent:{body.agent_id}", "error",
                     f"task {body.task_id} failed", task_id=body.task_id)
    else:
        result_text = body.result if body.result is not None else ""
        if task.get("verification_spec"):
            # Agent claims success, but this task requires independent proof.
            # Don't trust it yet â€” park the claimed result and let the sweep
            # loop (dispatch.py) run the actual check.
            db.set_task_verifying(body.task_id, result_text)
            db.add_event(f"agent:{body.agent_id}", "verifying",
                         f"task {body.task_id} claimed done, awaiting verification",
                         task_id=body.task_id)
        else:
            # No verification required for this task type â€” unchanged behavior.
            db.complete_task(body.task_id, result_text)
            db.add_event(f"agent:{body.agent_id}", "result",
                         f"task {body.task_id} done", task_id=body.task_id)

    if db.get_agent(body.agent_id) is not None:
        db.set_agent_heartbeat(body.agent_id, status="online", current_task=None)
    return db.get_task(body.task_id)


@router.post("/api/work/heartbeat")
def work_heartbeat(body: WorkerHeartbeat,
                   x_worker_token: str | None = Header(default=None)):
    _check_worker_token(x_worker_token)
    if db.get_agent(body.agent_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown agent {body.agent_id}; register first")
    db.set_agent_heartbeat(body.agent_id, status="online")
    return {"ok": True, "agent_id": body.agent_id}


# ------------------------------------------------------------------------------ wiki
@router.get("/api/wiki/tree")
def wiki_tree():
    base = Path(WIKI_DIR)
    if not base.exists():
        return {"base": str(base), "mounted": False, "pages": []}
    pages = []
    for p in sorted(base.rglob("*.md")):
        folder = p.parent.relative_to(base).as_posix()
        pages.append({
            "path": p.relative_to(base).as_posix(),
            "name": p.stem,
            "dir": "" if folder == "." else folder,
        })
    return {"base": str(base), "mounted": True, "pages": pages}


@router.get("/api/wiki/page")
def wiki_page(path: str):
    base = Path(WIKI_DIR).resolve()
    target = (base / path).resolve()
    if base not in target.parents or not target.is_file() or target.suffix.lower() != ".md":
        raise HTTPException(status_code=404, detail="no such wiki page")
    return {"path": path, "markdown": target.read_text(encoding="utf-8", errors="replace")}


# ---------------------------------------------------------------------------- models
@router.get("/api/models")
def list_models():
    """Proxy llama-swap's registered model aliases, plus our best-effort
    record of which one was last switched to via this dashboard/API."""
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(f"{LLAMA_SWAP_URL}/v1/models")
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"llama-swap unreachable: {exc}")

    models = [m.get("id") for m in data.get("data", []) if m.get("id")]
    last = _read_last_switch()
    return {
        "models": sorted(models),
        "last_switched": last,  # {"model": ..., "switched_at": ...} or None
    }


@router.post("/api/models/switch")
def switch_model(body: ModelSwitchRequest):
    """Validate the requested model against llama-swap, then switch Hermes's
    active model via its own /api/default-model endpoint (the same code path
    Hermes's own Settings panel uses -- see api/config.py:set_hermes_default_model
    in the hermes-webui repo)."""
    model = body.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")

    try:
        with httpx.Client(timeout=10.0) as client:
            models_resp = client.get(f"{LLAMA_SWAP_URL}/v1/models")
            models_resp.raise_for_status()
            available = {m.get("id") for m in models_resp.json().get("data", [])}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"llama-swap unreachable: {exc}")

    if model not in available:
        raise HTTPException(
            status_code=400,
            detail=f"'{model}' is not a registered llama-swap alias. Available: {sorted(available)}",
        )

    try:
        with httpx.Client(timeout=15.0) as client:
            hermes_resp = client.post(
                f"{HERMES_WEBUI_URL}/api/default-model",
                json={"model": model},
            )
            hermes_resp.raise_for_status()
            hermes_result = hermes_resp.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Hermes rejected the switch: {exc.response.text}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Hermes WebUI unreachable: {exc}")

    _write_last_switch(model)
    updated_agents = _update_agents_for_model_switch(model)
    return {"ok": True, "model": model, "hermes_response": hermes_result, "updated_agents": updated_agents}


@router.post("/api/agents/{agent_id}/model")
def set_agent_model(agent_id: str, body: ModelSwitchRequest):
    """Assign a model to a specific agent, from its own Control Room.

    Behavior depends on whether this agent actually shares the llama-swap
    backend with other agents:

    - If it does (inference_endpoint points at the same host as
      LLAMA_SWAP_URL): this triggers a REAL switch, identical to
      /api/models/switch -- because llama-swap only serves one model at a
      time, there's no such thing as changing the model for just this one
      agent while leaving its backend-mates on the old model. The response
      honestly reports every agent that got updated as a side effect
      (`updated_agents`), so the caller isn't misled into thinking this was
      an isolated, single-agent change when it wasn't.
    - If it doesn't (a different/unknown backend -- OpenRouter, a custom
      endpoint, another agent's own separate inference setup Helmward has
      no ability to actually switch): this only updates the stored label
      (`agents.inference_models`) for this one agent. It does NOT attempt
      to change what that agent is actually running -- Helmward has no
      generic way to do that for an arbitrary backend. The response makes
      this explicit via `shared_backend: false` and a `note` explaining
      the limitation, rather than silently pretending a real switch
      happened.
    """
    agent = db.get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"no agent {agent_id}")

    model = body.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")

    host = _llama_swap_host()
    endpoint = agent.get("inference_endpoint") or ""
    shares_llama_swap = bool(host) and host in endpoint

    if shares_llama_swap:
        # Delegate to the exact same logic /api/models/switch uses -- this
        # IS a real, shared-backend switch, so it should behave identically,
        # including updating every other agent on the same backend.
        result = switch_model(ModelSwitchRequest(model=model))
        result["shared_backend"] = True
        if len(result.get("updated_agents", [])) > 1:
            others = [a for a in result["updated_agents"] if a != agent_id]
            result["note"] = (
                f"This agent shares its inference backend with: "
                f"{', '.join(others)}. Switching the model here changed it "
                f"for all of them, not just {agent_id} -- llama-swap only "
                f"serves one active model at a time."
            )
        return result

    # Different/unknown backend -- label-only update, no live switch attempted.
    db.upsert_agent({"agent_id": agent_id, "inference_models": model})
    return {
        "ok": True,
        "model": model,
        "shared_backend": False,
        "updated_agents": [agent_id],
        "note": (
            "This agent's endpoint isn't the shared local inference backend "
            "Helmward knows how to switch live -- updated the stored label "
            "only. If this agent gets its model from its own separate "
            "system, change it there directly; this just updates what "
            "Helmward displays and records for it."
        ),
    }


@router.get("/api/models/reliability")
def models_reliability():
    """Aggregate verification pass/fail stats grouped by (model, verification
    type), from tasks that actually went through the verification gate.

    Model attribution comes from a snapshot taken at task-claim time
    (tasks.executed_model), not the agent's current config -- an agent's
    active model can change over time (e.g. swapping models on llama-swap),
    so reading current config here would misattribute historical tasks to
    whatever model happens to be active right now. Tasks that predate this
    snapshot mechanism, or whose agent had no model info at claim time,
    are grouped under "unknown" rather than silently dropped."""
    return {"stats": db.get_model_reliability_stats()}


# ----------------------------------------------------------------------------- state
@router.get("/api/state")
def get_state():
    return {
        "agents": db.list_agents(),
        "tasks": db.list_tasks(limit=100),
        "events": db.list_events(limit=50),
        "approvals": db.list_approvals(),
    }


@router.get("/api/mcp/graph_data")
def get_graph_data():
    import subprocess
    result = subprocess.run(
        ['python3', '/root/helmward-mcp/graph_data.py'],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        return json.loads(result.stdout)
    return {"error": result.stderr}


@router.get("/healthz")
def healthz():
    return {"ok": True}
