from threading import Event

from bili_fav.application.task_bridge import LegacyTaskBridge


def test_bridge_runs_legacy_callable(tmp_path):
    completed = Event()
    bridge = LegacyTaskBridge(tmp_path / "tasks.sqlite3")
    try:
        task_id = bridge.launch("scan", lambda: completed.set(), resource="scan")
        assert completed.wait(3)
    finally:
        bridge.close()
    assert bridge.manager.get(task_id)["state"] == "completed"
