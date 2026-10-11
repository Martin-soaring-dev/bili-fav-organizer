"""Secure Vault：DEK/KEK 包装与恢复码。"""
import tempfile
import unittest
from pathlib import Path

import vault


class VaultCryptoTests(unittest.TestCase):
    def test_wrap_unwrap_password_roundtrip(self):
        dek = vault.generate_dek()
        wrap = vault.wrap_dek(dek, "secret-password")
        self.assertEqual(dek, vault.unwrap_dek(wrap, "secret-password"))

    def test_wrong_secret_fails(self):
        dek = vault.generate_dek()
        wrap = vault.wrap_dek(dek, "secret-password")
        with self.assertRaises(vault.VaultError):
            vault.unwrap_dek(wrap, "wrong-password")

    def test_recovery_code_normalization(self):
        code = vault.generate_recovery_code()
        self.assertEqual(code, vault.normalize_recovery_code(code.lower()))
        self.assertEqual(code, vault.normalize_recovery_code(code.replace("-", " ")))
        with self.assertRaises(vault.VaultError):
            vault.normalize_recovery_code("short")
        with self.assertRaises(vault.VaultError):
            vault.normalize_recovery_code("AAAA-AAAA-AAAA-AAAA-IIII")

    def test_blob_roundtrip(self):
        dek = vault.generate_dek()
        data = "你好，收藏夹".encode("utf-8")
        blob = vault.encrypt_blob(dek, data)
        self.assertEqual(data, vault.decrypt_blob(dek, blob))
        with self.assertRaises(vault.VaultError):
            vault.decrypt_blob(vault.generate_dek(), blob)

    def test_file_roundtrip(self):
        dek = vault.generate_dek()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, enc, out = root / "a.bin", root / "a.enc", root / "b.bin"
            src.write_bytes(b"payload-bytes")
            v = vault.Vault(root / "vault.json")
            v.initialize(bound_mid="123", password="secret-pass")
            v.encrypt_file(src, enc)
            self.assertNotEqual(src.read_bytes(), enc.read_bytes())
            v.decrypt_file(enc, out)
            self.assertEqual(src.read_bytes(), out.read_bytes())


class VaultLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.v = vault.Vault(self.root / "vault.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_initialize_sets_password_and_recovery(self):
        code = self.v.initialize(bound_mid="42", password="secret-pass")
        self.assertEqual(5, len(code.split("-")))
        st = self.v.state
        self.assertTrue(st.password_set)
        self.assertTrue(st.has_recovery_wrap)
        self.assertTrue(st.unlocked)
        self.assertEqual("42", st.bound_mid)
        self.assertFalse(st.onboarding_complete)

    def test_lock_unlock_password(self):
        self.v.initialize(bound_mid="1", password="secret-pass")
        self.v.lock()
        self.assertFalse(self.v.is_unlocked())
        self.v.unlock_with_password("secret-pass")
        self.assertTrue(self.v.is_unlocked())
        self.v.lock()
        with self.assertRaises(vault.VaultError):
            self.v.unlock_with_password("nope")

    def test_recovery_unlocks_after_password_change(self):
        code = self.v.initialize(bound_mid="1", password="secret-pass")
        self.v.set_password("new-pass")
        self.v.lock()
        self.v.unlock_with_password("new-pass")
        self.v.lock()
        self.v.unlock_with_recovery(code)
        self.assertTrue(self.v.is_unlocked())

    def test_meta_persists_without_dek(self):
        code = self.v.initialize(bound_mid="9", password="secret-pass")
        self.v.lock()
        v2 = vault.Vault(self.root / "vault.json")
        v2.load_meta()
        self.assertFalse(v2.state.unlocked)
        self.assertTrue(v2.state.password_set)
        v2.unlock_with_recovery(code)
        self.assertTrue(v2.is_unlocked())

    def test_set_password_requires_unlock_or_current(self):
        self.v.initialize(bound_mid="1", password="secret-pass")
        self.v.lock()
        with self.assertRaises(vault.VaultError):
            self.v.set_password("other")
        self.v.set_password("other", current_password="secret-pass")
        self.v.lock()
        self.v.unlock_with_password("other")

    def test_onboarding_flag(self):
        self.v.initialize(bound_mid="1", password="secret-pass")
        self.v.complete_onboarding()
        self.assertTrue(self.v.state.onboarding_complete)


if __name__ == "__main__":
    unittest.main()
