"""Single-process task scheduler with durable lifecycle and checkpoints."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, runtime_checkable
import logging
import threading
import time
import uuid

from .models import (
    CheckpointValidation,
    PauseReason,
    ReconciliationResult,
    ResumeIntent,
    ResumePolicy,
    TaskRecord,
    TaskStatus,
    utc_now,
)
from .ownership import FileOwnerLock, OwnerLock, ProcessOwnerLock
from .store import (
    ConcurrentTaskUpdateError,
    InMemoryTaskStore,
    SQLiteTaskStore,
    TaskNotFoundError,
    TaskStore,
)

logger = logging.getLogger(__name__)


class SchedulerError(RuntimeError):
    """Base class for scheduler API errors."""


class SchedulerOwnershipError(SchedulerError):
    """Another process or scheduler instance owns task execution."""


class InvalidTaskTransitionError(SchedulerError):
    """A task transition is not part of the lifecycle contract."""


class AdmissionClosedError(SchedulerError):
    """The scheduler is not accepting new tasks while a maintenance barrier is active."""


class TaskWorkerNotFoundError(SchedulerError):
    """No worker has been registered for the requested task type."""


class TaskControlRequested(Exception):
    """Base exception a worker can raise at a safe checkpoint boundary."""


class PauseRequested(TaskControlRequested):
    """The worker should persist its checkpoint and exit as paused."""


class CancelRequested(TaskControlRequested):
    """The worker should stop and never be resumed automatically."""


class CheckpointRejectedError(SchedulerError):
    """A worker rejected the stored checkpoint as unsafe or incompatible."""


class ResourceBusyError(SchedulerError):
    """A reconciliation call cannot safely acquire the task's resource keys."""


@runtime_checkable
class TaskWorker(Protocol):
    """Worker contract.

    ``execute`` is required.  ``validate``, ``validate_checkpoint`` and
    ``reconcile`` are optional hooks, discovered at runtime.  A resumed worker
    must implement ``validate_checkpoint`` or explicitly set
    ``can_restart_without_checkpoint = True``.
    """

    def execute(self, context: "TaskContext") -> Any: ...


@dataclass
class _RunHandle:
    task_id: str
    pause_event: threading.Event
    cancel_event: threading.Event
    record: TaskRecord
    thread: threading.Thread | None = None
    reconciliation_in_progress: bool = False
    pending_finish: tuple[TaskStatus, dict[str, Any] | None] | None = None


class TaskContext:
    """Per-run context; workers report durable progress through this object."""

    def __init__(self, scheduler: "TaskScheduler", handle: _RunHandle) -> None:
        self._scheduler = scheduler
        self._handle = handle

    @property
    def task(self) -> TaskRecord:
        return self._scheduler.get_task(self._handle.task_id)

    @property
    def pause_requested(self) -> bool:
        return self._handle.pause_event.is_set()

    @property
    def cancel_requested(self) -> bool:
        return self._handle.cancel_event.is_set()

    def save_checkpoint(self, checkpoint: Any, *, progress: Mapping[str, Any] | None = None) -> TaskRecord:
        """Persist checkpoint and visible progress in one task-row update."""
        return self._scheduler._save_checkpoint(
            self._handle, checkpoint, progress=progress
        )

    def report_progress(self, progress: Mapping[str, Any]) -> TaskRecord:
        """Persist UI progress without replacing the previous checkpoint."""
        return self._scheduler._save_progress(self._handle, progress)

    def safe_point(
        self, checkpoint: Any, *, progress: Mapping[str, Any] | None = None
    ) -> TaskRecord:
        """Commit a safe continuation point, then honor pause/cancel signals."""
        record = self.save_checkpoint(checkpoint, progress=progress)
        self.raise_if_control_requested()
        return record

    def mark_reconciliation_required(self, reason: str) -> TaskRecord:
        """Persist uncertainty before/while a worker performs an external write."""
        return self._scheduler._mark_reconciliation_required(self._handle, reason)

    def clear_reconciliation_requirement(self) -> TaskRecord:
        """Clear task-level uncertainty after a worker has verified the outcome."""
        return self._scheduler._clear_reconciliation_requirement(self._handle)

    def raise_if_control_requested(self) -> None:
        """Call at safe boundaries; cancellation takes precedence over pausing."""
        if self._handle.cancel_event.is_set():
            raise CancelRequested("任务已请求取消")
        if self._handle.pause_event.is_set():
            raise PauseRequested("任务已请求暂停")
        if self._handle.record.reconciliation_required:
            raise PauseRequested("存在未核对的外部操作，任务必须停在安全边界")

    def wait_for_control(self, timeout: float) -> bool:
        """Wait for pause/cancel and return whether either was requested."""
        if (self._handle.cancel_event.is_set() or self._handle.pause_event.is_set()
                or self._handle.record.reconciliation_required):
            return True
        return (self._handle.pause_event.wait(timeout)
                or self._handle.cancel_event.is_set()
                or self._handle.record.reconciliation_required)


class TaskScheduler:
    """Durable task lifecycle coordinator.

    The first version is deliberately single-owner and defaults to one worker
    at a time.  Stores and workers are injected so the core can be tested
    independently of the HTTP and business modules.
    """

    _TRANSITIONS = {
        TaskStatus.QUEUED: {TaskStatus.RUNNING, TaskStatus.PAUSED, TaskStatus.CANCELLED},
        TaskStatus.RUNNING: {
            TaskStatus.PAUSING, TaskStatus.CANCELLING, TaskStatus.COMPLETED,
            TaskStatus.INTERRUPTED, TaskStatus.FAILED,
            TaskStatus.NEEDS_RECONCILIATION,
        },
        TaskStatus.PAUSING: {
            TaskStatus.PAUSED, TaskStatus.CANCELLING, TaskStatus.COMPLETED,
            TaskStatus.INTERRUPTED, TaskStatus.FAILED,
            TaskStatus.NEEDS_RECONCILIATION,
        },
        TaskStatus.PAUSED: {TaskStatus.RESUMING, TaskStatus.INTERRUPTED, TaskStatus.CANCELLED},
        TaskStatus.RESUMING: {
            TaskStatus.RUNNING, TaskStatus.PAUSING, TaskStatus.CANCELLING,
            TaskStatus.INTERRUPTED, TaskStatus.NEEDS_RECONCILIATION,
            TaskStatus.FAILED, TaskStatus.CANCELLED,
        },
        TaskStatus.CANCELLING: {
            TaskStatus.CANCELLED, TaskStatus.NEEDS_RECONCILIATION, TaskStatus.FAILED,
        },
        TaskStatus.INTERRUPTED: {
            TaskStatus.RESUMING, TaskStatus.NEEDS_RECONCILIATION,
            TaskStatus.CANCELLED, TaskStatus.FAILED,
        },
        TaskStatus.NEEDS_RECONCILIATION: {
            TaskStatus.INTERRUPTED, TaskStatus.RESUMING,
            TaskStatus.CANCELLED, TaskStatus.FAILED,
        },
        TaskStatus.COMPLETED: set(),
        TaskStatus.CANCELLED: set(),
        TaskStatus.FAILED: set(),
    }

    def __init__(
        self,
        store: TaskStore,
        *,
        workers: Mapping[str, TaskWorker | Callable[[], TaskWorker]] | None = None,
        max_workers: int = 1,
        owner_lock: OwnerLock | None = None,
        owner_id: str | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers 必须至少为 1")
        self.store = store
        self.max_workers = max_workers
        self.owner_id = owner_id or uuid.uuid4().hex
        self._workers: dict[str, Any] = dict(workers or {})
        self._owner_lock = owner_lock or self._default_owner_lock(store)
        self._owner_epoch = 0
        self._started = False
        self._closing = False
        self._admission_open = True
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._active: dict[str, _RunHandle] = {}
        self._reserved_resources: set[str] = set()
        self._listeners: set[Callable[[TaskRecord], None]] = set()
        self._pump_thread: threading.Thread | None = None
        self._last_error: str | None = None

    @staticmethod
    def _default_owner_lock(store: TaskStore) -> OwnerLock:
        if isinstance(store, SQLiteTaskStore):
            if store.owner_lock_path is not None:
                return FileOwnerLock(store.owner_lock_path)
        return ProcessOwnerLock((type(store).__name__, id(store)))

    @property
    def owner_epoch(self) -> int:
        return self._owner_epoch

    @property
    def admission_open(self) -> bool:
        with self._lock:
            return self._admission_open

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def register_worker(self, task_type: str, worker: TaskWorker | Callable[[], TaskWorker]) -> None:
        task_type = str(task_type).strip()
        if not task_type:
            raise ValueError("task_type 不能为空")
        with self._lock:
            if self._started and any(
                record.task_type == task_type and record.status in {
                    TaskStatus.RUNNING, TaskStatus.PAUSING,
                    TaskStatus.RESUMING, TaskStatus.CANCELLING,
                }
                for record in self.store.list()
            ):
                raise SchedulerError("该任务类型仍有工作器运行，不能替换注册")
            self._workers[task_type] = worker
            self._condition.notify_all()

    def subscribe(self, listener: Callable[[TaskRecord], None]) -> Callable[[], None]:
        """Subscribe to persisted state changes; return an unsubscribe function."""
        with self._lock:
            self._listeners.add(listener)

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.discard(listener)

        return unsubscribe

    def start(self) -> None:
        """Claim process ownership, mark stale work interrupted, then dispatch."""
        with self._lock:
            if self._started:
                return
        self.store.ensure_schema()
        try:
            self._owner_lock.acquire()
        except Exception as exc:
            raise SchedulerOwnershipError(str(exc)) from exc
        try:
            epoch = self.store.claim_owner_epoch(self.owner_id)
            with self._lock:
                self._owner_epoch = epoch
            self._recover_incomplete_tasks()
            with self._lock:
                self._closing = False
                self._started = True
                self._pump_thread = threading.Thread(
                    target=self._pump, name="bfo-task-scheduler", daemon=True
                )
                self._pump_thread.start()
                self._condition.notify_all()
        except Exception:
            self._owner_lock.release()
            raise

    def submit(
        self,
        task_type: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        task_schema_version: int = 1,
        checkpoint_schema_version: int = 1,
        resume_policy: ResumePolicy = ResumePolicy.MANUAL,
        resource_keys: tuple[str, ...] | list[str] | set[str] = (),
        idempotency_key: str | None = None,
        task_id: str | None = None,
    ) -> TaskRecord:
        """Create a persistent QUEUED task or return its idempotent prior row."""
        self.store.ensure_schema()
        with self._lock:
            if not self._admission_open:
                raise AdmissionClosedError("调度器已关闭新任务准入")
            if task_type not in self._workers:
                raise TaskWorkerNotFoundError(task_type)
        record = TaskRecord(
            task_type=task_type,
            parameters=dict(parameters or {}),
            task_id=task_id or uuid.uuid4().hex,
            task_schema_version=task_schema_version,
            checkpoint_schema_version=checkpoint_schema_version,
            resume_policy=resume_policy,
            resource_keys=tuple(resource_keys),
            idempotency_key=idempotency_key,
        )
        saved, created = self.store.create(record)
        if created:
            self._emit(saved)
        with self._condition:
            self._condition.notify_all()
        return saved

    create_task = submit

    def get_task(self, task_id: str) -> TaskRecord:
        return self.store.get(task_id)

    def list_tasks(self, statuses: set[TaskStatus] | None = None) -> list[TaskRecord]:
        return self.store.list(statuses)

    def pause_task(
        self,
        task_id: str,
        *,
        reason: PauseReason = PauseReason.USER,
        resume_intent: ResumeIntent | None = None,
    ) -> TaskRecord:
        if isinstance(reason, str):
            reason = PauseReason(reason)
        current = self.store.get(task_id)
        intent = resume_intent or (
            ResumeIntent.AFTER_VAULT_UNLOCK
            if reason == PauseReason.VAULT_LOCK and current.resume_policy == ResumePolicy.AUTO_SAFE
            else ResumeIntent.MANUAL
        )
        if isinstance(intent, str):
            intent = ResumeIntent(intent)
        if current.status == TaskStatus.QUEUED:
            record = self._change(
                task_id,
                expected={TaskStatus.QUEUED},
                status=TaskStatus.PAUSED,
                pause_reason=reason,
                resume_intent=intent,
            )
        elif current.status in {TaskStatus.RUNNING, TaskStatus.RESUMING}:
            record = self._change(
                task_id,
                expected={current.status},
                status=TaskStatus.PAUSING,
                pause_reason=reason,
                resume_intent=intent,
            )
            handle = self._active.get(task_id)
            if handle:
                handle.pause_event.set()
        elif current.status == TaskStatus.PAUSING:
            record = self._change(
                task_id,
                expected={TaskStatus.PAUSING},
                pause_reason=reason,
                resume_intent=intent,
            )
            handle = self._active.get(task_id)
            if handle:
                handle.pause_event.set()
        elif current.status in {TaskStatus.PAUSED, TaskStatus.CANCELLED,
                                TaskStatus.COMPLETED, TaskStatus.FAILED}:
            return current
        else:
            raise InvalidTaskTransitionError(
                f"不能从 {current.status.value} 请求暂停"
            )
        return record

    pause = pause_task

    def cancel_task(self, task_id: str) -> TaskRecord:
        current = self.store.get(task_id)
        if current.status in {TaskStatus.QUEUED, TaskStatus.PAUSED, TaskStatus.INTERRUPTED}:
            if current.reconciliation_required:
                target = TaskStatus.NEEDS_RECONCILIATION
            else:
                target = TaskStatus.CANCELLED
            if current.status == target:
                return current
            record = self._change(
                task_id,
                expected={current.status},
                status=target,
                error=(current.error if target == TaskStatus.NEEDS_RECONCILIATION else None),
                # A cancelled task is not paused any more; the reason must not
                # survive on the record or the UI keeps showing "用户暂停".
                pause_reason=None,
                resume_intent=ResumeIntent.MANUAL,
            )
        elif current.status in {TaskStatus.RUNNING, TaskStatus.PAUSING, TaskStatus.RESUMING}:
            record = self._change(
                task_id,
                expected={current.status},
                status=TaskStatus.CANCELLING,
                pause_reason=None,
                resume_intent=ResumeIntent.MANUAL,
            )
            handle = self._active.get(task_id)
            if handle:
                handle.cancel_event.set()
                handle.pause_event.set()
        elif current.status in {TaskStatus.CANCELLING, TaskStatus.CANCELLED,
                                TaskStatus.COMPLETED, TaskStatus.FAILED,
                                TaskStatus.NEEDS_RECONCILIATION}:
            return current
        else:
            raise InvalidTaskTransitionError(
                f"不能从 {current.status.value} 请求取消"
            )
        return record

    cancel = cancel_task

    def resume_task(self, task_id: str) -> TaskRecord:
        with self._condition:
            if not self._admission_open:
                raise AdmissionClosedError("调度器正在执行维护屏障，暂不能恢复任务")
        current = self.store.get(task_id)
        if current.status == TaskStatus.NEEDS_RECONCILIATION:
            result = self.reconcile_task(task_id)
            if not result.resolved or not result.safe_to_resume:
                return self.store.get(task_id)
            current = self.store.get(task_id)
        with self._condition:
            # Serialize the final state change with close_admission(). A
            # resume that began just before a vault barrier must not dispatch
            # a worker after the barrier closed admission.
            if not self._admission_open:
                raise AdmissionClosedError("调度器正在执行维护屏障，暂不能恢复任务")
            current = self.store.get(task_id)
            if current.status not in {TaskStatus.PAUSED, TaskStatus.INTERRUPTED}:
                raise InvalidTaskTransitionError(
                    f"不能从 {current.status.value} 恢复任务"
                )
            if current.reconciliation_required:
                raise InvalidTaskTransitionError("任务仍有未核对副作用，不能恢复")
            if current.resume_policy == ResumePolicy.NEVER:
                raise InvalidTaskTransitionError("该任务策略禁止恢复")
            record = self._change(
                task_id,
                expected={current.status},
                status=TaskStatus.RESUMING,
                error=None,
                pause_reason=None,
                resume_intent=ResumeIntent.MANUAL,
            )
            self._condition.notify_all()
        return record

    resume = resume_task

    def close_admission(
        self,
        *,
        reason: PauseReason = PauseReason.VAULT_LOCK,
        pause_running: bool = False,
    ) -> list[TaskRecord]:
        """Close new task admission before requesting a pause barrier."""
        with self._condition:
            self._admission_open = False
            self._condition.notify_all()
            active_ids = list(self._active)
        if not pause_running:
            return []
        changed = []
        for task_id in active_ids:
            try:
                changed.append(self.pause_task(task_id, reason=reason))
            except InvalidTaskTransitionError:
                continue
        return changed

    def request_pause_all(
        self,
        *,
        reason: PauseReason = PauseReason.VAULT_LOCK,
        resume_intent: ResumeIntent | None = None,
    ) -> list[TaskRecord]:
        """Request pause for every queued or active task.

        A returned ``PAUSING`` record is not a completed pause. Call
        :meth:`wait_for_idle` and inspect persisted statuses before sealing the
        database. Active workers only become ``PAUSED`` after they return from
        their safe boundary and release their resources.
        """
        if isinstance(reason, str):
            reason = PauseReason(reason)
        if isinstance(resume_intent, str):
            resume_intent = ResumeIntent(resume_intent)
        candidates = self.store.list({
            TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.RESUMING,
            TaskStatus.PAUSING,
        })
        paused = []
        for record in candidates:
            try:
                paused.append(self.pause_task(
                    record.task_id,
                    reason=reason,
                    resume_intent=resume_intent,
                ))
            except InvalidTaskTransitionError:
                # The worker may have completed between the list and request.
                continue
        return paused

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        """Wait until every worker has exited and active states are durable."""
        deadline = None if timeout is None else time.monotonic() + timeout
        active_states = {
            TaskStatus.RUNNING, TaskStatus.PAUSING, TaskStatus.RESUMING,
            TaskStatus.CANCELLING,
        }
        while True:
            with self._condition:
                no_workers = not self._active
                if no_workers:
                    try:
                        no_active_records = not self.store.list(active_states)
                    except Exception as exc:
                        self._last_error = f"检查任务屏障失败：{type(exc).__name__}: {exc}"
                        return False
                    if no_active_records:
                        return True
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(0.1 if remaining is None else min(remaining, 0.1))

    def open_admission(self) -> None:
        with self._condition:
            self._admission_open = True
            self._condition.notify_all()

    reopen_admission = open_admission

    def resume_after_vault_unlock(self) -> list[TaskRecord]:
        """Resume only tasks whose persisted intent came from a vault lock."""
        if not self.admission_open:
            raise AdmissionClosedError("必须先开放调度准入，再恢复保险库暂停的任务")
        candidates = [
            record for record in self.store.list()
            if record.status in {TaskStatus.PAUSED, TaskStatus.INTERRUPTED}
            and record.pause_reason == PauseReason.VAULT_LOCK
            and record.resume_intent == ResumeIntent.AFTER_VAULT_UNLOCK
            and record.resume_policy == ResumePolicy.AUTO_SAFE
            and not record.reconciliation_required
        ]
        resumed = []
        for record in candidates:
            try:
                resumed.append(self.resume_task(record.task_id))
            except InvalidTaskTransitionError:
                continue
        return resumed

    def resume_safe_interrupted(self) -> list[TaskRecord]:
        """Request worker validation for crash-interrupted AUTO_SAFE tasks."""
        if not self.admission_open:
            raise AdmissionClosedError("调度准入关闭时不能恢复任务")
        candidates = [
            record for record in self.store.list({TaskStatus.INTERRUPTED})
            if record.resume_policy == ResumePolicy.AUTO_SAFE
            and not record.reconciliation_required
            and record.resume_intent == ResumeIntent.MANUAL
        ]
        resumed = []
        for record in candidates:
            try:
                resumed.append(self.resume_task(record.task_id))
            except InvalidTaskTransitionError:
                continue
        return resumed

    def reconcile_task(self, task_id: str) -> ReconciliationResult:
        """Run an injected read/verification hook without resending a write."""
        current = self.store.get(task_id)
        if current.status != TaskStatus.NEEDS_RECONCILIATION:
            raise InvalidTaskTransitionError("只有待核对任务可以执行 reconcile")
        worker = self._make_worker(current.task_type)
        reconcile = getattr(worker, "reconcile", None)
        if not callable(reconcile):
            return ReconciliationResult(False, False, "该任务类型未注册核对器")
        keys = set(current.resource_keys)
        with self._condition:
            if not self._admission_open:
                raise AdmissionClosedError("调度器正在执行维护屏障，暂不能核对任务")
            if self._reserved_resources.intersection(keys):
                raise ResourceBusyError("任务所需资源正在被其它任务占用")
            self._reserved_resources.update(keys)
            handle = _RunHandle(
                task_id=task_id,
                pause_event=threading.Event(),
                cancel_event=threading.Event(),
                record=current,
                reconciliation_in_progress=True,
            )
            self._active[task_id] = handle
        try:
            raw = reconcile(TaskContext(self, handle))
            if isinstance(raw, ReconciliationResult):
                result = raw
            elif isinstance(raw, bool):
                result = ReconciliationResult(raw, raw, "" if raw else "核对未能确认结果")
            else:
                raise TypeError("reconcile 必须返回 ReconciliationResult 或 bool")
            if result.resolved and result.safe_to_resume:
                self._change(
                    task_id,
                    expected={TaskStatus.NEEDS_RECONCILIATION},
                    status=TaskStatus.INTERRUPTED,
                    reconciliation_required=False,
                    reconciliation_reason="",
                    error=None,
                )
            elif result.reason:
                self._change(
                    task_id,
                    expected={TaskStatus.NEEDS_RECONCILIATION},
                    reconciliation_reason=result.reason,
                    error={"category": "needs_reconciliation", "message": result.reason},
                )
            return result
        except Exception as exc:
            reason = f"核对失败：{type(exc).__name__}: {str(exc)[:800]}"
            self._change(
                task_id,
                expected={TaskStatus.NEEDS_RECONCILIATION},
                reconciliation_reason=reason,
                error={"category": "reconciliation_error", "type": type(exc).__name__,
                       "message": str(exc)[:800]},
            )
            return ReconciliationResult(False, False, reason)
        finally:
            with self._condition:
                self._active.pop(task_id, None)
                self._reserved_resources.difference_update(keys)
                self._condition.notify_all()

    def wait_for_status(
        self, task_id: str, statuses: set[TaskStatus], *, timeout: float = 5.0
    ) -> TaskRecord:
        deadline = time.monotonic() + timeout
        while True:
            record = self.store.get(task_id)
            if record.status in statuses:
                return record
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return record
            with self._condition:
                self._condition.wait(min(remaining, 0.1))

    def shutdown(
        self,
        *,
        wait: bool = True,
        timeout: float | None = None,
        pause_running: bool = False,
    ) -> None:
        """Stop dispatch and release ownership only after workers have exited."""
        if not self._started:
            return
        self.close_admission(
            reason=PauseReason.SCHEDULER_SHUTDOWN,
            pause_running=pause_running,
        )
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            self._closing = True
            self._condition.notify_all()
            while wait and self._active:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    # Keep the owner lock while a worker might still write.
                    raise TimeoutError("工作器尚未退出；保留调度器所有权")
                self._condition.wait(remaining)
            if self._active:
                # Non-waiting shutdown is not allowed to release the owner lock.
                raise SchedulerError("仍有工作器运行，不能释放调度器所有权")
            pump = self._pump_thread
            self._started = False
            self._pump_thread = None
        if pump and pump is not threading.current_thread():
            pump.join(timeout=2.0)
        self._owner_lock.release()

    close = shutdown

    def _recover_incomplete_tasks(self) -> None:
        active_states = {
            TaskStatus.RUNNING, TaskStatus.PAUSING, TaskStatus.RESUMING,
            TaskStatus.CANCELLING,
        }
        for record in self.store.list(active_states | {TaskStatus.PAUSED}):
            if record.status == TaskStatus.PAUSED:
                # App workers (including user-paused and vault-paused workers)
                # are process-local closures. A PAUSED row from an older owner
                # therefore cannot actually resume after restart; retain its
                # checkpoint and mark it for business-layer recovery.
                if record.owner_epoch >= self._owner_epoch:
                    continue
                self._change(
                    record.task_id,
                    expected={TaskStatus.PAUSED},
                    status=TaskStatus.INTERRUPTED,
                    error={
                        "category": "paused_task_after_restart",
                        "message": "程序重启后原任务工作器已丢失；请从业务页面按检查点继续",
                    },
                )
                continue
            if record.reconciliation_required:
                status = TaskStatus.NEEDS_RECONCILIATION
                category = "needs_reconciliation"
                message = record.reconciliation_reason or "进程退出时存在未确认的外部操作"
            elif record.status == TaskStatus.CANCELLING:
                status = TaskStatus.CANCELLED
                category = "cancelled_after_restart"
                message = "程序重启时发现取消请求已提交；任务不会自动恢复"
            else:
                status = TaskStatus.INTERRUPTED
                category = "process_interrupted"
                message = "程序在任务运行期间退出；恢复前必须校验检查点"
            self._change(
                record.task_id,
                expected={record.status},
                status=status,
                error={"category": category, "message": message},
            )

    def _pump(self) -> None:
        while True:
            with self._condition:
                if self._closing:
                    return
                if self._admission_open:
                    self._dispatch_available_locked()
                # Wait while holding the condition lock.  This pairs atomically
                # with submit()/worker-finally notifications and avoids both
                # RuntimeError (wait without lock) and lost wakeups.
                self._condition.wait()

    def _dispatch_available_locked(self) -> None:
        available = self.max_workers - len(self._active)
        if available <= 0:
            return
        try:
            queued = self.store.list({TaskStatus.QUEUED, TaskStatus.RESUMING})
        except Exception as exc:
            self._last_error = f"读取调度队列失败：{type(exc).__name__}: {exc}"
            logger.exception("Unable to read scheduler queue")
            return
        for record in queued:
            if available <= 0:
                break
            if record.task_id in self._active:
                continue
            if record.task_type not in self._workers:
                continue
            keys = set(record.resource_keys)
            if self._reserved_resources.intersection(keys):
                continue
            self._reserved_resources.update(keys)
            handle = _RunHandle(
                task_id=record.task_id,
                pause_event=threading.Event(),
                cancel_event=threading.Event(),
                record=record,
            )
            thread = threading.Thread(
                target=self._run_task,
                args=(handle,),
                name=f"bfo-task-{record.task_type}-{record.task_id[:8]}",
                daemon=True,
            )
            handle.thread = thread
            self._active[record.task_id] = handle
            thread.start()
            available -= 1

    def _run_task(self, handle: _RunHandle) -> None:
        task_id = handle.task_id
        context = TaskContext(self, handle)
        try:
            record = self.store.get(task_id)
            if record.status not in {TaskStatus.QUEUED, TaskStatus.RESUMING}:
                if record.status == TaskStatus.PAUSING:
                    self._finish(handle, TaskStatus.PAUSED, error=None)
                elif record.status == TaskStatus.CANCELLING:
                    target = (TaskStatus.NEEDS_RECONCILIATION
                              if record.reconciliation_required else TaskStatus.CANCELLED)
                    error = ({"category": "needs_reconciliation",
                              "message": record.reconciliation_reason or
                              "取消时存在未确认的外部操作"}
                             if target == TaskStatus.NEEDS_RECONCILIATION else None)
                    self._finish(handle, target, error=error)
                return
            worker = self._make_worker(record.task_type)
            self._call_validate(worker, context)
            if record.status == TaskStatus.RESUMING:
                self._validate_checkpoint(worker, context)
            with self._condition:
                current = self.store.get(task_id)
                if current.status not in {TaskStatus.QUEUED, TaskStatus.RESUMING}:
                    return
                record = self._change(
                    task_id,
                    expected={current.status},
                    status=TaskStatus.RUNNING,
                    attempt=current.attempt + 1,
                    owner_epoch=self._owner_epoch,
                    error=None,
                    pause_reason=None,
                    emit=False,
                )
                handle.record = record
                self._condition.notify_all()
            self._emit(record)
            context.raise_if_control_requested()
            execute = getattr(worker, "execute", None)
            if callable(execute):
                execute(context)
            elif callable(worker):
                worker(context)
            else:
                raise TypeError("worker 必须实现 execute(context)")
            current = self.store.get(task_id)
            if current.reconciliation_required:
                target = TaskStatus.NEEDS_RECONCILIATION
                error = {
                    "category": "needs_reconciliation",
                    "message": current.reconciliation_reason or "外部操作结果尚未确认",
                }
            elif handle.cancel_event.is_set() or current.status == TaskStatus.CANCELLING:
                target = TaskStatus.CANCELLED
                error = None
            else:
                target = TaskStatus.COMPLETED
                error = None
            self._finish(handle, target, error=error)
        except PauseRequested as exc:
            current = self.store.get(task_id)
            if current.reconciliation_required:
                self._finish(handle, TaskStatus.NEEDS_RECONCILIATION, error={
                    "category": "needs_reconciliation",
                    "message": current.reconciliation_reason or str(exc),
                })
            elif handle.cancel_event.is_set() or current.status == TaskStatus.CANCELLING:
                self._finish(handle, TaskStatus.CANCELLED, error=None)
            else:
                if current.status == TaskStatus.RUNNING:
                    self._change(task_id, expected={TaskStatus.RUNNING},
                                 status=TaskStatus.PAUSING)
                self._finish(handle, TaskStatus.PAUSED, error=None)
        except CancelRequested:
            current = self.store.get(task_id)
            target = (TaskStatus.NEEDS_RECONCILIATION if current.reconciliation_required
                      else TaskStatus.CANCELLED)
            error = ({"category": "needs_reconciliation",
                      "message": current.reconciliation_reason or "取消时存在未确认的外部操作"}
                     if target == TaskStatus.NEEDS_RECONCILIATION else None)
            self._finish(handle, target, error=error)
        except CheckpointRejectedError as exc:
            self._finish(handle, TaskStatus.FAILED, error={
                "category": "checkpoint_invalid", "message": str(exc)[:1000]
            })
        except Exception as exc:
            logger.exception("Task %s failed", task_id)
            current = self.store.get(task_id)
            if current.reconciliation_required:
                target = TaskStatus.NEEDS_RECONCILIATION
                error = {"category": "needs_reconciliation",
                         "message": current.reconciliation_reason or str(exc)[:1000]}
            elif handle.cancel_event.is_set() or current.status == TaskStatus.CANCELLING:
                target = TaskStatus.CANCELLED
                error = None
            else:
                target = TaskStatus.FAILED
                error = {"category": "worker_error", "type": type(exc).__name__,
                         "message": str(exc)[:1000]}
            self._finish(handle, target, error=error)
        finally:
            keys = set(handle.record.resource_keys)
            emitted = None
            with self._condition:
                self._active.pop(task_id, None)
                self._reserved_resources.difference_update(keys)
                if handle.pending_finish is not None:
                    target, error = handle.pending_finish
                    current = self.store.get(task_id)
                    if current.reconciliation_required:
                        target = TaskStatus.NEEDS_RECONCILIATION
                        error = {
                            "category": "needs_reconciliation",
                            "message": current.reconciliation_reason or
                            "任务退出时仍有未确认的外部操作",
                        }
                    elif current.status == TaskStatus.CANCELLING:
                        target = TaskStatus.CANCELLED
                        error = None
                    if current.status != target:
                        if target not in self._TRANSITIONS[current.status]:
                            self._last_error = (
                                f"任务 {task_id} 退出时无法完成状态迁移："
                                f"{current.status.value} -> {target.value}"
                            )
                        else:
                            updated = replace(
                                current,
                                status=target,
                                error=error,
                                updated_at=utc_now(),
                                resume_intent=(current.resume_intent
                                               if target == TaskStatus.PAUSED
                                               else ResumeIntent.MANUAL),
                            )
                            emitted = self.store.update(updated, current.revision)
                            handle.record = emitted
                self._condition.notify_all()
            if emitted is not None:
                self._emit(emitted)

    def _finish(
        self,
        handle: _RunHandle,
        target: TaskStatus,
        *,
        error: dict[str, Any] | None,
    ) -> TaskRecord:
        current = self.store.get(handle.task_id)
        if current.status == target:
            return current
        if current.status in {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}:
            return current
        if target == TaskStatus.PAUSED:
            if current.status == TaskStatus.RUNNING:
                current = self._change(
                    handle.task_id,
                    expected={TaskStatus.RUNNING},
                    status=TaskStatus.PAUSING,
                )
            if current.status != TaskStatus.PAUSING:
                raise InvalidTaskTransitionError(
                    f"工作器退出时不能从 {current.status.value} 进入 PAUSED"
                )
            handle.pending_finish = (target, error)
            return current
        record = self._change(
            handle.task_id,
            expected={current.status},
            status=target,
            error=error,
            # Only PAUSED keeps a reason; PAUSED is handled by the pending-finish
            # branch above, so every target that reaches here is a state where a
            # stale "为什么暂停" would be wrong.
            pause_reason=None,
            resume_intent=(current.resume_intent if target == TaskStatus.PAUSED
                           else ResumeIntent.MANUAL),
        )
        handle.record = record
        return record

    def _make_worker(self, task_type: str) -> TaskWorker:
        registered = self._workers.get(task_type)
        if registered is None:
            raise TaskWorkerNotFoundError(task_type)
        if isinstance(registered, type):
            return registered()
        if not callable(getattr(registered, "execute", None)) and callable(registered):
            return registered()
        return registered

    @staticmethod
    def _call_validate(worker: TaskWorker, context: TaskContext) -> None:
        validate = getattr(worker, "validate", None)
        if callable(validate):
            result = validate(context)
            if result is False:
                raise ValueError("任务输入校验失败")

    @staticmethod
    def _validate_checkpoint(worker: TaskWorker, context: TaskContext) -> None:
        task = context.task
        expected_version = getattr(worker, "checkpoint_schema_version", None)
        if expected_version is not None and int(expected_version) != task.checkpoint_schema_version:
            raise CheckpointRejectedError(
                "检查点 Schema 版本不匹配："
                f"存储版本 {task.checkpoint_schema_version}，工作器版本 {expected_version}"
            )
        validate = getattr(worker, "validate_checkpoint", None)
        if callable(validate):
            result = validate(context)
            if isinstance(result, CheckpointValidation):
                verdict = result
            elif isinstance(result, bool):
                verdict = CheckpointValidation(result, "工作器拒绝检查点" if not result else "")
            else:
                raise CheckpointRejectedError(
                    "validate_checkpoint 必须返回 bool 或 CheckpointValidation"
                )
        elif context.task.checkpoint is None and getattr(
            worker, "can_restart_without_checkpoint", False
        ):
            verdict = CheckpointValidation.accept()
        else:
            verdict = CheckpointValidation.reject("工作器没有验证该检查点的能力")
        if not verdict.valid:
            raise CheckpointRejectedError(verdict.reason)

    def _change(
        self,
        task_id: str,
        *,
        expected: set[TaskStatus] | None = None,
        emit: bool = True,
        **changes,
    ) -> TaskRecord:
        with self._lock:
            current = self.store.get(task_id)
            if expected is not None and current.status not in expected:
                raise InvalidTaskTransitionError(
                    f"任务状态已变化：期望 {sorted(s.value for s in expected)}，"
                    f"实际为 {current.status.value}"
                )
            target = changes.get("status", current.status)
            if isinstance(target, str):
                target = TaskStatus(target)
                changes["status"] = target
            if target != current.status and target not in self._TRANSITIONS[current.status]:
                raise InvalidTaskTransitionError(
                    f"不允许任务状态从 {current.status.value} 转为 {target.value}"
                )
            if not changes:
                return current
            updated = replace(current, updated_at=utc_now(), **changes)
            saved = self.store.update(updated, current.revision)
            handle = self._active.get(task_id)
            if handle:
                handle.record = saved
            self._condition.notify_all()
        if emit:
            self._emit(saved)
        return saved

    def _save_checkpoint(
        self,
        handle: _RunHandle,
        checkpoint: Any,
        *,
        progress: Mapping[str, Any] | None,
    ) -> TaskRecord:
        with self._lock:
            current = self.store.get(handle.task_id)
            if current.status not in {
                TaskStatus.RUNNING, TaskStatus.PAUSING, TaskStatus.CANCELLING,
                TaskStatus.RESUMING, TaskStatus.NEEDS_RECONCILIATION,
            }:
                raise InvalidTaskTransitionError(
                    f"任务状态 {current.status.value} 下禁止写入检查点"
                )
            changes = {"checkpoint": checkpoint}
            if progress is not None:
                changes["progress"] = dict(progress)
            updated = replace(current, updated_at=utc_now(), **changes)
            saved = self.store.update(updated, current.revision)
            handle.record = saved
            self._condition.notify_all()
        self._emit(saved)
        return saved

    def _save_progress(self, handle: _RunHandle, progress: Mapping[str, Any]) -> TaskRecord:
        with self._lock:
            current = self.store.get(handle.task_id)
            if current.status not in {
                TaskStatus.RUNNING, TaskStatus.PAUSING, TaskStatus.CANCELLING,
                TaskStatus.RESUMING, TaskStatus.NEEDS_RECONCILIATION,
            }:
                raise InvalidTaskTransitionError(
                    f"任务状态 {current.status.value} 下禁止更新进度"
                )
            updated = replace(current, progress=dict(progress), updated_at=utc_now())
            saved = self.store.update(updated, current.revision)
            handle.record = saved
            self._condition.notify_all()
        self._emit(saved)
        return saved

    def _mark_reconciliation_required(self, handle: _RunHandle, reason: str) -> TaskRecord:
        reason = str(reason or "外部操作结果尚未确认")[:1000]
        current = self.store.get(handle.task_id)
        saved = self._change(
            handle.task_id,
            expected={current.status},
            reconciliation_required=True,
            reconciliation_reason=reason,
            error={"category": "needs_reconciliation", "message": reason},
        )
        return saved

    def _clear_reconciliation_requirement(self, handle: _RunHandle) -> TaskRecord:
        current = self.store.get(handle.task_id)
        return self._change(
            handle.task_id,
            expected={current.status},
            reconciliation_required=False,
            reconciliation_reason="",
            error=None,
        )

    def _emit(self, record: TaskRecord) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(record)
            except Exception:
                logger.exception("Task scheduler listener failed")
