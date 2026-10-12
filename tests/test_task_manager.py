import json
import threading

import pytest

from bili_fav.tasks import TaskManager, TaskConflictError


def test_task_completes_and_persists(tmp_path):
    done = threading.Event()
    manager = TaskManager(tmp_path / "tasks.json", max_workers=1)
    try:
        task_id = manager.submit("scan", lambda cancel: done.set(), resource="scan")
        assert done.wait(3)
    finally:
        manager.shutdown()
    assert manager.get(task_id)["state"] == "completed"
    assert json.loads((tmp_path / "tasks.json").read_text())["tasks"][0]["id"] == task_id


def test_exclusive_resource_and_cancel(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    manager = TaskManager(tmp_path / "tasks.json", max_workers=1)
    def worker(cancel):
        entered.set()
        release.wait(3)
    try:
        task_id = manager.submit("apply", worker, resource="remote-write")
        assert entered.wait(3)
        with pytest.raises(TaskConflictError):
            manager.submit("apply", worker, resource="remote-write")
        assert manager.cancel(task_id)
    finally:
        release.set()
        manager.shutdown()
    assert manager.get(task_id)["state"] == "cancelled"


def test_interrupted_jobs_are_not_replayed(tmp_path):
    journal = tmp_path / "tasks.json"
    journal.write_text(json.dumps({"schema": 1, "tasks": [
        {"id": "old", "kind": "apply", "state": "running", "resource": "remote-write",
         "created_at": 1, "started_at": 2, "finished_at": None, "error": None}
    ]}))
    manager = TaskManager(journal)
    try:
        record = manager.get("old")
        assert record["state"] == "interrupted"
        assert "verify external effects" in record["error"]
    finally:
        manager.shutdown()
