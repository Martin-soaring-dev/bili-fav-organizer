"""金库 API 与 onboarding 门闩。"""
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import server
import vault as vault_mod


class VaultApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL", "1")
        cls.client = TestClient(server.app)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmp.name)
        self._old = (server.VAULT, server.VAULT_DB_ENC, server._APP_LOCK.copy())
        server.VAULT = vault_mod.Vault(root / "vault.json")
        server.VAULT_DB_ENC = root / "library.sqlite3.enc"
        server._APP_LOCK.update(enabled=False, unlocked=True, failed_attempts=0)

    def tearDown(self):
        server.VAULT, server.VAULT_DB_ENC, old_lock = self._old
        server._APP_LOCK.update(old_lock)
        self._tmp.cleanup()

    def test_status_default_unconfigured(self):
        st = self.client.get("/api/vault/status").json()
        self.assertFalse(st["configured"])
        self.assertTrue(st["unlocked"])

    def test_onboarding_returns_recovery_code_once(self):
        res = self.client.post("/api/vault/onboarding", json={
            "bound_mid": "12345",
            "password": "secret-pass",
        })
        self.assertEqual(200, res.status_code)
        body = res.json()
        self.assertTrue(body["ok"])
        code = body["recovery_code"]
        self.assertEqual(5, len(code.split("-")))
        self.assertTrue(body["configured"])
        self.assertFalse(body["onboarding_complete"])
        # 再次 status 不返回恢复码
        st = self.client.get("/api/vault/status").json()
        self.assertNotIn("recovery_code", st)

    def test_onboarding_requires_password_and_mid(self):
        res = self.client.post("/api/vault/onboarding",
                               json={"bound_mid": "1", "password": "123"})
        self.assertEqual(400, res.status_code)
        res = self.client.post("/api/vault/onboarding",
                               json={"bound_mid": "", "password": "secret-pass"})
        self.assertEqual(400, res.status_code)

    def test_unlock_with_password_and_recovery(self):
        created = self.client.post("/api/vault/onboarding", json={
            "bound_mid": "9", "password": "secret-pass"}).json()
        code = created["recovery_code"]
        self.client.post("/api/vault/lock")
        locked = self.client.get("/api/vault/status").json()
        self.assertFalse(locked["unlocked"])

        bad = self.client.post("/api/vault/unlock", json={"password": "wrong-pass"})
        self.assertEqual(403, bad.status_code)

        ok = self.client.post("/api/vault/unlock", json={"password": "secret-pass"})
        self.assertEqual(200, ok.status_code)
        self.assertTrue(ok.json()["unlocked"])

        self.client.post("/api/vault/lock")
        ok2 = self.client.post("/api/vault/unlock", json={"recovery_code": code})
        self.assertEqual(200, ok2.status_code)

    def test_complete_onboarding_requires_unlock(self):
        self.client.post("/api/vault/onboarding", json={
            "bound_mid": "1", "password": "secret-pass"})
        self.client.post("/api/vault/lock")
        res = self.client.post("/api/vault/complete-onboarding")
        self.assertEqual(403, res.status_code)
        self.client.post("/api/vault/unlock", json={"password": "secret-pass"})
        res = self.client.post("/api/vault/complete-onboarding")
        self.assertEqual(200, res.status_code)
        self.assertTrue(res.json()["onboarding_complete"])

    def test_encrypt_file_roundtrip_via_vault(self):
        self.client.post("/api/vault/onboarding", json={
            "bound_mid": "1", "password": "secret-pass"})
        root = Path(self._tmp.name)
        src, enc = root / "db.sqlite3", root / "db.enc"
        src.write_bytes(b"sqlite-bytes")
        server.VAULT.encrypt_file(src, enc)
        self.assertNotEqual(b"sqlite-bytes", enc.read_bytes())
        out = root / "out.sqlite3"
        server.VAULT.decrypt_file(enc, out)
        self.assertEqual(b"sqlite-bytes", out.read_bytes())


if __name__ == "__main__":
    unittest.main()
