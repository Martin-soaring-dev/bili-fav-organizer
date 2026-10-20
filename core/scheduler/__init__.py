"""Persistent, single-process task scheduling primitives.

The package deliberately has no dependency on the HTTP layer or business
workers.  Applications inject a :class:`TaskStore` and register worker
implementations at composition time.
"""

from .models import (
    CheckpointValidation,
    PauseReason,
    ReconciliationResult,
    ResumeIntent,
    ResumePolicy,
    TaskRecord,
    TaskStatus,
)
from .scheduler import (
    AdmissionClosedError,
    CancelRequested,
    CheckpointRejectedError,
    InvalidTaskTransitionError,
    PauseRequested,
    ResourceBusyError,
    SchedulerError,
    SchedulerOwnershipError,
    TaskContext,
    TaskScheduler,
    TaskWorker,
)
from .store import (
    ConcurrentTaskUpdateError,
    CorruptTaskRecordError,
    DatabaseNotFoundError,
    InMemoryTaskStore,
    SQLiteTaskStore,
    TaskNotFoundError,
    TaskStore,
)

__all__ = [
    "AdmissionClosedError",
    "CancelRequested",
    "CheckpointRejectedError",
    "CheckpointValidation",
    "ConcurrentTaskUpdateError",
    "CorruptTaskRecordError",
    "DatabaseNotFoundError",
    "InMemoryTaskStore",
    "InvalidTaskTransitionError",
    "PauseReason",
    "PauseRequested",
    "ReconciliationResult",
    "ResourceBusyError",
    "ResumeIntent",
    "ResumePolicy",
    "SQLiteTaskStore",
    "SchedulerError",
    "SchedulerOwnershipError",
    "TaskContext",
    "TaskNotFoundError",
    "TaskRecord",
    "TaskScheduler",
    "TaskStatus",
    "TaskStore",
    "TaskWorker",
]
