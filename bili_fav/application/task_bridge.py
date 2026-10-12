"""Incremental bridge between legacy jobs and the task runtime.

This adapter deliberately does not own the vault lifecycle. Callers must enforce
the existing vault gate before launching tasks and keep legacy cancellation flags.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import Event

from bili_fav.tasks import TaskManager, TaskConflictError


class LegacyTaskBridge:
    def __init__(self, journal_path: str | Path, max_workers: int = 3):
        self.manager = TaskManager(journal_path, max_workers=max_workers)

    def launch(self, kind: str, run: Callable[[], None], *,
               resource: str | None = None) -> str:
        def worker(cancel: Event):
            if not cancel.is_set():
                run()
        return self.manager.submit(kind, worker, resource=resource)

    def close(self, wait: bool = True):
        self.manager.shutdown(wait=wait)
