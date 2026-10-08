"""更新流程的守卫：版本比较、用户确认后才安装、随时可取消、收尾清理与失败可见性。

这里是服务端行为；前端「确认框 / 停止下载」按钮由 Playwright 端到端验证。
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import hashlib
from pathlib import Path
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 只在自己最先导入 server 时隔离数据目录；若别的测试已导入则复用，不改变其行为。
if "server" not in sys.modules:
    TEST_ROOT = Path(__file__).resolve().parent / ".test-data" / "update-flow"
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    os.environ["BILI_FAV_ORGANIZER_DATA_DIR"] = str(TEST_ROOT)
    os.environ["LOCALAPPDATA"] = str(TEST_ROOT)
    settings = TEST_ROOT / "BiliFavOrganizer"
    settings.mkdir(exist_ok=True)
    for filename in ("config.json", "secrets.json"):
        (settings / filename).write_text("{}", encoding="utf-8")
    with sqlite3.connect(TEST_ROOT / "library.sqlite3") as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations"
                     "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(6, 'test')")

import server
from fastapi.testclient import TestClient

FAKE_RELEASE = {
    "version": "v9.9.9",
    "release_url": "https://github.com/Martin-soaring-dev/bili-fav-organizer/releases/tag/v9.9.9",
    "zip_name": "x.zip",
    "zip_url": "https://example.invalid/x.zip",
    "checksum_url": "https://example.invalid/x.zip.sha256",
    "setup_name": "BiliFavOrganizer-Setup-v9.9.9.exe",
    "setup_url": "https://example.invalid/BiliFavOrganizer-Setup-v9.9.9.exe",
    "setup_checksum_url": "https://example.invalid/BiliFavOrganizer-Setup-v9.9.9.exe.sha256",
    "zip_size": 0,
}


class VersionComparisonTests(unittest.TestCase):
    def test_newer_release_is_detected(self):
        self.assertTrue(server._is_newer_version("v0.207", "v0.206"))
        self.assertFalse(server._is_newer_version("v0.206", "v0.207"))
        self.assertFalse(server._is_newer_version("v0.206", "v0.206"))


class UpdateCheckTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.addCleanup(setattr, server, "_UPDATE_RELEASE", None)

    def test_dev_build_never_reports_an_available_update(self):
        """开发构建（dev）解析不出版本号：不比较、不提示、更不能开始下载。"""
        with patch.object(server, "_fetch_latest_release", return_value=dict(FAKE_RELEASE)):
            body = self.client.post("/api/update/check").json()
        self.assertEqual("dev", body["current_version"])
        self.assertFalse(body["version_comparable"])
        self.assertFalse(body["update_available"])
        self.assertIsNone(server._UPDATE_RELEASE,
                          "dev 构建被标记为可更新时，一点检查就会下载并覆盖当前程序")

    def test_release_build_still_offers_the_newer_version(self):
        with patch.object(server, "_fetch_latest_release", return_value=dict(FAKE_RELEASE)), \
                patch.object(server, "BUILD_VERSION", "v0.206"):
            body = self.client.post("/api/update/check").json()
        self.assertTrue(body["version_comparable"])
        self.assertTrue(body["update_available"])
        self.assertEqual("v9.9.9", body["latest_version"])
        self.assertIsNotNone(server._UPDATE_RELEASE)


class UpdateInstallGuardTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.addCleanup(setattr, server, "_UPDATE_RELEASE", None)

    def test_dev_build_refuses_in_app_update(self):
        """即使前端被绕过，服务端也不给开发构建做覆盖更新。"""
        with patch.object(server.sys, "frozen", True, create=True):
            response = self.client.post("/api/update/install")
        self.assertEqual(400, response.status_code)
        self.assertIn("开发构建", response.json()["detail"])


class UpdateCancelTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.state_snapshot = dict(server.UPDATE_STATE)
        self.addCleanup(server.UPDATE_STATE.update, self.state_snapshot)
        self.addCleanup(server._UPDATE_CANCEL.clear)

    def test_cancel_without_a_download_is_rejected(self):
        server.UPDATE_STATE.update(status="idle", error="")
        self.assertEqual(409, self.client.post("/api/update/cancel").status_code)

    def test_cancel_during_download_stops_the_worker(self):
        server._UPDATE_CANCEL.clear()
        server.UPDATE_STATE.update(status="downloading", error="")
        response = self.client.post("/api/update/cancel")
        self.assertEqual(200, response.status_code)
        self.assertTrue(server._UPDATE_CANCEL.is_set(), "下载循环依赖这个标志中止")
        self.assertEqual("cancelled", server.UPDATE_STATE["status"])

    def test_cancel_after_replacement_started_is_refused(self):
        server.UPDATE_STATE.update(status="installing", error="")
        response = self.client.post("/api/update/cancel")
        self.assertEqual(409, response.status_code)
        self.assertIn("无法中止", response.json()["error"])


class InstallModeTests(unittest.TestCase):
    def test_missing_registry_marker_means_portable(self):
        """没有安装包写入的注册表标记时，必须按便携版处理。"""
        import winreg
        with patch.object(winreg, "OpenKey", side_effect=OSError("no key")):
            self.assertIsNone(server._installed_info())

    def test_version_endpoint_reports_install_mode(self):
        client = TestClient(server.app)
        with patch.object(server, "_installed_info", return_value=None):
            self.assertEqual("portable", client.get("/api/version").json()["install_mode"])
        with patch.object(server, "_installed_info",
                          return_value={"install_dir": r"C:\x", "scope": "current-user"}):
            self.assertEqual("installed", client.get("/api/version").json()["install_mode"])

    def test_update_check_reports_install_mode(self):
        with patch.object(server, "_fetch_latest_release", return_value=dict(FAKE_RELEASE)), \
                patch.object(server, "BUILD_VERSION", "v0.206"), \
                patch.object(server, "_installed_info",
                             return_value={"install_dir": r"C:\x", "scope": "current-user"}):
            body = TestClient(server.app).post("/api/update/check").json()
        self.assertEqual("installed", body["install_mode"])


class InstalledModeUpdateTests(unittest.TestCase):
    """安装版必须走安装包更新；便携版仍走 ZIP 覆盖。"""

    def setUp(self):
        self.client = TestClient(server.app)
        self.state_snapshot = dict(server.UPDATE_STATE)
        self.addCleanup(server.UPDATE_STATE.update, self.state_snapshot)
        self.addCleanup(setattr, server, "_UPDATE_RELEASE", None)

    def _install_and_wait(self, calls):
        server._UPDATE_RELEASE = dict(FAKE_RELEASE)
        with patch.object(server.sys, "frozen", True, create=True), \
                patch.object(server, "BUILD_VERSION", "v0.206"):
            response = self.client.post("/api/update/install")
        deadline = time.monotonic() + 3
        while not calls and time.monotonic() < deadline:
            time.sleep(0.05)
        return response

    def test_installed_build_downloads_and_runs_the_installer(self):
        calls = []
        with patch.object(server, "_installed_info",
                          return_value={"install_dir": r"C:\x", "scope": "current-user"}), \
                patch.object(server, "_run_installer_update_worker",
                             side_effect=lambda rel, info: calls.append(
                                 ("installer", rel["version"], info["scope"]))), \
                patch.object(server, "_run_update_worker",
                             side_effect=lambda rel: calls.append(("zip", rel["version"]))):
            response = self._install_and_wait(calls)
        self.assertEqual(200, response.status_code)
        self.assertEqual("installed", response.json()["install_mode"])
        self.assertEqual([("installer", "v9.9.9", "current-user")], calls)

    def test_portable_build_keeps_the_zip_worker(self):
        calls = []
        with patch.object(server, "_installed_info", return_value=None), \
                patch.object(server, "_run_installer_update_worker",
                             side_effect=lambda rel, info: calls.append(("installer", rel["version"]))), \
                patch.object(server, "_run_update_worker",
                             side_effect=lambda rel: calls.append(("zip", rel["version"]))):
            response = self._install_and_wait(calls)
        self.assertEqual(200, response.status_code)
        self.assertEqual("portable", response.json()["install_mode"])
        self.assertEqual([("zip", "v9.9.9")], calls)


class UpdateCleanupTests(unittest.TestCase):
    """更新收尾清理与失败可见性：不能留下上百 MB 临时目录，也不能把错误吞掉。"""

    def test_sweep_removes_only_stale_leftovers(self):
        with tempfile.TemporaryDirectory() as base:
            app_parent = Path(base) / "app-parent"
            temp_dir = Path(base) / "sys-temp"
            app_parent.mkdir()
            temp_dir.mkdir()
            stale_update = app_parent / ".bfo-update-stale"
            fresh_update = app_parent / ".bfo-update-fresh"
            # 备份目录名是「应用目录名.previous-随机」，与助手脚本一致
            stale_backup = app_parent / f"{server.APP_DIR.name}.previous-abc"
            stale_setup = temp_dir / ".bfo-setup-stale"
            unrelated = app_parent / "unrelated-dir"
            for path in (stale_update, fresh_update, stale_backup, stale_setup, unrelated):
                path.mkdir()
            old = time.time() - 3 * 3600
            for path in (stale_update, stale_backup, stale_setup):
                os.utime(path, (old, old))

            removed = server._cleanup_stale_update_leftovers(app_parent, temp_dir,
                                                             min_age_hours=2.0)

            self.assertEqual(3, removed)
            self.assertFalse(stale_update.exists(), "超时的便携更新目录要删掉")
            self.assertFalse(stale_backup.exists(), "超时的旧程序备份要删掉")
            self.assertFalse(stale_setup.exists(), "超时的安装版更新目录要删掉")
            self.assertTrue(fresh_update.exists(), "正在进行的更新目录不能误删")
            self.assertTrue(unrelated.exists(), "模式外的目录不能动")

    def test_failure_is_exposed_from_fixed_file_and_cleared_by_next_check(self):
        client = TestClient(server.app)
        server._write_update_error("测试用失败原因")
        self.addCleanup(lambda: server.UPDATE_ERROR_FILE.unlink(missing_ok=True))

        state = client.get("/api/update/status").json()
        self.assertEqual("error", state["status"])
        self.assertIn("测试用失败原因", state["error"],
                      "失败原因写在固定文件里，重启后也要能显示")

        with patch.object(server, "_fetch_latest_release", return_value=dict(FAKE_RELEASE)), \
                patch.object(server, "BUILD_VERSION", "v0.206"):
            body = client.post("/api/update/check").json()
        self.assertTrue(body["update_available"])
        self.assertFalse(server.UPDATE_ERROR_FILE.exists(), "检查更新成功后应清掉旧失败记录")


class EventStreamShutdownTests(unittest.TestCase):
    """SSE 长连接不能拖住退出——否则替换程序时旧进程不退出，表现为「更新卡住」。"""

    def test_event_stream_ends_once_shutdown_is_set(self):
        client = TestClient(server.app)
        server._SHUTDOWN.set()
        self.addCleanup(server._SHUTDOWN.clear)
        result = {}

        def read_stream():
            try:
                with client.stream("GET", "/api/events/stream?since=0") as response:
                    for _ in response.iter_lines():
                        pass
                result["ended"] = True
            except Exception as exc:  # noqa: BLE001 - 记录任何异常交给断言判断
                result["error"] = repr(exc)

        reader = threading.Thread(target=read_stream, daemon=True)
        reader.start()
        reader.join(timeout=10)
        self.assertFalse(reader.is_alive(), "SSE 流没有在 10 秒内结束：退出会被长连接拖住")
        self.assertNotIn("error", result, result.get("error"))
        self.assertTrue(result.get("ended"), "SSE 流应以正常结束返回")


class _FakeResponse:
    """只提供 _download_release_asset 用到的那几个接口。"""

    def __init__(self, chunks, status_code, content_length):
        self._chunks = chunks
        self.status_code = status_code
        self.headers = {"Content-Length": str(content_length)}

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=0):
        for chunk in self._chunks:
            yield chunk

    def close(self):
        return None


class DownloadResumeTests(unittest.TestCase):
    """下载被掐断时要续传，而不是整次失败（国内代理访问 GitHub 资源常断）。"""

    def setUp(self):
        self.snapshot = dict(server.UPDATE_STATE)
        self.addCleanup(server.UPDATE_STATE.update, self.snapshot)

    def _run(self, fake_get, payload):
        with tempfile.TemporaryDirectory() as base:
            dest = Path(base) / "asset.bin"
            expected = hashlib.sha256(payload).hexdigest()
            with patch.object(server.requests, "get", side_effect=fake_get):
                size = server._download_release_asset("https://example.invalid/asset",
                                                      dest, expected, "测试包",
                                                      attempts=3, retry_delay_seconds=0)
            self.assertEqual(len(payload), size)
            self.assertEqual(expected, hashlib.sha256(dest.read_bytes()).hexdigest())

    def test_resumes_after_broken_connection(self):
        payload = bytes(range(256)) * 4096          # 1 MiB
        cut = 300 * 1024
        offsets = []

        def fake_get(url, stream=False, timeout=None, headers=None):
            rng = (headers or {}).get("Range")
            offset = int(rng.split("=")[1].split("-")[0]) if rng else 0
            offsets.append(offset)
            if offset == 0:
                def chunks():
                    yield payload[:cut]
                    raise requests.exceptions.ChunkedEncodingError(
                        f"Connection broken: IncompleteRead({cut} bytes read, "
                        f"{len(payload) - cut} more expected)")
                return _FakeResponse(chunks(), 200, len(payload))
            rest = payload[offset:]
            return _FakeResponse([rest], 206, len(rest))

        self._run(fake_get, payload)
        self.assertEqual([0, cut], offsets, "第二次必须带 Range 从断点续下")

    def test_restarts_when_server_ignores_range(self):
        payload = bytes(range(256)) * 4096
        cut = 100 * 1024
        offsets = []

        def fake_get(url, stream=False, timeout=None, headers=None):
            rng = (headers or {}).get("Range")
            offsets.append(int(rng.split("=")[1].split("-")[0]) if rng else 0)
            if len(offsets) == 1:
                def chunks():
                    yield payload[:cut]
                    raise requests.exceptions.ChunkedEncodingError("IncompleteRead")
                return _FakeResponse(chunks(), 200, len(payload))
            # 服务器忽略 Range：返回 200 与完整内容，客户端应丢掉残片重下
            return _FakeResponse([payload], 200, len(payload))

        self._run(fake_get, payload)
        self.assertEqual([0, cut], offsets)


if __name__ == "__main__":
    main()
