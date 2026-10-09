"""应用密码：PBKDF2 校验、锁定拦截、DPAPI 自动解锁。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import server


class AppLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL", "1")
        cls.client = TestClient(server.app)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._old = (server.SECRETS_FILE, server.APP_UNLOCK_BLOB)
        root = Path(self._tmp.name)
        server.SECRETS_FILE = root / "secrets.json"
        server.APP_UNLOCK_BLOB = root / "app_unlock.blob"
        server._APP_LOCK.update(enabled=False, unlocked=True, failed_attempts=0)

    def tearDown(self):
        server.SECRETS_FILE, server.APP_UNLOCK_BLOB = self._old
        server._APP_LOCK.update(enabled=False, unlocked=True, failed_attempts=0)
        self._tmp.cleanup()

    def test_status_unlocked_without_password(self):
        res = self.client.get("/api/app-lock/status")
        self.assertEqual(200, res.status_code)
        body = res.json()
        self.assertFalse(body["password_set"])
        self.assertTrue(body["unlocked"])

    def test_set_password_locks_and_unlocks(self):
        res = self.client.post("/api/app-lock/set-password",
                               json={"password": "secret12", "auto_unlock": False})
        self.assertEqual(200, res.status_code)
        self.assertTrue(res.json()["password_set"])
        self.assertTrue(res.json()["unlocked"])

        self.assertTrue(self.client.post("/api/app-lock/lock").json()["password_set"])
        self.assertFalse(server._APP_LOCK["unlocked"])

        # 锁定时业务 API 被拦
        blocked = self.client.put("/api/scan/selection", json={"folder_ids": []})
        self.assertEqual(403, blocked.status_code)
        self.assertEqual("app_locked", blocked.json().get("code"))

        # 错误密码
        bad = self.client.post("/api/app-lock/unlock", json={"password": "wrong-pass"})
        self.assertEqual(403, bad.status_code)
        self.assertFalse(server._APP_LOCK["unlocked"])

        ok = self.client.post("/api/app-lock/unlock", json={"password": "secret12"})
        self.assertEqual(200, ok.status_code)
        self.assertTrue(ok.json()["unlocked"])

    def test_set_password_requires_current_when_already_set(self):
        self.client.post("/api/app-lock/set-password", json={"password": "secret12"})
        bad = self.client.post("/api/app-lock/set-password",
                               json={"password": "secret34", "current_password": "nope"})
        self.assertEqual(403, bad.status_code)
        ok = self.client.post("/api/app-lock/set-password",
                              json={"password": "secret34", "current_password": "secret12"})
        self.assertEqual(200, ok.status_code)
        self.assertTrue(server._password_matches("secret34"))
        self.assertFalse(server._password_matches("secret12"))

    def test_remove_password(self):
        self.client.post("/api/app-lock/set-password", json={"password": "secret12"})
        bad = self.client.post("/api/app-lock/remove", json={"current_password": "nope"})
        self.assertEqual(403, bad.status_code)
        ok = self.client.post("/api/app-lock/remove", json={"current_password": "secret12"})
        self.assertEqual(200, ok.status_code)
        self.assertFalse(ok.json()["password_set"])
        self.assertTrue(ok.json()["unlocked"])

    def test_auto_unlock_blob_roundtrip(self):
        self.client.post("/api/app-lock/set-password",
                         json={"password": "secret12", "auto_unlock": True})
        self.assertTrue(server.APP_UNLOCK_BLOB.is_file())
        server._APP_LOCK["unlocked"] = False
        self.assertTrue(server._try_auto_unlock())
        server._set_auto_unlock(False)
        self.assertFalse(server.APP_UNLOCK_BLOB.is_file())
        self.assertFalse(server._try_auto_unlock())

    def test_short_password_rejected(self):
        res = self.client.post("/api/app-lock/set-password", json={"password": "12345"})
        self.assertEqual(400, res.status_code)

    def test_lock_open_paths_still_reachable(self):
        self.client.post("/api/app-lock/set-password", json={"password": "secret12"})
        self.client.post("/api/app-lock/lock")
        self.assertEqual(200, self.client.get("/api/app-lock/status").status_code)
        self.assertEqual(200, self.client.get("/api/version").status_code)


if __name__ == "__main__":
    unittest.main()
