"""Read-only compatibility view of legacy task status.

This does not schedule work and must not expose the entire APP dictionary.
The existing local API guard and vault lock gate apply to these routes.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from fastapi import APIRouter

LEGACY_KINDS = ("scan", "analyze", "apply")


def normalize_legacy_task(kind: str, raw: Mapping | None) -> dict:
    """Convert a legacy status snapshot to a stable read-only task representation."""
    if kind not in LEGACY_KINDS:
        raise ValueError("Unsupported task kind")
    status = dict(raw or {})
    running = bool(status.get("running"))
    error = status.get("error")
    if running:
        state = "cancelling" if status.get("stop") else "running"
    elif error:
        state = "failed"
    elif status.get("finished") or status.get("completed"):
        state = "completed"
    else:
        state = "idle"
    return {
        "id": f"legacy:{kind}",
        "kind": kind,
        "state": state,
        "running": running,
        "done": status.get("done", 0),
        "total": status.get("total", 0),
        "error": str(error) if error else None,
        "source": "legacy",
    }


def create_task_router(snapshot: Callable[[], Mapping]) -> APIRouter:
    router = APIRouter(prefix="/api/tasks", tags=["tasks"])

    @router.get("/legacy")
    def list_legacy_tasks():
        current = snapshot()
        return {"tasks": [
            normalize_legacy_task(kind, current.get(f"{kind}_run"))
            for kind in LEGACY_KINDS
        ]}


    @router.get("/legacy/{kind}")
    def get_legacy_task(kind: str):
        from fastapi import HTTPException
        if kind not in LEGACY_KINDS:
            raise HTTPException(status_code=404, detail="Unknown task kind")
        return normalize_legacy_task(kind, snapshot().get(f"{kind}_run"))

    return router
