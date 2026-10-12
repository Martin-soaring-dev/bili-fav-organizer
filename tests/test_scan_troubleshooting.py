"""Scan recovery uses synthetic remote responses and a fresh isolated SQLite DB."""
import os
import shutil
import sqlite3
import sys
import time
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL", "1")
# Keep this feature's tests independent of other, pre-existing test helpers.
# Seed empty storage before importing the app so legacy user data is never migrated.
TEST_ROOT = Path(__file__).resolve().parent / ".test-data" / "scan-recovery"
TEST_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["BILI_FAV_ORGANIZER_DATA_DIR"] = str(TEST_ROOT)
os.environ["LOCALAPPDATA"] = str(TEST_ROOT)
settings = TEST_ROOT / "BiliFavOrganizer"
settings.mkdir(exist_ok=True)
for filename in ("config.json", "secrets.json"):
    (settings / filename).write_text("{}", encoding="utf-8")
with sqlite3.connect(TEST_ROOT / "library.sqlite3") as conn:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(6, 'test')")
import server
import store
from core.database import DatabaseNotInitializedError, DatabaseRecoveryRequiredError
from fastapi.testclient import TestClient


class FakeSession:
    def __init__(self, count=32, readable=31):
        self.count, self.readable = count, readable
        self.calls = []
        self.failure = None
        self.clean_failure = None
        self.clean_changes_count = True
        self.duplicate = False

    def list_folders(self):
        self.calls.append("directory")
        return [{"media_id": "10", "title": "日常生活技巧", "count": self.count, "attr": 0}]

    def get_folder_resource_ids(self, *args, **kwargs):
        raise server.bili_api.BiliApiError("模拟批量接口数量不一致，回退分页")

    def iter_folder_videos(self, media_id, expected, **kwargs):
        if self.failure:
            raise self.failure
        for index in range(self.readable):
            yield {"bvid": f"BVtest{index}", "aid": index + 1, "type": 2,
                   "title": f"视频 {index}", "source_folder_id": media_id}
        if self.duplicate:
            yield {"bvid": "BVtest0", "aid": 1, "type": 2, "title": "视频 0"}

    def clean_invalid_folder(self, media_id, **kwargs):
        self.calls.append("clean")
        if self.clean_failure:
            raise self.clean_failure
        if self.clean_changes_count:
            self.count = self.readable


class ScanTroubleshootingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        profile = TEST_ROOT / f"troubleshooting-{uuid.uuid4().hex}"
        data_dir = profile / "data"
        data_dir.mkdir(parents=True)
        db = data_dir / "library.sqlite3"
        self.stack.callback(shutil.rmtree, profile, True)
        for name, value in (("DATA_DIR", data_dir), ("DB_FILE", db),
                            ("LEGACY_DB_FILE", profile / "legacy.sqlite3"),
                            ("_LEGACY_FILES", {})):
            self.stack.enter_context(patch.object(store, name, value))
        store.initialize_new_database()
        self.stack.enter_context(patch.dict(server.APP, {key: None for key in (
            "session", "scan_run", "analyze_run", "apply_run", "folder_merge_run",
            "folder_merge_ai_run", "folder_profile_run")}))
        self.stack.enter_context(patch.object(server, "load_config", return_value={}))
        self.stack.enter_context(patch.object(server, "get_session_cookie", return_value="test"))
        self.fake = FakeSession()
        self.stack.enter_context(patch.object(server.bili_api, "BiliSession", return_value=self.fake))
        self.client = self.stack.enter_context(TestClient(server.app))

    def run_scan(self):
        result = server.scan(server.ScanIn(folder_ids=["10"], mode="rebuild"))
        self.wait_scan(result)
        return result

    def wait_scan(self, result):
        self.assertIsInstance(result, dict, getattr(result, "body", None))
        self.assertTrue(result["started"])
        deadline = time.monotonic() + 5
        while server.APP["scan_run"]["running"] and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(server.APP["scan_run"]["running"])
        self.assertEqual(result["run_id"], server.APP["scan_run"]["id"])

    def recover(self, action="retry", confirmed=False):
        result = server.scan_issue_recover("10", server.ScanRecoveryIn(action=action, confirmed=confirmed))
        self.wait_scan(result)

    def test_normal_scan_does_not_create_issue(self):
        self.fake.count = 31
        self.run_scan()
        self.assertFalse(server.scan_issues_get()["blocked"])
        self.assertEqual("complete", store.load_folder_scan_states()["10"]["status"])
        self.assertNotIn("clean", self.fake.calls)

    def test_gap_persists_and_does_not_clean_or_replace_old_snapshot(self):
        store.save_videos({"BVold": {"bvid": "BVold", "aid": 99, "folder_ids": ["10"]}})
        self.run_scan()
        issue = server.scan_issues_get()["issues"][0]
        self.assertEqual((32, 31, 31), (issue["expected_count"], issue["fetched_count"], issue["unique_count"]))
        self.assertEqual(1, store.load_folder_item_counts()["10"])
        self.assertNotIn("clean", self.fake.calls)
        server.APP["scan_run"] = None  # A restart must not remove the issue.
        self.assertTrue(server.scan_issues_get()["blocked"])

    def test_manual_retry_resolves_after_remote_cleanup(self):
        self.run_scan()
        self.fake.count = 31
        store.save_scan_selection(["10", "other"])
        self.recover()
        self.assertFalse(server.scan_issues_get()["blocked"])
        self.assertEqual(["10", "other"], store.load_scan_selection())
        self.assertEqual(31, store.load_folder_item_counts()["10"])
        self.assertEqual("resolved", store.load_scan_issues()["10"]["status"])
        self.assertNotIn("clean", self.fake.calls)

    def test_refresh_resolves_sync_delay_without_cleaning(self):
        self.run_scan()
        self.fake.count = 31
        self.recover("refresh")
        self.assertFalse(server.scan_issues_get()["blocked"])
        self.assertNotIn("clean", self.fake.calls)

    def test_save_config_can_persist_qr_login_before_database_initialization(self):
        profile = TEST_ROOT / f"fresh-config-{uuid.uuid4().hex}"
        profile.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, profile, True)
        config_file = profile / "config.json"
        secrets_file = profile / "secrets.json"
        secrets_file.write_text('{"api_key":"legacy-key"}', encoding="utf-8")

        with patch.object(server, "USER_DATA_DIR", profile), \
             patch.object(server, "CONFIG_FILE", config_file), \
             patch.object(server, "SECRETS_FILE", secrets_file), \
             patch.object(server.store, "list_providers",
                          side_effect=DatabaseNotInitializedError("first use")):
            server.save_config({"cookie_string": "SESSDATA=qr-login", "api_key": "legacy-key",
                                "scan_interval": 2})

        self.assertEqual({"scan_interval": 2}, server._read_json_file(config_file)[0])
        self.assertEqual({"api_key": "legacy-key", "cookie_string": "SESSDATA=qr-login"},
                         server._read_json_file(secrets_file)[0])

    def test_save_config_does_not_swallow_database_recovery_required(self):
        profile = TEST_ROOT / f"recovery-config-{uuid.uuid4().hex}"
        profile.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, profile, True)

        with patch.object(server, "USER_DATA_DIR", profile), \
             patch.object(server, "CONFIG_FILE", profile / "config.json"), \
             patch.object(server, "SECRETS_FILE", profile / "secrets.json"), \
             patch.object(server.store, "list_providers",
                          side_effect=DatabaseRecoveryRequiredError("restore required")):
            with self.assertRaises(DatabaseRecoveryRequiredError):
                server.save_config({"scan_interval": 2})

    def test_scan_tree_and_organize_suggest_accept_legacy_media_count(self):
        legacy_folders = [{"media_id": "10", "title": "旧版收藏夹", "media_count": 3}]
        with patch.object(server.store, "load_folders", return_value=legacy_folders), \
             patch.object(server.store, "load_folder_scan_states", return_value={}), \
             patch.object(server.store, "load_videos_raw", return_value={}):
            tree = server.scan_tree()
        self.assertEqual(3, tree["folders"][0]["expected"])

        with patch.object(server, "_organization_profile_readiness",
                          return_value={"ready": True, "missing": []}), \
             patch.object(server.store, "load_folders", return_value=legacy_folders), \
             patch.object(server.store, "load_videos", return_value=[]), \
             patch.object(server, "_current_folder_profile_contexts", return_value=[]), \
             patch.object(server, "make_llm_config", return_value=SimpleNamespace(configured=False)):
            result = server.folder_organize_suggest()
        self.assertEqual(400, result.status_code)
        self.assertIn("LLM 未配置", result.body.decode("utf-8"))

    def test_scheduler_pause_emits_paused_scan_end_and_clears_running_state(self):
        self.fake.count = 0
        self.fake.readable = 0
        before = len(server.APP["events"])

        def submit_inline(_task_type, state, runner, **_kwargs):
            try:
                runner()
            except server.PauseRequested:
                pass
            return SimpleNamespace(task_id=state["id"]), None

        with patch.object(server, "_submit_app_task", side_effect=submit_inline), \
             patch.object(server, "_task_safe_point",
                          side_effect=server.PauseRequested("user pause")):
            result = server.scan(server.ScanIn(folder_ids=["10"], mode="rebuild"))

        self.assertTrue(result["started"])
        self.assertFalse(server.APP["scan_run"]["running"])
        self.assertEqual("paused", server.APP["scan_run"]["status"])
        events = [event for event in server.APP["events"][before:]
                  if event.get("run_id") == result["run_id"]]
        self.assertTrue(any(event.get("kind") == "scan_end" and
                            event.get("outcome") == "paused" for event in events))

    def test_manual_stop_rate_limit_is_reported_as_cancellation(self):
        self.fake.count = 1
        self.fake.readable = 0
        before = len(server.APP["events"])

        def stop_then_rate_limit(*_args, **_kwargs):
            server.APP["scan_run"]["stop"] = True
            raise server.bili_api.RateLimitedError("已手动停止（请求间隔等待期间）")

        self.fake.iter_folder_videos = stop_then_rate_limit

        def submit_inline(_task_type, state, runner, **_kwargs):
            try:
                runner()
            except server.CancelRequested:
                pass
            return SimpleNamespace(task_id=state["id"]), None

        with patch.object(server, "_submit_app_task", side_effect=submit_inline):
            result = server.scan(server.ScanIn(folder_ids=["10"], mode="rebuild"))

        self.assertTrue(result["started"])
        self.assertFalse(server.APP["scan_run"]["running"])
        self.assertEqual("cancelled", server.APP["scan_run"]["status"])
        events = [event for event in server.APP["events"][before:]
                  if event.get("run_id") == result["run_id"]]
        self.assertTrue(any(event.get("kind") == "scan_end" and
                            event.get("outcome") == "cancelled" for event in events))
        self.assertFalse(any(event.get("outcome") == "rate_limited" for event in events))

    def test_retry_with_gap_stays_blocked(self):
        self.run_scan()
        self.recover()
        self.assertTrue(server.scan_issues_get()["blocked"])
        self.assertGreaterEqual(len(store.load_scan_issues()["10"]["history"]), 3)

    def test_clean_requires_explicit_confirmation(self):
        self.run_scan()
        response = self.client.post("/api/scan/issues/10/recover", json={"action": "clean"})
        self.assertEqual(400, response.status_code)
        self.assertNotIn("clean", self.fake.calls)

    def test_confirmed_clean_refreshes_and_validates(self):
        self.run_scan()
        self.recover("clean", True)
        self.assertFalse(server.scan_issues_get()["blocked"])
        clean_index = self.fake.calls.index("clean")
        self.assertEqual("directory", self.fake.calls[clean_index + 1])
        self.assertEqual(1, self.fake.calls.count("clean"))

    def test_clean_success_without_count_recovery_is_not_resolved(self):
        self.run_scan()
        self.fake.clean_changes_count = False
        self.recover("clean", True)
        self.assertTrue(server.scan_issues_get()["blocked"])

    def test_uncertain_cleanup_is_never_replayed(self):
        self.run_scan()
        self.fake.clean_failure = server.bili_api.WriteUncertainError("清理请求超时，结果不确定")
        self.recover("clean", True)
        self.assertTrue(store.load_scan_issues()["10"]["cleanup_unknown"])
        result = server.scan_issue_recover("10", server.ScanRecoveryIn(action="clean", confirmed=True))
        self.assertEqual(409, result.status_code)
        self.assertEqual(1, self.fake.calls.count("clean"))
        self.fake.count = 31  # The user checks the result remotely and retries the read.
        self.recover()
        self.assertFalse(server.scan_issues_get()["blocked"])

    def test_rate_limit_keeps_issue_and_records_failure(self):
        self.run_scan()
        self.fake.clean_failure = server.bili_api.RateLimitedError("风控，请稍后重试")
        self.recover("clean", True)
        self.assertTrue(server.scan_issues_get()["blocked"])
        self.assertIn("风控", store.load_scan_issues()["10"]["last_error"])
        self.assertFalse(store.load_scan_issues()["10"]["cleanup_unknown"])

    def test_network_error_does_not_become_hidden_invalid_diagnosis(self):
        self.fake.failure = server.bili_api.BiliApiError("网络异常")
        self.run_scan()
        self.assertEqual([], server.scan_issues_get()["issues"])
        self.assertIn("网络异常", server.APP["scan_run"]["error"])
        self.assertNotIn("clean", self.fake.calls)

    def test_duplicate_resources_are_reported_separately(self):
        self.fake.count = 31
        self.fake.duplicate = True
        self.run_scan()
        issue = server.scan_issues_get()["issues"][0]
        self.assertEqual((32, 31), (issue["fetched_count"], issue["unique_count"]))
        self.assertTrue(server.scan_issues_get()["blocked"])

    def test_business_apis_are_blocked_including_delete_only_execution(self):
        self.run_scan()
        for path in ("/api/analyze/start", "/api/apply/start", "/api/folder-organize/start",
                     "/api/folder-organize/suggest", "/api/folder-profiles/generate", "/api/plan/apply"):
            with self.subTest(path=path):
                response = self.client.post(path, json={})
                self.assertEqual(409, response.status_code)
                self.assertEqual("scan_issue_pending", response.json()["code"])
        self.assertEqual(200, self.client.get("/api/scan/tree").status_code)
        self.assertFalse(server._organization_profile_readiness()["ready"])

    def test_legacy_inconsistent_state_is_available_in_popup(self):
        store.save_folders(self.fake.list_folders())
        store.begin_folder_scan("old", "10", 32, "paged")
        store.update_folder_scan_progress("10", 31)
        store.mark_folder_scan("10", "inconsistent", "旧版数量不一致")
        self.assertEqual(32, server.scan_issues_get()["issues"][0]["expected_count"])
        self.fake.count = 31
        self.recover()
        self.assertFalse(server.scan_issues_get()["blocked"])

    def test_other_running_job_prevents_recovery(self):
        self.run_scan()
        server.APP["apply_run"] = {"running": True}
        result = server.scan_issue_recover("10", server.ScanRecoveryIn(action="clean", confirmed=True))
        self.assertEqual(409, result.status_code)
        self.assertNotIn("clean", self.fake.calls)

    def test_scan_reserves_running_state_before_starting_worker(self):
        with patch.object(server.threading, "Thread"):
            first = server.scan(server.ScanIn(folder_ids=["10"]))
            self.assertTrue(first["started"])
            self.assertTrue(server.APP["scan_run"]["running"])
            second = server.scan(server.ScanIn(folder_ids=["10"]))
            self.assertEqual(409, second.status_code)

    def test_index_fallback_discards_partial_index_results(self):
        self.fake.count = 2
        self.fake.readable = 1
        self.fake.get_folder_resource_ids = lambda *args, **kwargs: [
            {"type": 2, "id": "1"}, {"type": 2, "id": "2"}]
        self.fake.get_resource_infos_bulk = lambda *args, **kwargs: [
            {"type": 2, "id": "1", "bvid": "BVindexOnly", "title": "索引资源"}]
        self.run_scan()
        issue = server.scan_issues_get()["issues"][0]
        self.assertEqual((1, 1), (issue["fetched_count"], issue["unique_count"]))
        self.assertTrue(server.scan_issues_get()["blocked"])

    def test_resolving_one_folder_does_not_unblock_other_issue(self):
        self.run_scan()
        store.update_scan_issue("20", title="另一个收藏夹", status="pending",
                                expected_count=2, fetched_count=1, unique_count=1)
        self.fake.count = 31
        self.recover()
        pending = server.scan_issues_get()["issues"]
        self.assertEqual(["20"], [row["media_id"] for row in pending])
        self.assertTrue(server.scan_issues_get()["blocked"])


if __name__ == "__main__":
    unittest.main()
