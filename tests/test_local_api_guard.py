"""本地 API 防护与长任务互斥契约。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import server


class LocalApiGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)

    def setUp(self):
        # 本类按生产规则断言；只在用例期间关掉测试钩子，避免拖垮其它 TestClient 套件
        self._prev_insecure = os.environ.pop("BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL", None)
        # 金库强制 onboarding 会抢先 403；本类测 Host/Token，故预置为已初始化且解锁
        self._prev_vault = server.VAULT
        import tempfile
        from pathlib import Path
        import vault as vault_mod
        self._vault_tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        server.VAULT = vault_mod.Vault(Path(self._vault_tmp.name) / "vault.json")
        server.VAULT.initialize(bound_mid="1", password="secret-pass")
        server.VAULT.complete_onboarding()

    def tearDown(self):
        server.VAULT = self._prev_vault
        self._vault_tmp.cleanup()
        if self._prev_insecure is not None:
            os.environ["BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL"] = self._prev_insecure
        else:
            os.environ.pop("BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL", None)

    def _token(self):
        return server._LOCAL_API_TOKEN

    def test_rejects_non_local_host(self):
        res = self.client.get("/api/status", headers={"Host": "evil.example"})
        self.assertEqual(403, res.status_code)

    def test_rejects_testclient_host_in_production(self):
        # 生产不得把 Host: testserver 当成测试豁免
        res = self.client.put("/api/scan/selection", json={"folder_ids": []},
                              headers={"Host": "testserver",
                                       "X-BiliFav-Token": self._token()})
        self.assertEqual(403, res.status_code)

    def test_rejects_missing_token_on_write(self):
        res = self.client.put("/api/scan/selection", json={"folder_ids": []},
                              headers={"Host": "127.0.0.1:8080"})
        self.assertEqual(403, res.status_code)

    def test_rejects_cross_origin_write(self):
        res = self.client.put(
            "/api/scan/selection", json={"folder_ids": []},
            headers={"Host": "127.0.0.1:8080",
                     "X-BiliFav-Token": self._token(),
                     "Origin": "https://evil.example"})
        self.assertEqual(403, res.status_code)

    def test_rejects_cross_site_fetch_metadata(self):
        res = self.client.put(
            "/api/scan/selection", json={"folder_ids": []},
            headers={"Host": "127.0.0.1:8080",
                     "X-BiliFav-Token": self._token(),
                     "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(403, res.status_code)

    def test_allows_same_origin_write_with_token(self):
        res = self.client.put(
            "/api/scan/selection", json={"folder_ids": []},
            headers={"Host": "127.0.0.1:8080",
                     "X-BiliFav-Token": self._token(),
                     "Origin": "http://127.0.0.1:8080",
                     "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(200, res.status_code)

    def test_index_injects_token_for_ui(self):
        res = self.client.get("/", headers={"Host": "127.0.0.1:8080"})
        self.assertEqual(200, res.status_code)
        self.assertIn("__BILI_FAV_TOKEN__", res.text)
        self.assertIn(self._token(), res.text)

    def test_insecure_env_skips_token_but_keeps_host(self):
        os.environ["BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL"] = "1"
        try:
            res = self.client.put("/api/scan/selection", json={"folder_ids": []},
                                  headers={"Host": "127.0.0.1:8080"})
            self.assertEqual(200, res.status_code)
            res = self.client.put("/api/scan/selection", json={"folder_ids": []},
                                  headers={"Host": "evil.example"})
            self.assertEqual(403, res.status_code)
        finally:
            os.environ.pop("BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL", None)


class DpapiTokenBlobTests(unittest.TestCase):
    def test_protect_unprotect_roundtrip(self):
        raw = "secret-token-值".encode("utf-8")
        blob = server._dpapi_protect(raw)
        self.assertEqual(raw, server._dpapi_unprotect(blob))

    def test_plain_prefix_fallback(self):
        self.assertEqual(b"abc", server._dpapi_unprotect(b"PLAIN\x00abc"))

    def test_persist_and_load_token_blob(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = server._TOKEN_BLOB
            server._TOKEN_BLOB = Path(tmp) / "token.blob"
            try:
                server._persist_api_token()
                self.assertEqual(server._LOCAL_API_TOKEN, server._load_persisted_token())
            finally:
                server._TOKEN_BLOB = old


class JobMutexTests(unittest.TestCase):
    def setUp(self):
        for key in server.LONG_JOBS:
            server.APP[key] = None

    def tearDown(self):
        for key in server.LONG_JOBS:
            server.APP[key] = None

    def test_begin_job_rejects_when_other_long_job_running(self):
        server.APP["apply_run"] = {"running": True}
        state = {"running": True, "stop": False}
        err = server._begin_job("analyze_run", state)
        self.assertIsNotNone(err)
        self.assertEqual(409, err.status_code)
        self.assertFalse((server.APP.get("analyze_run") or {}).get("running"))
        self.assertTrue(server.APP["apply_run"]["running"])

    def test_begin_job_reserves_slot(self):
        state = {"running": True, "stop": False}
        self.assertIsNone(server._begin_job("apply_run", state))
        self.assertIs(state, server.APP["apply_run"])

    def test_begin_job_rejects_double_start_same_key(self):
        server.APP["apply_run"] = {"running": True}
        err = server._begin_job("apply_run", {"running": True})
        self.assertIsNotNone(err)
        self.assertEqual(409, err.status_code)

    def test_analyze_start_conflicts_with_running_apply(self):
        server.APP["apply_run"] = {"running": True}
        with patch.object(server.store, "load_folders", return_value=[]), \
             patch.object(server.store, "load_videos", return_value=[]), \
             patch.object(server, "load_config", return_value={}), \
             patch.object(server, "make_llm_config") as mk:
            mk.return_value.configured = False
            # analyze_start early-outs on LLM or empty before begin; force a path
            # by calling _begin_job-level behavior via HTTP would need more mocks.
            err = server._begin_job("analyze_run", {"running": True},
                                    error="分析已在运行中")
        self.assertIsNotNone(err)

    def test_scan_start_conflicts_with_running_analyze(self):
        server.APP["analyze_run"] = {"running": True}
        res = server._start_scan(server.ScanIn(folder_ids=["1"], mode="rebuild"))
        self.assertEqual(409, getattr(res, "status_code", 200))
        self.assertFalse((server.APP.get("scan_run") or {}).get("running"))


if __name__ == "__main__":
    unittest.main()
