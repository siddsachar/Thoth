from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


ApprovalMode = Literal["read_only", "ask", "auto_edit", "agent_run"]
TodoStatus = Literal["pending", "in_progress", "completed", "blocked"]
ExecutionMode = Literal["local", "docker"]
SandboxNetworkPolicy = Literal["off", "ask", "on"]


DEFAULT_APPROVAL_MODE: ApprovalMode = "ask"
DEFAULT_EXECUTION_MODE: ExecutionMode = "local"
DEFAULT_SANDBOX_NETWORK: SandboxNetworkPolicy = "off"
DEFAULT_SANDBOX_IMAGE = "nikolaik/python-nodejs:python3.11-nodejs20"


@dataclass
class DeveloperWorkspace:
    """Persisted user-owned repository/workspace entry."""

    id: str
    name: str
    path: str
    repo_url: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    default_thread_id: str = ""
    approval_mode: ApprovalMode = DEFAULT_APPROVAL_MODE
    execution_mode: ExecutionMode = DEFAULT_EXECUTION_MODE
    sandbox_network: SandboxNetworkPolicy = DEFAULT_SANDBOX_NETWORK
    sandbox_image: str = DEFAULT_SANDBOX_IMAGE
    sandbox_env_allowlist: list[str] = field(default_factory=list)
    trusted: bool = True
    hidden: bool = False

    def touch(self) -> None:
        self.updated_at = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "repo_url": self.repo_url,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "default_thread_id": self.default_thread_id,
            "approval_mode": self.approval_mode,
            "execution_mode": self.execution_mode,
            "sandbox_network": self.sandbox_network,
            "sandbox_image": self.sandbox_image,
            "sandbox_env_allowlist": list(self.sandbox_env_allowlist),
            "trusted": self.trusted,
            "hidden": self.hidden,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DeveloperWorkspace":
        allowed = set(cls.__dataclass_fields__)
        values = {k: v for k, v in data.items() if k in allowed}
        ws = cls(**values)
        if ws.approval_mode not in {"read_only", "ask", "auto_edit", "agent_run"}:
            ws.approval_mode = DEFAULT_APPROVAL_MODE
        if ws.execution_mode not in {"local", "docker"}:
            ws.execution_mode = DEFAULT_EXECUTION_MODE
        if ws.sandbox_network not in {"off", "ask", "on"}:
            ws.sandbox_network = DEFAULT_SANDBOX_NETWORK
        if not str(ws.sandbox_image or "").strip():
            ws.sandbox_image = DEFAULT_SANDBOX_IMAGE
        if not isinstance(ws.sandbox_env_allowlist, list):
            ws.sandbox_env_allowlist = []
        ws.sandbox_env_allowlist = [str(item) for item in ws.sandbox_env_allowlist if str(item or "").strip()]
        return ws


@dataclass
class DeveloperTodo:
    """Task-scoped todo item shown in the Developer Inspector."""

    id: str
    label: str
    status: TodoStatus = "pending"
    detail: str = ""
    reference: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "reference": self.reference,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DeveloperTodo":
        allowed = set(cls.__dataclass_fields__)
        values = {k: v for k, v in data.items() if k in allowed}
        todo = cls(**values)
        if todo.status not in {"pending", "in_progress", "completed", "blocked"}:
            todo.status = "pending"
        return todo
