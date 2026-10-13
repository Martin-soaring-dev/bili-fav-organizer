"""Beta 阻断项的前端回归：真实 Chromium + 隔离 SQLite，全程不碰真实 B 站。

这些修复（配置请求循环、扫描异常弹窗被事件重新打开、空核对记录被当成已核对、
任务卡片进度语义与检查点展示、快速模式只认扫描成功终态）都发生在 static/app.js
的交互里，只跑 Python 单测证明不了，所以这里起一个真实服务 + 真实浏览器来驱动。

Playwright 或浏览器不可用时整类跳过：CI 只装 requirements-test.txt，装不上浏览器，
跳过比让流水线变红更诚实。
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:  # pragma: no cover - 只在没装 playwright 的环境命中
    PlaywrightTimeoutError = Exception
    sync_playwright = None
    HAS_PLAYWRIGHT = False


BOOTSTRAP = r'''
"""隔离数据目录 + 假 B 站会话 + 预置异常/任务/远端操作，然后跑真实 uvicorn。"""
import json
import os
import sys

import store

store.initialize_new_database()

from core.remote_operations import OperationIntent, SqliteRemoteOperationRepository
from core.scheduler import SQLiteTaskStore, TaskRecord, TaskStatus

# 一个数量对不上的收藏夹：扫描异常弹窗与横幅必须有内容。
store.save_folders([{"media_id": "10", "title": "异常收藏夹", "count": 5, "attr": 0}])
store.begin_folder_scan("seed-run", "10", 5, "paged")
store.update_folder_scan_progress("10", 4)
store.mark_folder_scan("10", "inconsistent", "数量不一致")

task_store = SQLiteTaskStore.from_lifecycle(store._lifecycle())
task_store.create(TaskRecord(
    task_type="scan", status=TaskStatus.PAUSED,
    checkpoint={"version": 1, "phase": "paged", "folder_id": "10", "page": 2, "done": 30},
    progress={"folders": {"done": 1, "total": 2}, "items": {"done": 30, "total": 60}},
))
task_store.create(TaskRecord(
    task_type="analyze", status=TaskStatus.PAUSED,
    progress={"done": 3, "total": 10, "current": "BVlegacy"},
))
task_store.create(TaskRecord(
    task_type="apply", status=TaskStatus.PAUSED,
    checkpoint={"version": 1, "phase": None, "page": "abc", "done": "未知"},
    progress={"folders": "坏数据"},
))

connection = store._connect(owner="ui-regression-seed")
try:
    repository = SqliteRemoteOperationRepository(connection)

    def seed(account_id, operation_type):
        # operation_id 由意图稳定派生，所以用不同的 operation_type 区分两张卡片。
        intent = OperationIntent(
            task_id="ui-task", account_id=account_id, operation_type=operation_type,
            target={"media_id": "10"}, entity_ids=["1"])
        repository.prepare(intent)
        repository.begin_dispatch(intent.operation_id)
        repository.mark_needs_reconciliation(intent.operation_id, reason_code="timeout")
        return intent.operation_id

    seed("ui-account-1", "ui_probe_empty")
    recorded_id = seed("ui-account-2", "ui_probe_recorded")
    recorded = repository.record_reconciliation(
        recorded_id, decision="not_applied", preconditions_valid=True,
        evidence={"user_note": "已核对 B 站页面，原操作未生效"})
finally:
    connection.close()

import server

# 完成保险库引导，否则前端会一直盖着引导弹窗，点不到后面的界面。
server.VAULT.initialize(bound_mid="12345", password="ui-regression-pass")
server.VAULT.complete_onboarding()

# 首启会把配置里的 base_url/model 迁成默认供应商+模型；这里要的是真正的
# 「没有模型配置」，所以把它删空，才能复现 /api/config 被反复请求的条件。
for seeded_provider in store.list_providers():
    store.delete_provider_models(seeded_provider["id"])
    store.delete_provider(seeded_provider["id"])


def load_config():
    return {"scan_interval": 2, "write_interval": 2}


class FakeSession:
    read_interval = 2
    write_interval = 2

    def get_mid(self):
        return "12345"

    def list_folders(self):
        return [{"media_id": "10", "title": "异常收藏夹", "count": 5, "attr": 0}]

    def get_folder_resource_ids(self, media_id, expected, **kwargs):
        raise server.bili_api.BiliApiError("模拟批量接口失败，回退分页")

    def iter_folder_videos(self, media_id, expected, **kwargs):
        # 只给出 4 条，数量对不上，所以每次扫描都会保留异常并发出 scan_idle。
        for index in range(4):
            yield {"bvid": f"BVui{index}", "aid": index + 1, "type": 2,
                   "title": f"视频 {index}", "source_folder_id": str(media_id)}


server.load_config = load_config
server.get_session_cookie = lambda: "ui-regression-cookie"
server.bili_api.BiliSession = lambda cookie: FakeSession()

import uvicorn

print("SEEDED", json.dumps({"checked_at": recorded.reconciliation["checked_at"]}), flush=True)
uvicorn.run(server.app, host="127.0.0.1", port=int(os.environ["BFO_UI_PORT"]),
            log_level="warning")
'''


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@unittest.skipUnless(HAS_PLAYWRIGHT, "需要 playwright（CI 只装 requirements-test.txt）")
class BetaUiRegressionTests(unittest.TestCase):
    """整类共用一个隔离服务与浏览器，避免每个用例重复冷启动。"""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="bfo-ui-regression-",
                                                   ignore_cleanup_errors=True)
        root = Path(cls.temp_dir.name)
        profile = root / "profile"
        home = root / "home"
        for path in (profile, home, profile / "BiliFavOrganizer"):
            path.mkdir(parents=True, exist_ok=True)
        bootstrap = root / "bootstrap.py"
        bootstrap.write_text(textwrap.dedent(BOOTSTRAP), encoding="utf-8")
        cls.port = free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}/"
        env = os.environ.copy()
        env.update({
            "LOCALAPPDATA": str(profile),
            "USERPROFILE": str(profile),
            "HOME": str(home),
            "BILI_FAV_ORGANIZER_DATA_DIR": str(profile / "BiliFavOrganizer" / "data"),
            "BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL": "1",
            "BFO_UI_PORT": str(cls.port),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        })
        cls.server_log = root / "server.log"
        cls.log_handle = cls.server_log.open("w", encoding="utf-8")
        cls.process = subprocess.Popen(
            [sys.executable, str(bootstrap)], cwd=REPOSITORY_ROOT, env=env,
            stdout=cls.log_handle, stderr=subprocess.STDOUT, text=True)
        try:
            cls._wait_for_server(profile)
        except Exception:
            output = cls._server_output()
            cls._shutdown_server()
            raise AssertionError(f"测试服务未能启动：\n{output}") from None

        cls.playwright = None
        cls.browser = None
        try:
            cls.playwright = sync_playwright().start()
            cls.browser = cls.playwright.chromium.launch()
        except Exception as exc:  # 浏览器没装/启动不了：跳过而不是失败
            cls._teardown_browser()
            cls._shutdown_server()
            raise unittest.SkipTest(f"无法启动 Chromium：{exc}") from exc

    @classmethod
    def _shutdown_server(cls):
        process = getattr(cls, "process", None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except Exception:
                process.kill()
                try:
                    process.wait(timeout=15)
                except Exception:
                    pass
        if getattr(cls, "log_handle", None) is not None:
            cls.log_handle.close()

    @classmethod
    def _wait_for_server(cls, profile):
        deadline = time.monotonic() + 90
        url = f"http://127.0.0.1:{cls.port}/api/status"
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                raise AssertionError(
                    "测试服务提前退出：\n" + cls._server_output())
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(0.25)
        raise AssertionError(
            f"测试服务 90 秒内未就绪：\n{cls._server_output()}")

    @classmethod
    def _server_output(cls) -> str:
        cls.log_handle.flush()
        try:
            return cls.server_log.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            return "<日志不可读>"

    @classmethod
    def _teardown_browser(cls):
        if cls.browser is not None:
            try:
                cls.browser.close()
            except Exception:
                pass
            cls.browser = None
        if cls.playwright is not None:
            try:
                cls.playwright.stop()
            except Exception:
                pass
            cls.playwright = None

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "browser", None) is not None:
            cls._teardown_browser()
        cls._shutdown_server()
        if getattr(cls, "temp_dir", None) is not None:
            cls.temp_dir.cleanup()

    def open_app(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        self.page_errors = []
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        # 必须在 goto 之前挂上，否则漏掉首屏加载期间发出的请求。
        self.config_requests = []
        page.on("request", lambda request: self.config_requests.append(request.url)
                if "/api/config" in request.url else None)
        page.goto(self.base_url, wait_until="load")
        return page

    def dismiss_scan_issue_popup(self, page):
        """关掉进入页面时自动弹出的扫描异常提示，好去点后面的界面。"""
        close = page.locator("#scan-issue-close")
        try:
            close.wait_for(state="visible", timeout=5000)
        except PlaywrightTimeoutError:
            return
        close.click()
        page.locator("#scan-issue-modal").wait_for(state="hidden", timeout=5000)

    def scan_status(self, page):
        return page.evaluate(
            "async () => (await fetch('/api/scan/status')).json()")

    def install_fast_mode_stubs(self, page, scan_status, analyze_calls):
        """接管快速模式链路用到的接口，只把扫描终态留给用例决定。

        终态是这条回归唯一要变的输入，其余接口一律给成功响应，否则用例会先在
        「刷新目录」之类的步骤上失败，测不到「等待扫描完成」的判断。
        """
        preset = {
            "/api/login/status": {"configured": True, "logged_in": True},
            "/api/config": {"active_model_id": "m1", "scan_interval": 2,
                            "write_interval": 2},
            "/api/status": {"scan": {"running": False}, "analyze": {"running": False},
                            "apply": {"running": False}},
            "/api/folder-profiles/status": {"running": False},
            # 一个可选中的 active 收藏夹：没有可选项时画像步骤会先报错，
            # 用例就到不了后面的分析阶段。
            "/api/folder-profiles": {
                "folders": [{"media_id": "10", "title": "夹", "count": 1,
                             "local_count": 1, "is_default": False,
                             "profile_state": "missing"}],
                "run": {"running": False}},
            "/api/organization/readiness": {"ready": True, "missing": [],
                                            "current_count": 1,
                                            "required_count": 1},
            "/api/folder-organize": {"ai_run": {"running": False},
                                     "run": {"running": False}},
            "/api/folders": {"folders": [{"media_id": "10", "title": "夹", "count": 1}],
                             "selected_ids": ["10"]},
            "/api/folders/refresh": {"ok": True},
            "/api/scan": {"ok": True, "started": True, "run_id": "stub-run",
                          "task_id": "stub-task"},
            "/api/scan/issues": {"issues": [], "running": False},
            "/api/analyze/status": {"running": False, "done": 1, "total": 1},
        }

        def fulfill(route, payload):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(payload))

        def handler(route):
            path = urlparse(route.request.url).path
            if path == "/api/analyze/start":
                analyze_calls.append(route.request.url)
                return fulfill(route, {"ok": True, "total": 0})
            if path == "/api/scan/status":
                return fulfill(route, dict(scan_status))
            return fulfill(route, preset.get(path, {"ok": True}))

        page.route("**/api/**", handler)

    def start_fast_content_mode(self, page):
        """选「内容整理」并启动：只有它会走到分析阶段。"""
        self.dismiss_scan_issue_popup(page)
        page.evaluate("""() => {
            const el = document.querySelector('input[name="fast-biz"][value="content"]');
            el.checked = true;
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }""")
        page.click("#fast-run")

    def assert_fast_run_stopped_on_scan(self, scan_status, expected_text):
        """扫描终态不是成功时：停下、说明原因，且绝不进入分析阶段。"""
        page = self.open_app()
        analyze_calls = []
        self.install_fast_mode_stubs(page, scan_status, analyze_calls)
        self.start_fast_content_mode(page)
        page.wait_for_function(
            "expected => document.getElementById('fast-status').textContent"
            ".includes(expected)",
            arg=expected_text, timeout=30000)
        # 给残留的部分扫描结果留出「被继续处理」的时间窗，然后确认没有发生。
        page.wait_for_timeout(3000)
        self.assertEqual([], analyze_calls,
                         "扫描未成功结束时不得进入分析阶段")
        self.assertEqual(1, page.locator("#fast-steps .step-fail").count())
        self.assertEqual([], self.page_errors)

    def test_fast_mode_stops_after_paused_scan_instead_of_analyzing(self):
        self.assert_fast_run_stopped_on_scan(
            {"running": False, "status": "paused", "error": None},
            "扫描已暂停")

    def test_fast_mode_stops_after_cancelled_scan_instead_of_analyzing(self):
        # 取消终态带 null error：旧实现只看 running/error，会把它当成扫描完成。
        self.assert_fast_run_stopped_on_scan(
            {"running": False, "status": "cancelled", "error": None},
            "扫描已取消")

    def test_fast_mode_still_continues_after_completed_scan(self):
        page = self.open_app()
        analyze_calls = []
        self.install_fast_mode_stubs(
            page, {"running": False, "status": "completed", "error": None},
            analyze_calls)
        self.start_fast_content_mode(page)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not analyze_calls:
            page.wait_for_timeout(250)
        self.assertEqual(1, len(analyze_calls),
                         "扫描正常完成后仍应继续进入分析阶段")
        self.assertEqual([], self.page_errors)

    def run_scan(self, page, timeout=45):
        page.evaluate(
            """async () => {
                await fetch('/api/scan', {method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({folder_ids: ['10'], mode: 'rebuild'})});
            }""")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.scan_status(page)
            if status.get("running") is False:
                return status
            page.wait_for_timeout(200)
        self.fail(f"扫描未在 {timeout} 秒内结束：{status}")

    def test_empty_model_config_does_not_storm_the_config_endpoint(self):
        page = self.open_app()
        requests = self.config_requests

        # 空模型配置是这条回归的关键条件：配置读成功、但模型列表是空的。
        page.wait_for_function(
            "() => document.getElementById('lamp-model-text').textContent !== '未测试'",
            timeout=20000)
        self.assertIn("未选择模型", page.inner_text("#lamp-model-text"))

        # 启动阶段允许几次正常的补拉。
        page.wait_for_timeout(5000)
        settled = len(requests)
        self.assertLessEqual(settled, 8, f"启动阶段 /api/config 请求异常：{len(requests)} 次")

        # 稳态：旧实现在这里会成串刷请求（每一轮都会互相触发一次）。
        page.wait_for_timeout(6000)
        self.assertLessEqual(len(requests) - settled, 2,
                            f"稳态仍在持续请求 /api/config：{len(requests)} 次")
        page.wait_for_timeout(6000)
        self.assertLessEqual(len(requests) - settled, 3,
                            f"稳态仍在持续请求 /api/config：{len(requests)} 次")
        self.assertEqual([], self.page_errors)

    def test_closed_scan_issue_popup_is_not_reopened_by_scan_idle(self):
        page = self.open_app()
        modal = page.locator("#scan-issue-modal")
        modal.wait_for(state="visible", timeout=20000)
        page.click("#scan-issue-close")
        modal.wait_for(state="hidden", timeout=5000)
        self.assertTrue(page.locator("#scan-issue-banner").is_visible())

        # 每次扫描结束都会发 scan_idle；关闭后旧异常不得再抢占界面。
        for _ in range(2):
            self.run_scan(page)
            page.wait_for_timeout(1000)
            self.assertEqual("none", modal.evaluate("el => el.style.display"))
        self.assertTrue(page.locator("#scan-issue-banner").is_visible())

        # 用户仍能主动重新查看。
        page.click("#scan-issue-open")
        modal.wait_for(state="visible", timeout=5000)
        self.assertEqual([], self.page_errors)

    def test_task_center_separates_progress_units_and_shows_checkpoint(self):
        page = self.open_app()
        self.dismiss_scan_issue_popup(page)
        page.click("#rail-tasks")
        tasks = page.locator("#task-center-tasks")
        tasks.wait_for(state="visible", timeout=15000)
        # 面板先出现、数据后到：等第一张卡片渲染出来再断言文案。
        tasks.locator("article").first.wait_for(state="visible", timeout=20000)
        text = tasks.inner_text()

        # 收藏夹进度与条目进度各算各的，不再共用一行会变含义的 done/total。
        self.assertIn("收藏夹 1/2", text)
        self.assertIn("条目 30/60", text)
        self.assertIn("检查点：", text)
        self.assertIn("第 2 页", text)
        # 早期只有 done/total 的任务记录照旧显示，不报错。
        self.assertIn("3/10", text)
        # 字段缺失/类型不对的检查点不能让页面崩。
        self.assertEqual([], self.page_errors)

    def test_operations_tab_does_not_invent_a_reconciliation_record(self):
        page = self.open_app()
        self.dismiss_scan_issue_popup(page)
        page.click("#rail-tasks")
        page.click('[data-task-center-tab="operations"]')
        operations = page.locator("#task-center-operations")
        operations.wait_for(state="visible", timeout=15000)

        empty_card = operations.locator("article", has_text="ui_probe_empty")
        empty_card.wait_for(state="visible", timeout=15000)
        # 没有核对记录时服务端也会返回 {}，不能再显示成「已记录核对」。
        self.assertNotIn("上次核对", empty_card.inner_text())

        recorded_card = operations.locator("article", has_text="ui_probe_recorded")
        recorded_text = recorded_card.inner_text()
        self.assertIn("上次核对：已确认未生效", recorded_text)
        self.assertIn("已核对 B 站页面，原操作未生效", recorded_text)
        self.assertRegex(recorded_text, re.compile(r"\d{4}-\d{2}-\d{2}"))
        self.assertEqual([], self.page_errors)


if __name__ == "__main__":
    unittest.main()
