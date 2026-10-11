# -*- coding: utf-8 -*-
"""Hello 设备 secret 必须与应用密码的本机材料分开存放。

回归背景：两者曾共用 app_unlock.blob，应用密码的自动解锁会写固定字面量
b"app-unlock" 并在关闭时删除该文件，把金库的设备 secret 一并覆盖/抹掉 ——
表现为「Windows Hello 验证通过，却仍然要求输入应用密码」。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.responses import JSONResponse

import server
import store
import vault as vault_mod


def _payload(res):
    if isinstance(res, JSONResponse):
        return json.loads(res.body), res.status_code
    return res, 200


class VaultDeviceSecretTests(unittest.TestCase):
    # 生产路径在 setUp 重新绑定前取，否则「两个名字指向同一个文件」这类回归测不出来
    _prod_app_unlock = server.APP_UNLOCK_BLOB
    _prod_device_secret = server.DEVICE_SECRET_BLOB

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmp.name)
        self._old = (
            server.USER_DATA_DIR,
            server.APP_UNLOCK_BLOB,
            server.DEVICE_SECRET_BLOB,
            server.VAULT,
            store.DATA_DIR,
            store.DB_FILE,
            server.VAULT_DB_ENC,
        )
        server.USER_DATA_DIR = root
        server.APP_UNLOCK_BLOB = root / "app_unlock.blob"
        server.DEVICE_SECRET_BLOB = root / "device_secret.blob"
        # 解锁会做解密自检，自检读的是加密快照 —— 不隔离会去碰真实库。
        # 快照的唯一来源是 server.VAULT_DB_ENC（写入/回读/自检都看它），
        # 所以它必须跟着 store.DATA_DIR 一起换。
        store.DATA_DIR = root / "data"
        store.DATA_DIR.mkdir(parents=True, exist_ok=True)
        store.DB_FILE = store.DATA_DIR / "library.sqlite3"
        store._initialize()
        server.VAULT_DB_ENC = store.DATA_DIR / "library.sqlite3.enc"

        vault = vault_mod.Vault(root / "vault.json")
        vault.initialize(bound_mid="1", password="secret12",
                         recovery_code="AAAA-BBBB-CCCC-DDDD-EEEE")
        vault.save_meta()
        server.VAULT = vault
        server._APP_LOCK.update(enabled=False, unlocked=True, failed_attempts=0)

    def tearDown(self):
        (server.USER_DATA_DIR, server.APP_UNLOCK_BLOB,
         server.DEVICE_SECRET_BLOB, server.VAULT, store.DATA_DIR,
         store.DB_FILE, server.VAULT_DB_ENC) = self._old
        server._APP_LOCK.update(enabled=False, unlocked=True, failed_attempts=0)
        self._tmp.cleanup()

    def test_production_paths_are_distinct(self):
        """两套密钥材料必须落在两个文件里；共用一个文件时会互相覆盖。"""
        self.assertNotEqual(
            self._prod_app_unlock, self._prod_device_secret,
            "APP_UNLOCK_BLOB 与 DEVICE_SECRET_BLOB 必须是不同文件")

    def test_bind_writes_its_own_file_not_app_unlock(self):
        body, code = _payload(server.vault_bind_device(server.VaultBindDeviceIn(skip_hello=True)))
        self.assertEqual(200, code, body)
        self.assertTrue(body["has_device_wrap"])
        self.assertTrue(server.DEVICE_SECRET_BLOB.is_file(),
                        "设备 secret 应写入 device_secret.blob")
        self.assertFalse(server.APP_UNLOCK_BLOB.exists(),
                         "绑定 Hello 不该碰应用密码的 app_unlock.blob")

    def test_bind_verifies_hello_by_default(self):
        """绑定写入的是「能解开 DEK 的设备密钥」，默认必须先过一次 Windows Hello。"""
        seen = []
        original = server.vault_hello_verify
        server.vault_hello_verify = lambda: (seen.append(1) or {"ok": True, "status": "ok"})
        try:
            body, code = _payload(server.vault_bind_device())
            self.assertEqual(200, code, body)
            self.assertEqual(1, len(seen), "默认应请求一次 Windows Hello 验证")
        finally:
            server.vault_hello_verify = original

    def test_bind_cancelled_by_hello_writes_nothing(self):
        seen = []
        original = server.vault_hello_verify
        server.vault_hello_verify = lambda: (seen.append(1) or
                                             {"ok": False, "status": "cancel", "message": "已取消"})
        try:
            body, code = _payload(server.vault_bind_device())
            self.assertEqual(403, code, body)
            self.assertFalse(server.VAULT.state.has_device_wrap,
                             "Hello 未通过时不得写入设备包装")
            self.assertFalse(server.DEVICE_SECRET_BLOB.exists(),
                             "Hello 未通过时不得写入设备 secret")
        finally:
            server.vault_hello_verify = original

    def test_bind_skip_hello_does_not_prompt(self):
        seen = []
        original = server.vault_hello_verify
        server.vault_hello_verify = lambda: (seen.append(1) or {"ok": True, "status": "ok"})
        try:
            body, code = _payload(server.vault_bind_device(
                server.VaultBindDeviceIn(skip_hello=True)))
            self.assertEqual(200, code, body)
            self.assertEqual(0, len(seen), "skip_hello 不应再弹一次 Hello")
        finally:
            server.vault_hello_verify = original

    def test_app_lock_auto_unlock_toggle_keeps_device_secret(self):
        _payload(server.vault_bind_device(server.VaultBindDeviceIn(skip_hello=True)))
        before = server.DEVICE_SECRET_BLOB.read_bytes()

        # 应用密码开启/关闭本机自动解锁：会写入并删除 app_unlock.blob
        server._set_auto_unlock(True)
        self.assertTrue(server.APP_UNLOCK_BLOB.is_file())
        self.assertEqual(b"app-unlock", server._dpapi_unprotect(
            server.APP_UNLOCK_BLOB.read_bytes()))
        server._set_auto_unlock(False)
        self.assertFalse(server.APP_UNLOCK_BLOB.exists())

        self.assertTrue(server.DEVICE_SECRET_BLOB.is_file(),
                        "应用密码的自动解锁不应删除金库设备 secret")
        self.assertEqual(before, server.DEVICE_SECRET_BLOB.read_bytes())

        server.VAULT.lock()
        body, code = _payload(server.vault_unlock_device(
            server.VaultUnlockDeviceIn(skip_hello=True)))
        self.assertEqual(200, code, body)
        self.assertTrue(server.VAULT.is_unlocked())

    def test_migrates_device_secret_from_legacy_shared_blob(self):
        # 旧布局：金库的随机 secret 与应用密码材料挤在同一个 app_unlock.blob
        secret = "legacy-device-secret-0001"
        server.APP_UNLOCK_BLOB.write_bytes(
            server._dpapi_protect(secret.encode("utf-8")))
        server.VAULT.bind_device_blob(secret)
        server.VAULT.lock()

        body, code = _payload(server.vault_unlock_device(
            server.VaultUnlockDeviceIn(skip_hello=True)))
        self.assertEqual(200, code, body)
        self.assertTrue(server.DEVICE_SECRET_BLOB.is_file(),
                        "旧布局的设备 secret 应被迁到独立文件")
        self.assertEqual(secret, server._dpapi_unprotect(
            server.DEVICE_SECRET_BLOB.read_bytes()).decode("utf-8"))
        # 迁移不该改写应用密码的材料
        self.assertEqual(secret, server._dpapi_unprotect(
            server.APP_UNLOCK_BLOB.read_bytes()).decode("utf-8"))

    def test_missing_wrap_while_locked_explains_what_to_do(self):
        server.VAULT.lock()
        body, code = _payload(server.vault_unlock_device(
            server.VaultUnlockDeviceIn(skip_hello=True)))
        self.assertEqual(400, code, body)
        self.assertIn("缺少 Hello 设备密钥", body["error"])
        self.assertFalse(server.VAULT.is_unlocked())


if __name__ == "__main__":
    unittest.main()
