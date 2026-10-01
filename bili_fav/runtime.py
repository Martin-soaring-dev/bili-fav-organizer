"""Persistent, single-worker job runtime used by the local application."""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from contextlib import nullcontext
from typing import Callable


class JobConflict(RuntimeError):
    """Raised when a durable job is already active."""


def _json_safe(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class TaskManager:
    """Serialize long-running work and persist its lifecycle in SQLite.

    Runners are registered in memory for the current process. On restart, work that
    was queued or running is marked interrupted and is never replayed implicitly.
    Existing domain checkpoints remain the source for a user-initiated resume.
    """

    def __init__(self, store_module, operation_lock=None):
        self._store = store_module
        self._operation_lock = operation_lock
        self._condition = threading.Condition()
        self._queue = deque()
        self._callbacks = {}
        self._local = threading.local()
        self._started = False
        self._worker = None

    def start(self):
        with self._condition:
            if self._started:
                return
            self._store.recover_incomplete_work()
            self._started = True
            self._worker = threading.Thread(target=self._work, name="bili-fav-worker", daemon=True)
            self._worker.start()

    @staticmethod
    def _snapshot(provider):
        if not provider:
            return {}
        try:
            state = provider()
            return _json_safe(state if isinstance(state, dict) else {})
        except Exception:
            return {}

    def submit(self, kind: str, runner: Callable, *, payload=None,
               state_provider=None, on_cancel=None) -> str:
        self.start()
        job_id = uuid.uuid4().hex
        initial = self._snapshot(state_provider)
        try:
            self._store.create_job(job_id, kind, payload or {}, initial)
        except RuntimeError as exc:
            raise JobConflict(str(exc)) from exc
        with self._condition:
            self._callbacks[job_id] = (runner, state_provider, on_cancel)
            self._queue.append(job_id)
            self._condition.notify()
        return job_id

    def _work(self):
        while True:
            with self._condition:
                while not self._queue:
                    self._condition.wait()
                job_id = self._queue.popleft()
                callbacks = self._callbacks.get(job_id)
            row = self._store.load_job(job_id)
            if not row or row.get("status") != "queued" or callbacks is None:
                with self._condition:
                    self._callbacks.pop(job_id, None)
                    self._condition.notify_all()
                continue
            runner, state_provider, _on_cancel = callbacks
            self._store.update_job(job_id, status="running",
                                   progress=self._snapshot(state_provider))
            self._local.job_id = job_id
            error = None
            try:
                with (self._operation_lock if self._operation_lock is not None else nullcontext()):
                    # A local synchronous catalog update may have won the lock after
                    # the job was queued. Re-check cancellation before any work starts.
                    row = self._store.load_job(job_id) or {}
                    if not row.get("cancel_requested"):
                        runner()
            except Exception as exc:  # persist failure before allowing the worker to continue
                error = str(exc) or type(exc).__name__
            finally:
                state = self._snapshot(state_provider)
                row = self._store.load_job(job_id) or {}
                if row.get("cancel_requested"):
                    status = "cancelled"
                elif error or state.get("error"):
                    status = "failed"
                    error = error or str(state.get("error"))
                else:
                    status = "completed"
                self._store.update_job(job_id, status=status, progress=state, error=error)
                self._local.job_id = None
                with self._condition:
                    self._callbacks.pop(job_id, None)
                    self._condition.notify_all()

    def cancel(self, job_id: str) -> bool:
        self.start()
        row = self._store.load_job(job_id)
        if not row or row.get("status") not in ("queued", "running"):
            return False
        if row["status"] == "queued":
            self._store.update_job(job_id, status="cancelled", cancel_requested=True)
            return True
        with self._condition:
            callback = self._callbacks.get(job_id)
        on_cancel = callback[2] if callback else None
        if on_cancel is None:
            return False
        self._store.update_job(job_id, cancel_requested=True)
        on_cancel()
        return True

    def record_event(self, event: dict):
        job_id = getattr(self._local, "job_id", None)
        if not job_id:
            return
        if not any(key in event for key in ("done", "total", "current", "step", "kind")):
            return
        callbacks = self._callbacks.get(job_id)
        state = self._snapshot(callbacks[1]) if callbacks else {}
        state["last_event"] = _json_safe(event)
        self._store.update_job(job_id, progress=state)

    @property
    def current_job_id(self) -> str | None:
        return getattr(self._local, "job_id", None)

    def get(self, job_id: str) -> dict | None:
        return self._store.load_job(job_id)

    def list(self, *, limit=50, kind=None) -> list[dict]:
        return self._store.load_jobs(limit=limit, kind=kind)

    def latest(self, kind: str) -> dict | None:
        rows = self._store.load_jobs(limit=1, kind=kind)
        return rows[0] if rows else None

    def active(self) -> dict | None:
        return self._store.load_active_job()

    def wait(self, job_id: str, timeout: float = 5.0) -> dict | None:
        deadline = time.monotonic() + max(0, timeout)
        while time.monotonic() < deadline:
            row = self._store.load_job(job_id)
            if not row or row.get("status") not in ("queued", "running"):
                return row
            time.sleep(0.01)
        return self._store.load_job(job_id)
