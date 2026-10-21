"""Scan recovery uses synthetic remote responses and a fresh isolated SQLite DB."""
import os
import shutil
import sqlite3
import sys
import threading
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
from bili_api import BiliSession as RealBiliSession, CookieInfo
from core.database import DatabaseNotInitializedError, DatabaseRecoveryRequiredError
from fastapi.testclient import TestClient
# Imported after the data directory is redirected: the helper imports store itself.
from store_profile_isolation import stop_task_scheduler


# 任务不再处于调度器主动执行中的状态。扫描执行体把 running 置为 False 之后，
# 调度器才落终态记录，所以只能等 running 是不够的。
SETTLED_TASK_STATUSES = frozenset({
    server.TaskStatus.PAUSED,
    server.TaskStatus.CANCELLED,
    server.TaskStatus.INTERRUPTED,
    server.TaskStatus.NEEDS_RECONCILIATION,
    server.TaskStatus.COMPLETED,
    server.TaskStatus.FAILED,
})


class FakeSession:
    def __init__(self, count=32, readable=31):
        self.count, self.readable = count, readable
        self.calls = []
        self.failure = None
        self.clean_failure = None
        self.clean_changes_count = True
        self.duplicate = False

    def list_folders(self, *, should_stop=None):
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
        # 上一个用例的调度器线程不能在重建 profile 期间插手，否则它会读到
        # 半初始化的状态；拆目录前也必须先停掉它。
        self.stack.callback(stop_task_scheduler)
        stop_task_scheduler()
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

    def wait_task_settled(self, task_id, statuses=None):
        """等待调度器把任务记录写到终态，返回该记录。

        只等 ``app_state["running"]`` 变 false 会留下竞态：扫描执行体先把 running
        置 false，调度器随后才写终态；测试先拆掉隔离目录的话，后台线程会在已删除
        的路径上继续连接数据库。
        """
        scheduler = server._TASK_SCHEDULER
        self.assertIsNotNone(scheduler, "调度器尚未创建，无法确认任务终态")
        expected = set(SETTLED_TASK_STATUSES if statuses is None else statuses)
        record = scheduler.wait_for_status(task_id, expected, timeout=20)
        self.assertIn(record.status, expected, record)
        return record

    def wait_scan(self, result, statuses=None):
        self.assertIsInstance(result, dict, getattr(result, "body", None))
        self.assertTrue(result["started"])
        # 执行体的 finally 先把 running 置回 false，调度器随后才写终态记录，
        # 所以「记录到终态」本身就蕴含 running 已归位，不必再赌固定秒数轮询。
        settled = self.wait_task_settled(result["task_id"], statuses)
        self.assertFalse(server.APP["scan_run"]["running"])
        self.assertEqual(result["run_id"], server.APP["scan_run"]["id"])
        return settled

    def recover(self, action="retry", confirmed=False):
        result = server.scan_issue_recover("10", server.ScanRecoveryIn(action=action, confirmed=confirmed))
        self.wait_scan(result)

    def start_paused_scan(self):
        """启动扫描并把它停在第一个检查点，返回 (result, task_record)。"""
        entered = threading.Event()
        release = threading.Event()

        def blocking_ids(media_id, expected, **kwargs):
            entered.set()
            assert release.wait(10), "测试没有放行扫描"
            return [{"type": 2, "id": "1"}, {"type": 2, "id": "2"}]

        def bulk(media_id, missing, **kwargs):
            return [{"type": item["type"], "id": item["id"],
                     "bvid": f"BV{item['id']}", "aid": int(item["id"]),
                     "title": f"视频 {item['id']}"} for item in missing]

        self.fake.count = 2
        self.fake.get_folder_resource_ids = blocking_ids
        self.fake.get_resource_infos_bulk = bulk
        result = server.scan(server.ScanIn(folder_ids=["10"], mode="rebuild"))
        self.assertTrue(result["started"], result)
        self.assertTrue(entered.wait(10), "扫描没有读到资源清单")

        scheduler = server._TASK_SCHEDULER
        self.assertIsNotNone(scheduler)
        scheduler.pause_task(result["task_id"])
        release.set()

        record = scheduler.wait_for_status(
            result["task_id"], {server.TaskStatus.PAUSED}, timeout=10)
        self.assertIs(record.status, server.TaskStatus.PAUSED)
        deadline = time.monotonic() + 10
        while server.APP["scan_run"]["running"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(server.APP["scan_run"]["running"])
        return result, record

    def assert_cancelled_everywhere(self, task_id):
        """任务记录和 /api/scan/status 必须同时落到 cancelled，且 error 为空。"""
        status = server.scan_status()
        self.assertEqual("cancelled", status["status"], status)
        self.assertIsNone(status["error"], status)
        record = server._TASK_SCHEDULER.get_task(task_id)
        self.assertIs(record.status, server.TaskStatus.CANCELLED)
        self.assertIsNone(record.error)
        self.assertIsNone(record.pause_reason)
        return record

    def test_paused_scan_cancelled_from_scan_page_keeps_both_states_cancelled(self):
        result, paused = self.start_paused_scan()
        self.assertEqual("paused", server.scan_status()["status"])

        self.assertTrue(server.scan_stop()["ok"])

        self.assert_cancelled_everywhere(result["task_id"])
        # 取消后不能再被当成可恢复任务。
        self.assertNotIn(result["task_id"], server._LEGACY_TASKS)

    def test_paused_scan_cancelled_from_task_center_keeps_both_states_cancelled(self):
        result, paused = self.start_paused_scan()
        emissions = []
        server._TASK_SCHEDULER.subscribe(emissions.append)

        response = server.task_cancel(result["task_id"])
        self.assertTrue(response["ok"], response)
        self.assert_cancelled_everywhere(result["task_id"])

        # 扫描页再补一次取消：状态不变，也不能再发一次取消终态事件。
        server.scan_stop()
        self.assert_cancelled_everywhere(result["task_id"])
        cancel_events = [record for record in emissions
                         if record.task_id == result["task_id"]
                         and record.status is server.TaskStatus.CANCELLED]
        self.assertEqual(1, len(cancel_events))

    def test_task_progress_separates_folder_and_item_counters(self):
        self.fake.count = 0
        self.fake.readable = 0
        result = self.run_scan()

        progress = server._TASK_SCHEDULER.get_task(result["task_id"]).progress
        # 同一行不再只有含义会变的 done/total：收藏夹和条目各算各的。
        self.assertEqual({"done": 1, "total": 1}, progress["folders"])
        self.assertEqual({"done": 0, "total": 0}, progress["items"])
        self.assertNotIn("done", progress)
        self.assertNotIn("total", progress)

    def test_scan_resume_skips_complete_folders_and_never_replays_writes(self):
        calls = []

        class TwoFolderSession(FakeSession):
            def list_folders(self, *, should_stop=None):
                calls.append(("directory", ""))
                return [{"media_id": "10", "title": "完整夹", "count": 1, "attr": 0},
                        {"media_id": "20", "title": "未完成夹", "count": 2, "attr": 0}]

            def get_folder_resource_ids(self, media_id, expected, **kwargs):
                calls.append(("ids", str(media_id)))
                return [{"type": 2, "id": f"{media_id}-{index}"} for index in range(expected)]

            def get_resource_infos_bulk(self, media_id, missing, **kwargs):
                return [{"type": item["type"], "id": item["id"], "aid": 1,
                         "bvid": f"BV{item['id']}", "title": "条目"} for item in missing]

            def iter_folder_videos(self, media_id, expected, **kwargs):
                calls.append(("pages", str(media_id)))
                yield from ()

            def move_batch(self, *args, **kwargs):
                calls.append(("move", ""))

            def batch_delete(self, *args, **kwargs):
                calls.append(("delete", ""))

            def clean_invalid_folder(self, *args, **kwargs):
                calls.append(("clean", ""))

        store.save_folders(TwoFolderSession().list_folders())
        store.save_videos({"BVkeep": {"bvid": "BVkeep", "aid": 7, "folder_ids": ["10"]}})
        store.begin_folder_scan("old-run", "10", 1, "bulk_ids_infos")
        store.finish_folder_scan("old-run", "10", 1, 1)

        session = TwoFolderSession()
        session.calls = calls
        self.stack.enter_context(patch.object(server.bili_api, "BiliSession",
                                             return_value=session))
        result = server.scan(server.ScanIn(mode="resume"))
        self.wait_scan(result)

        self.assertEqual(["20"], [media for kind, media in calls if kind == "ids"])
        self.assertFalse([call for call in calls if call[0] in ("move", "delete", "clean")])
        # 未完成夹重读后补全；完整夹的旧快照没有被丢掉。
        self.assertEqual("complete", store.load_folder_scan_states()["20"]["status"])
        self.assertIn("BVkeep", store.load_videos_raw())

    def test_interrupted_paged_folder_restarts_from_the_first_page(self):
        """恢复后的重读从该收藏夹第 1 页开始，不承诺页级续扫。"""
        reads = []
        holder = {"entered": threading.Event(), "release": threading.Event()}

        class PagedSession(FakeSession):
            def get_folder_resource_ids(self, *args, **kwargs):
                raise server.bili_api.BiliApiError("模拟批量接口失败，回退分页")

            def iter_folder_videos(self, media_id, expected, **kwargs):
                reads.append(0)
                for index in range(expected):
                    if index == 1:
                        # 第 1 页刚读完、第 2 页还没开始：这里暂停最利于验证页游标。
                        holder["entered"].set()
                        assert holder["release"].wait(10), "测试没有放行分页读取"
                    reads[-1] += 1
                    yield {"bvid": f"BVpaged{index}", "aid": index + 1, "type": 2,
                           "title": f"视频 {index}", "source_folder_id": str(media_id)}

        self.fake.count = 4
        session = PagedSession(count=4)
        self.stack.enter_context(patch.object(server.bili_api, "BiliSession",
                                             return_value=session))
        with patch.object(server.bili_api, "PAGE_SIZE", 2):
            result = server.scan(server.ScanIn(folder_ids=["10"], mode="rebuild"))
            self.assertTrue(result["started"], result)
            self.assertTrue(holder["entered"].wait(10), "分页读取没有开始")
            scheduler = server._TASK_SCHEDULER
            scheduler.pause_task(result["task_id"])
            holder["release"].set()
            paused = scheduler.wait_for_status(
                result["task_id"], {server.TaskStatus.PAUSED}, timeout=10)
            self.assertIs(paused.status, server.TaskStatus.PAUSED)
            # 暂停发生在第 2 页之后：页级检查点确实被记下来了。
            self.assertEqual("paged", paused.checkpoint.get("phase"))
            self.assertEqual(2, paused.checkpoint.get("page"))

            holder["entered"].clear()
            deadline = time.monotonic() + 10
            while server.APP["scan_run"]["running"] and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(server.APP["scan_run"]["running"])

            scheduler.resume_task(result["task_id"])
            self.assertTrue(holder["entered"].wait(10), "恢复后没有重新读取")
            holder["release"].set()
            completed = scheduler.wait_for_status(
                result["task_id"], {server.TaskStatus.COMPLETED}, timeout=10)
            self.assertIs(completed.status, server.TaskStatus.COMPLETED)

        # 两次都从头读完整列：恢复没有沿用上一轮的页游标。
        self.assertEqual([2, 4], reads)

    def test_normal_scan_does_not_create_issue(self):
        self.fake.count = 31
        self.run_scan()
        self.assertFalse(server.scan_issues_get()["blocked"])
        self.assertEqual("complete", store.load_folder_scan_states()["10"]["status"])

    def make_documented_bulk_session(self, count, *, omit_last=False):
        """Use the real API parser with synthetic HTTP responses, not a fake scan path."""
        session = RealBiliSession(CookieInfo("synthetic", "synthetic", "1"))
        session._mid = "1"
        session._throttle_read = lambda **kwargs: None
        session.list_folders = lambda **kwargs: [{"media_id": "10", "title": "批量读取测试夹",
                                         "count": count, "attr": 0}]
        calls = []
        options = {"omit_last": omit_last}

        def request(method, url, **kwargs):
            calls.append((method, url, kwargs.get("params")))
            if url.endswith("/resource/ids"):
                payload = [{"id": n, "type": 2, "bv_id": f"BVbulk{n}"}
                           for n in range(1, count + 1)]
            elif url.endswith("/resource/infos"):
                keys = kwargs["params"]["resources"].split(",")
                payload = [{"id": int(key.split(":")[0]), "type": 2,
                            "bv_id": f"BVbulk{key.split(':')[0]}", "title": "已返回明细"}
                           for key in keys
                           if not (options["omit_last"] and key == f"{count}:2")]
            else:
                raise AssertionError(f"正常扫描不能请求分页或额外接口：{url}")
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                                   json=lambda: {"code": 0, "data": payload})

        session.session.request = request
        self.stack.enter_context(patch.object(server.bili_api, "BiliSession", return_value=session))
        return calls, options

    def test_1000_item_scan_uses_ids_once_and_infos_once(self):
        calls, _ = self.make_documented_bulk_session(1000)
        self.run_scan()
        self.assertEqual(2, len(calls))
        self.assertEqual(1000, store.load_folder_item_counts()["10"])
        self.assertEqual("indexed_ids", store.load_folder_scan_states()["10"]["strategy"])
        self.assertFalse(server.scan_issues_get()["blocked"])

    def test_cached_folder_rebuild_reads_only_id_catalog(self):
        calls, _ = self.make_documented_bulk_session(316)
        self.run_scan()
        calls.clear()
        self.run_scan()
        self.assertEqual(1, len(calls))
        self.assertTrue(calls[0][1].endswith("/resource/ids"))

    def test_missing_info_is_retained_without_paging_and_only_it_is_refetched(self):
        calls, options = self.make_documented_bulk_session(316, omit_last=True)
        self.run_scan()
        self.assertEqual(2, len(calls))
        self.assertEqual(316, store.load_folder_item_counts()["10"])
        self.assertEqual({"10": 1}, store.load_metadata_pending_folder_counts())
        self.assertEqual(1, server.scan_tree()["folders"][0]["metadata_pending_count"])
        issue_state = server.scan_issues_get()
        self.assertTrue(issue_state["blocked"])
        self.assertEqual([("metadata_pending", 1)],
                         [(issue["kind"], issue["metadata_pending_count"])
                          for issue in issue_state["issues"]])
        rejected_clean = self.client.post(
            "/api/scan/issues/10/recover",
            json={"action": "clean", "confirmed": True})
        self.assertEqual(400, rejected_clean.status_code)
        blocked = self.client.post("/api/analyze/start", json={})
        self.assertEqual(409, blocked.status_code)
        self.assertEqual("scan_issue_pending", blocked.json()["code"])
        self.assertNotIn("clean", self.fake.calls)
        record = store.load_videos_raw()["BVbulk316"]
        self.assertEqual("unavailable", record["metadata_status"])
        self.assertFalse(server._is_invalid(record))
        total, samples = store.load_folder_profile_samples("10")
        self.assertEqual((316, 315), (total, len(samples)))
        calls.clear()
        options["omit_last"] = False
        # 数量不变且成员快照已完成，也应补查明细，不能永远跳过。
        result = server.scan_issue_recover("10", server.ScanRecoveryIn(action="retry"))
        self.wait_scan(result)
        self.assertEqual(2, len(calls))
        self.assertEqual("316:2", calls[1][2]["resources"])
        self.assertEqual({}, store.load_metadata_pending_folder_counts())
        self.assertNotIn("metadata_status", store.load_videos_raw()["BVbulk316"])
        self.assertFalse(server.scan_issues_get()["blocked"])
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
