"""Versioned task records and lifecycle state definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping
import uuid


class TaskStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    RESUMING = "RESUMING"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ResumePolicy(str, Enum):
    """Policy for restoring a task after a safe, explicit interruption."""

    MANUAL = "MANUAL"
    AUTO_SAFE = "AUTO_SAFE"
    NEVER = "NEVER"


class PauseReason(str, Enum):
    USER = "USER"
    VAULT_LOCK = "VAULT_LOCK"
    SCHEDULER_SHUTDOWN = "SCHEDULER_SHUTDOWN"
    RESOURCE_MAINTENANCE = "RESOURCE_MAINTENANCE"


class ResumeIntent(str, Enum):
    MANUAL = "MANUAL"
    AFTER_VAULT_UNLOCK = "AFTER_VAULT_UNLOCK"


@dataclass(frozen=True)
class CheckpointValidation:
    """Worker verdict on whether a stored checkpoint is safe to consume."""

    valid: bool
    reason: str = ""

    @classmethod
    def accept(cls) -> "CheckpointValidation":
        return cls(True, "")

    @classmethod
    def reject(cls, reason: str) -> "CheckpointValidation":
        return cls(False, reason or "检查点未通过校验")


@dataclass(frozen=True)
class ReconciliationResult:
    """Result returned by a worker-specific side-effect reconciliation hook."""

    resolved: bool
    safe_to_resume: bool = False
    reason: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _copy_json_object(value: Mapping[str, Any] | None, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} 必须是 JSON 对象")
    return dict(value)


@dataclass(frozen=True)
class TaskRecord:
    """Durable task metadata. Business payloads belong in business tables."""

    task_type: str
    parameters: dict[str, Any] = field(default_factory=dict)
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    task_schema_version: int = 1
    checkpoint_schema_version: int = 1
    status: TaskStatus = TaskStatus.QUEUED
    checkpoint: Any = None
    progress: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    resume_policy: ResumePolicy = ResumePolicy.MANUAL
    pause_reason: PauseReason | None = None
    resume_intent: ResumeIntent = ResumeIntent.MANUAL
    attempt: int = 0
    owner_epoch: int = 0
    resource_keys: tuple[str, ...] = ()
    idempotency_key: str | None = None
    reconciliation_required: bool = False
    reconciliation_reason: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    revision: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.task_type, str) or not self.task_type.strip():
            raise ValueError("task_type 不能为空")
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("task_id 不能为空")
        if self.task_schema_version < 1 or self.checkpoint_schema_version < 1:
            raise ValueError("任务和检查点 Schema 版本必须为正整数")
        if self.attempt < 0 or self.owner_epoch < 0 or self.revision < 0:
            raise ValueError("attempt、owner_epoch 和 revision 不能为负数")
        object.__setattr__(self, "parameters", _copy_json_object(self.parameters, "parameters"))
        object.__setattr__(self, "progress", _copy_json_object(self.progress, "progress"))
        if self.error is not None:
            object.__setattr__(self, "error", _copy_json_object(self.error, "error"))
        if isinstance(self.status, str):
            object.__setattr__(self, "status", TaskStatus(self.status))
        if isinstance(self.resume_policy, str):
            object.__setattr__(self, "resume_policy", ResumePolicy(self.resume_policy))
        if self.pause_reason is not None and isinstance(self.pause_reason, str):
            object.__setattr__(self, "pause_reason", PauseReason(self.pause_reason))
        if isinstance(self.resume_intent, str):
            object.__setattr__(self, "resume_intent", ResumeIntent(self.resume_intent))
        keys = tuple(sorted({str(key).strip() for key in self.resource_keys if str(key).strip()}))
        object.__setattr__(self, "resource_keys", keys)
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            object.__setattr__(self, "idempotency_key", None)

    def to_public_dict(self) -> dict[str, Any]:
        """Return a JSON-ready view without changing the persisted model."""
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "task_schema_version": self.task_schema_version,
            "checkpoint_schema_version": self.checkpoint_schema_version,
            "status": self.status.value,
            "parameters": self.parameters,
            "checkpoint": self.checkpoint,
            "progress": self.progress,
            "error": self.error,
            "resume_policy": self.resume_policy.value,
            "pause_reason": self.pause_reason.value if self.pause_reason else None,
            "resume_intent": self.resume_intent.value,
            "attempt": self.attempt,
            "owner_epoch": self.owner_epoch,
            "resource_keys": list(self.resource_keys),
            "reconciliation_required": self.reconciliation_required,
            "reconciliation_reason": self.reconciliation_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "revision": self.revision,
        }
