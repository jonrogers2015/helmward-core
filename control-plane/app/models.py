"""Pydantic v2 models and enums for the agent-os control plane."""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    queued = "queued"
    assigned = "assigned"
    running = "running"
    verifying = "verifying"   # NEW — agent claimed done, independent check pending
    done = "done"
    failed = "failed"
    cancelled = "cancelled"
    needs_approval = "needs_approval"


class AgentStatus(str, Enum):
    online = "online"
    idle = "idle"
    busy = "busy"
    offline = "offline"


class ApprovalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    denied = "denied"


# ------------------------------------------------------------------- request models
class TaskIn(BaseModel):
    capability: str = Field(default="general", description="capability tag used for routing")
    payload: Any = Field(default_factory=dict, description="{prompt, context, ...}")
    max_attempts: int = 3
    parent_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    verification_spec: Optional[dict] = Field(
        default=None,
        description=(
            "Optional. If set, task is not marked done on the agent's word alone — "
            "the control plane independently checks this spec before finalizing status. "
            'e.g. {"type": "file_exists", "path": "/root/OUTPUT.md", "min_bytes": 100}'
        ),
    )


class AgentRegister(BaseModel):
    agent_id: str
    role_name: Optional[str] = None
    host_identity: Optional[str] = None          # where the runner RUNS
    ipv4: Optional[str] = None
    ipv6: Optional[str] = None
    inference_endpoint: Optional[str] = None      # where it SENDS inference (separate!)
    inference_provider: Optional[str] = None
    inference_models: Optional[str] = None
    listening_port: Optional[int] = None
    worker_type: Optional[str] = "client-only"
    capabilities: list[str] = Field(default_factory=lambda: ["general"])
    status: AgentStatus = AgentStatus.online
    concurrency_limit: int = 1


class ApprovalResolve(BaseModel):
    id: str
    decision: str  # "approved" | "denied"
    decided_by: str = "jon"


class ApprovalCreate(BaseModel):
    task_id: str
    action: str
    details: Optional[str] = None


# ----------------------------------------------- pull-worker (HTTP claim/complete) models
class WorkerRegister(BaseModel):
    agent_id: str
    role_name: Optional[str] = None
    host_identity: Optional[str] = None          # where the worker RUNS
    ipv4: Optional[str] = None
    inference_endpoint: Optional[str] = None      # where it SENDS inference (separate!)
    inference_provider: Optional[str] = None
    inference_models: Optional[str] = None
    capabilities: list[str] = Field(default_factory=lambda: ["general"])


class WorkerClaim(BaseModel):
    agent_id: str
    capabilities: list[str] = Field(default_factory=lambda: ["general"])


class WorkerComplete(BaseModel):
    agent_id: str
    task_id: str
    result: Optional[Any] = None
    error: Optional[str] = None


class WorkerHeartbeat(BaseModel):
    agent_id: str


# -------------------------------------------------------------------- output models
class Task(BaseModel):
    id: str
    capability: Optional[str] = None
    payload: Optional[str] = None
    status: Optional[str] = None
    assigned_agent: Optional[str] = None
    attempts: int = 0
    max_attempts: int = 3
    result: Optional[str] = None
    error: Optional[str] = None
    parent_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    verification_spec: Optional[str] = None       # NEW — JSON string
    verification_result: Optional[str] = None     # NEW — JSON string
    verified_at: Optional[str] = None              # NEW


class Agent(BaseModel):
    agent_id: str
    role_name: Optional[str] = None
    host_identity: Optional[str] = None
    ipv4: Optional[str] = None
    ipv6: Optional[str] = None
    inference_endpoint: Optional[str] = None
    inference_provider: Optional[str] = None
    inference_models: Optional[str] = None
    listening_port: Optional[int] = None
    worker_type: Optional[str] = None
    capabilities: Optional[str] = None
    status: Optional[str] = None
    last_heartbeat: Optional[str] = None
    current_task: Optional[str] = None
    concurrency_limit: int = 1


class Approval(BaseModel):
    id: str
    task_id: Optional[str] = None
    action: Optional[str] = None
    details: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[str] = None
    resolved_at: Optional[str] = None
    decided_by: Optional[str] = None


class Event(BaseModel):
    id: int
    ts: Optional[str] = None
    source: Optional[str] = None
    type: Optional[str] = None
    task_id: Optional[str] = None
    message: Optional[str] = None
    ref: Optional[str] = None