"""Experimental SQLite task journal.

Use a dedicated database path, not the production vault-managed database.
Writes are transactional; stale tasks become interrupted without replay.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

ACTIVE = ("queued", "running", "cancelling")
STATES = (*ACTIVE, "completed", "failed", "cancelled", "interrupted")


class SQLiteTaskJournal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS task_jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                state TEXT NOT NULL,
                resource TEXT,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                error TEXT
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS task_jobs_state ON task_jobs(state)")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def load(self) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM task_jobs ORDER BY created_at, id")]

    def recover_interrupted(self) -> int:
        """Fail closed; caller must reconcile remote side effects."""
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE task_jobs SET state='interrupted', finished_at=?,
                   error='Process stopped; verify external effects before retrying'
                   WHERE state IN ('queued','running','cancelling')""",
                (time.time(),))
            return cursor.rowcount

    def save(self, record: dict) -> None:
        if record["state"] not in STATES:
            raise ValueError("Unknown task state")
        with self._connect() as db:
            db.execute("""INSERT INTO task_jobs
                (id,kind,state,resource,created_at,started_at,finished_at,error)
                VALUES (:id,:kind,:state,:resource,:created_at,:started_at,:finished_at,:error)
                ON CONFLICT(id) DO UPDATE SET
                state=excluded.state, resource=excluded.resource,
                started_at=excluded.started_at, finished_at=excluded.finished_at,
                error=excluded.error""", record)

    def get(self, task_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM task_jobs WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None
