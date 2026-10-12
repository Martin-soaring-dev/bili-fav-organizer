import pytest

from bili_fav.tasks.sqlite_journal import SQLiteTaskJournal


def record(task_id="t1", state="queued"):
    return dict(id=task_id, kind="scan", state=state, resource="scan",
                created_at=1.0, started_at=None, finished_at=None, error=None)


def test_roundtrip_and_update(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    journal = SQLiteTaskJournal(path)
    journal.save(record())
    assert SQLiteTaskJournal(path).get("t1")["state"] == "queued"
    updated = record(state="completed")
    updated["finished_at"] = 3.0
    journal.save(updated)
    assert journal.get("t1")["finished_at"] == 3.0
    assert len(journal.load()) == 1


def test_recovery_never_replays(tmp_path):
    journal = SQLiteTaskJournal(tmp_path / "jobs.sqlite3")
    for i, state in enumerate(("queued", "running", "cancelling", "completed")):
        journal.save(record(str(i), state))
    assert journal.recover_interrupted() == 3
    assert [r["state"] for r in journal.load()] == [
        "interrupted", "interrupted", "interrupted", "completed"]
    assert journal.recover_interrupted() == 0


def test_rejects_unknown_state(tmp_path):
    journal = SQLiteTaskJournal(tmp_path / "jobs.sqlite3")
    with pytest.raises(ValueError):
        journal.save(record(state="not-a-state"))
