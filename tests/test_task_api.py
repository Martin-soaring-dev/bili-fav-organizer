from fastapi import FastAPI
from fastapi.testclient import TestClient

from bili_fav.api.tasks import create_task_router, normalize_legacy_task


def test_normalize_running_and_failed():
    assert normalize_legacy_task("scan", {"running": True})["state"] == "running"
    assert normalize_legacy_task("scan", {"running": True, "stop": True})["state"] == "cancelling"
    assert normalize_legacy_task("apply", {"error": "network"})["state"] == "failed"


def test_router_reads_snapshot_without_mutation():
    state = {"scan_run": {"running": True, "done": 3, "total": 10},
             "analyze_run": None, "apply_run": None}
    app = FastAPI()
    app.include_router(create_task_router(lambda: state))
    client = TestClient(app)
    response = client.get("/api/tasks/legacy")
    assert response.status_code == 200
    tasks = response.json()["tasks"]
    assert [task["kind"] for task in tasks] == ["scan", "analyze", "apply"]
    assert tasks[0]["done"] == 3
    assert "token" not in str(tasks)
    assert state["scan_run"]["running"] is True


def test_individual_legacy_task_and_unknown_kind():
    app = FastAPI()
    app.include_router(create_task_router(lambda: {"scan_run": {"running": True}}))
    client = TestClient(app)
    assert client.get("/api/tasks/legacy/scan").json()["state"] == "running"
    assert client.get("/api/tasks/legacy/unknown").status_code == 404
