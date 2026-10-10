# -*- coding: utf-8 -*-
"""解锁后要先做解密自检：这把 DEK 必须真能解开本地数据。

背景：unwrap 成功只说明「这把 KEK 能打开这份 wrap」，不等于「这把 DEK 能解开磁盘上的库」。
两者不一致的典型场景是金库重建过（旧库还在、DEK 已换），此时若不自检就放行，
用户会进入「已解锁但数据读不出来」的状态。
"""
import tempfile
import unittest
from pathlib import Path

import server
import store
import vault as vault_mod


SQLITE_MAGIC = b"SQLite format 3\x00"


class DekSelftestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmp.name)
        self._old = (store.DATA_DIR, server.VAULT, server.DEVICE_SECRET_BLOB)
        store.DATA_DIR = root / "data"
        store.DATA_DIR.mkdir(parents=True, exist_ok=True)
        server.DEVICE_SECRET_BLOB = root / "device_secret.blob"
        vault = vault_mod.Vault(root / "vault.json")
        vault.initialize(bound_mid="1", password="secret12",
                         recovery_code="AAAA-BBBB-CCCC-DDDD-EEEE")
        server.VAULT = vault

    def tearDown(self):
        store.DATA_DIR, server.VAULT, server.DEVICE_SECRET_BLOB = self._old
        self._tmp.cleanup()

    def _write_enc(self, encryptor):
        path = store.DATA_DIR / "library.sqlite3.enc"
        path.write_bytes(encryptor.encrypt(SQLITE_MAGIC + b"\x00" * 64))
        return path

    def test_decrypt_head_returns_prefix_without_whole_file(self):
        blob = server.VAULT.encrypt(SQLITE_MAGIC + b"rest-of-file")
        self.assertEqual(SQLITE_MAGIC, server.VAULT.decrypt_head(blob, 16))

    def test_selftest_ok_when_encrypted_with_current_dek(self):
        self._write_enc(server.VAULT)
        test = server._dek_selftest()
        self.assertTrue(test["ok"], test)

    def test_selftest_fails_when_encrypted_with_another_dek(self):
        other = vault_mod.Vault(Path(self._tmp.name) / "other-vault.json")
        other.initialize(bound_mid="1", password="other-pass",
                         recovery_code="BBBB-CCCC-DDDD-EEEE-FFFF")
        self._write_enc(other)
        test = server._dek_selftest()
        self.assertFalse(test["ok"], test)

    def test_selftest_skips_without_snapshot(self):
        test = server._dek_selftest()
        self.assertTrue(test["ok"])
        self.assertIn("跳过", test["detail"])


if __name__ == "__main__":
    unittest.main()
