"""Small, dependency-free task manager for incremental adoption.

Jobs execute in worker threads; state snapshots are persisted atomically to a JSON
journal under a caller-selected data directory. This is deliberately separate from
the existing SQLite/vault until lifecycle integration is designed and tested.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Event, RLock
from typing import Callable, Any
import json
import os
import tempfile
import time
import uuid

from .sqlite_journal import SQLiteTaskJournal


class TaskConflictError(RuntimeError):
    """A task with the same exclusive resource is already active."""


@dataclass
class TaskRecord:
    id: str
    kind: str
    state: str = "queued"
    resource: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None


class TaskManager:
    """Thread-based jobs with cooperative cancellation and fail-closed recovery.

    A worker receives a threading.Event; it must check this event at safe
    cancellation points. Never replay interrupted remote mutations automatically.
    """

    def __init__(self, journal_path: str | Path, max_workers: int = 2):
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self.path = Path(journal_path)
        self._sqlite = SQLiteTaskJournal(self.path) if self.path.suffix in ('.sqlite', '.sqlite3', '.db') else None
        self._lock = RLock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="bilifav-task")
        self._records: dict[str, TaskRecord] = {}
        self._cancel: dict[str, Event] = {}
        self._load()

    def _load(self) -> None:
        if self._sqlite:
            self._sqlite.recover_interrupted()
            items = self._sqlite.load()
        else:
            if not self.path.exists():
                return
            items = json.loads(self.path.read_text(encoding="utf-8")).get("tasks", [])
        for item in items:
            record = TaskRecord(**item)
            if record.state in ("queued", "running", "cancelling"):
                record.state = "interrupted"
                record.finished_at = time.time()
                record.error = "Process stopped; verify external effects before retrying"
            self._records[record.id] = record
        self._save()

    def _save(self) -> None:
        if self._sqlite:
            for record in self._records.values():
                self._sqlite.save(asdict(record))
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"schema": 1, "tasks": [asdict(x) for x in self._records.values()]},
                             ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(prefix=".tasks-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def submit(self, kind: str, worker: Callable[[Event], Any], *, resource: str | None = None) -> str:
        with self._lock:
            if resource and any(r.resource == resource and r.state in ("queued", "running", "cancelling")
                                for r in self._records.values()):
                raise TaskConflictError(f"Resource busy: {resource}")
            record = TaskRecord(id=uuid.uuid4().hex, kind=kind, resource=resource)
            cancel = Event()
            self._records[record.id] = record
            self._cancel[record.id] = cancel
            self._save()
            self._pool.submit(self._run, record.id, worker, cancel)
            return record.id

    def _run(self, task_id: str, worker: Callable[[Event], Any], cancel: Event) -> None:
        with self._lock:
            record = self._records[task_id]
            if cancel.is_set():
                record.state = "cancelled"
                record.finished_at = time.time()
                self._save()
                return
            record.state = "running"
            record.started_at = time.time()
            self._save()
        try:
            worker(cancel)
        except Exception as exc:
            with self._lock:
                record.state = "failed"
                record.error = f"{type(exc).__name__}: {exc}"
                record.finished_at = time.time()
                self._save()
        else:
            with self._lock:
                record.state = "cancelled" if cancel.is_set() else "completed"
                record.finished_at = time.time()
                self._save()
        finally:
            with self._lock:
                self._cancel.pop(task_id, None)

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            record = self._records.get(task_id)
            if record is None or record.state not in ("queued", "running"):
                return False
            self._cancel[task_id].set()
            record.state = "cancelling"
            self._save()
            return True

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            record = self._records.get(task_id)
            return asdict(record) if record else None

    def list(self) -> list[dict]:
        with self._lock:
            return [asdict(record) for record in self._records.values()]

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)
