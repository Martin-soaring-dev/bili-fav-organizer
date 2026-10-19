"""Production composition smoke tests for scheduler and vault lifecycle wiring."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class ServerSchedulerIntegrationTests(unittest.TestCase):
    def _run_subprocess(self, env: dict[str, str], script: str) -> None:
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(script)],
            cwd=REPOSITORY_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout)

    def test_vault_lock_waits_for_checkpoint_then_resumes_same_process_runner(self):
        with tempfile.TemporaryDirectory(prefix="bfo-scheduler-vault-") as temp_dir:
            root = Path(temp_dir)
            env = os.environ.copy()
            env["LOCALAPPDATA"] = str(root / "profile")
            env["HOME"] = str(root / "home")
            env["BILI_FAV_ORGANIZER_DATA_DIR"] = str(root / "profile" / "data")
            env["BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL"] = "1"
            script = textwrap.dedent(r"""
                import os, tempfile, time, uuid
                from pathlib import Path
                import store
                store.initialize_new_database()
                import server
                server.VAULT.initialize(bound_mid="12345", password="safe-password")
                server.VAULT.complete_onboarding()
                state = {"id": uuid.uuid4().hex, "running": True, "done": 0}
                def runner():
                    if state["done"] == 0:
                        time.sleep(.25)
                        state["done"] = 1
                        server._task_safe_point(state, {"done": 1}, {"done": 1})
                    state["running"] = False
                _record, scheduler = server._submit_app_task(
                    "scan", state, runner, parameters={"fixture": True})
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    current = scheduler.get_task(state["id"])
                    if current.status is server.TaskStatus.RUNNING:
                        break
                    time.sleep(.005)
                locked = server.vault_lock()
                assert isinstance(locked, dict), getattr(locked, "body", locked)
                assert locked["ok"] is True, locked
                assert store.database_state() == store.DatabaseState.LOCKED.value
                assert not store.DB_FILE.exists()
                assert not store.database_connections()
                unlocked = server.vault_unlock(
                    server.VaultUnlockIn(password="safe-password"))
                assert isinstance(unlocked, dict), getattr(unlocked, "body", unlocked)
                assert unlocked["ok"] is True, unlocked
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    current = scheduler.get_task(state["id"])
                    if current.status in {server.TaskStatus.COMPLETED,
                                          server.TaskStatus.FAILED}:
                        break
                    time.sleep(.01)
                assert current.status is server.TaskStatus.COMPLETED, current
                assert state["done"] == 1
                assert scheduler.admission_open
            """)
            self._run_subprocess(env, script)

    def test_scan_route_persists_and_runs_through_the_scheduler(self):
        with tempfile.TemporaryDirectory(prefix="bfo-scheduler-scan-") as temp_dir:
            root = Path(temp_dir)
            env = os.environ.copy()
            env["LOCALAPPDATA"] = str(root / "profile")
            env["HOME"] = str(root / "home")
            env["BILI_FAV_ORGANIZER_DATA_DIR"] = str(root / "profile" / "data")
            env["BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL"] = "1"
            script = r"""
                import json
                import store
                store.initialize_new_database()
                import server
                class FakeSession:
                    read_interval = 2
                    write_interval = 2
                    def get_mid(self): return "12345"
                    def list_folders(self):
                        return [{"media_id": "10", "title": "empty", "count": 0,
                                 "attr": 0}]
                    def get_folder_resource_ids(self, *args, **kwargs): return []
                    def get_resource_infos_bulk(self, *args, **kwargs): return []
                server.load_config = lambda: {}
                server.get_session_cookie = lambda: object()
                server.bili_api.BiliSession = lambda _cookie: FakeSession()
                result = server.scan(server.ScanIn(folder_ids=["10"], mode="rebuild"))
                assert result["started"] is True, result
                # 业务 running 先落 false，调度器记录随后才写终态；只等 running
                # 会在记录还是 RUNNING 时就断言，属于竞态。
                task = server._TASK_SCHEDULER.wait_for_status(
                    result["task_id"],
                    {server.TaskStatus.COMPLETED, server.TaskStatus.FAILED},
                    timeout=20)
                assert not server.APP["scan_run"]["running"], server.APP["scan_run"]
                json.dumps(server.scan_status())
                assert task.status is server.TaskStatus.COMPLETED, task
                assert store.load_folder_scan_states()["10"]["status"] == "complete"
                assert not store.database_connections()
            """
            self._run_subprocess(env, script)

    def test_vault_seals_business_files_without_reading_or_replacing_runtime_locks(self):
        with tempfile.TemporaryDirectory(prefix="bfo-vault-owner-") as temp_dir:
            root = Path(temp_dir)
            env = os.environ.copy()
            env["LOCALAPPDATA"] = str(root / "profile")
            env["HOME"] = str(root / "home")
            env["BILI_FAV_ORGANIZER_DATA_DIR"] = str(root / "profile" / "data")
            env["BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL"] = "1"
            self._run_subprocess(env, r"""
                from pathlib import Path
                from unittest import mock
                import store
                store.initialize_new_database()
                import server
                from core.scheduler.ownership import FileOwnerLock

                server.VAULT.initialize(bound_mid="12345", password="safe-password")
                scheduler = server._ensure_task_scheduler()
                owner = store.DATA_DIR / ".scheduler-owner.lock"
                inode = owner.stat().st_ino
                store.LEGACY_DATA_DIR.mkdir(parents=True, exist_ok=True)
                legacy_locks = [store.LEGACY_DATA_DIR / name for name in
                                (".bfo-profile.lock", ".scheduler-owner.lock")]
                for lock in legacy_locks:
                    lock.write_bytes(b"runtime control file")
                # An arbitrary business file ending in .lock still needs sealing.
                business = store.DATA_DIR / "business-document.lock"
                business.write_bytes(b"synthetic business data")
                legacy_business = store.LEGACY_DATA_DIR / "folders.json"
                legacy_business.write_bytes(b"[]")
                runtime_paths = {path.resolve() for path in [owner, *legacy_locks]}
                read_attempts = []
                original_read = Path.read_bytes

                def windows_locked_read(path):
                    if path.resolve() in runtime_paths:
                        read_attempts.append(str(path))
                        raise PermissionError(13, "runtime byte range is locked", str(path))
                    return original_read(path)

                with mock.patch.object(Path, "read_bytes", windows_locked_read):
                    locked = server.vault_lock()
                    assert isinstance(locked, dict), getattr(locked, "body", locked)
                    assert locked["ok"] is True, locked
                    assert not business.exists()
                    assert not legacy_business.exists()
                    assert owner.stat().st_ino == inode
                    assert all(path.is_file() for path in legacy_locks)
                    contender = FileOwnerLock(owner)
                    try:
                        contender.acquire()
                    except RuntimeError:
                        pass
                    else:
                        contender.release()
                        raise AssertionError("vault lock released scheduler ownership")
                    unlocked = server.vault_unlock(
                        server.VaultUnlockIn(password="safe-password"))
                    assert isinstance(unlocked, dict), getattr(unlocked, "body", unlocked)
                    assert unlocked["ok"] is True, unlocked
                    assert owner.stat().st_ino == inode
                    manifest = server._vault_read_lock_manifest(required=True)
                    archived_sources = {Path(item["source"]).resolve()
                                        for item in manifest["entries"]}
                    assert business.resolve() in archived_sources
                    assert legacy_business.resolve() in archived_sources
                    assert not (runtime_paths & archived_sources)
                assert not read_attempts, read_attempts
                scheduler.shutdown(wait=True, timeout=5, pause_running=False)
            """)

    def test_unlock_starts_scheduler_and_recovers_stale_task_rows(self):
        with tempfile.TemporaryDirectory(prefix="bfo-scheduler-unlock-recovery-") as temp_dir:
            root = Path(temp_dir)
            env = os.environ.copy()
            env["LOCALAPPDATA"] = str(root / "profile")
            env["HOME"] = str(root / "home")
            env["BILI_FAV_ORGANIZER_DATA_DIR"] = str(root / "profile" / "data")
            env["BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL"] = "1"
            script = textwrap.dedent(r"""
                import store
                store.initialize_new_database()
                import server
                from core.scheduler import SQLiteTaskStore, TaskRecord, TaskStatus

                # This row represents a cancellation persisted by the old
                # process immediately before it exited.
                task_store = SQLiteTaskStore.from_lifecycle(store._lifecycle())
                stale, created = task_store.create(TaskRecord(
                    task_type="scan", status=TaskStatus.CANCELLING))
                assert created

                server.VAULT.initialize(bound_mid="12345", password="safe-password")
                server.VAULT.complete_onboarding()
                server._vault_save_db_encrypted()
                server.VAULT.lock()
                assert store.database_state() == store.DatabaseState.LOCKED.value
                assert not store.DB_FILE.exists()
                assert server._TASK_SCHEDULER is None

                unlocked = server.vault_unlock(
                    server.VaultUnlockIn(password="safe-password"))
                assert isinstance(unlocked, dict), getattr(unlocked, "body", unlocked)
                assert unlocked["ok"] is True, unlocked
                assert server._TASK_SCHEDULER is not None
                recovered = server._TASK_SCHEDULER.get_task(stale.task_id)
                assert recovered.status is TaskStatus.CANCELLED, recovered
                assert recovered.error["category"] == "cancelled_after_restart"
            """)
            self._run_subprocess(env, script)


if __name__ == "__main__":
    unittest.main()
