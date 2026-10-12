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
                assert locked["ok"] is True, locked
                assert store.database_state() == store.DatabaseState.LOCKED.value
                assert not store.DB_FILE.exists()
                assert not store.database_connections()
                unlocked = server.vault_unlock(
                    server.VaultUnlockIn(password="safe-password"))
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
                import time
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
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and server.APP["scan_run"]["running"]:
                    time.sleep(.01)
                assert not server.APP["scan_run"]["running"], server.APP["scan_run"]
                json.dumps(server.scan_status())
                task = server._TASK_SCHEDULER.get_task(result["task_id"])
                assert task.status is server.TaskStatus.COMPLETED, task
                assert store.load_folder_scan_states()["10"]["status"] == "complete"
                assert not store.database_connections()
            """
            self._run_subprocess(env, script)


if __name__ == "__main__":
    unittest.main()
