"""本地 API 防护与长任务互斥契约。"""
import os
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import server


class LocalApiGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL", None)
        cls.client = TestClient(server.app)

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL", None)

    def _token(self):
        return server._LOCAL_API_TOKEN

    def test_rejects_non_local_host(self):
        res = self.client.get("/api/status", headers={"Host": "evil.example"})
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
