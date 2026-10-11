"""Vault onboarding, lock, recovery and damaged ciphertext regression tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from migration.support import core_sha256, probe, runtime


class VaultMigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bfo-mig-vault-")
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_MIG_009_encryption_onboarding_preserves_logical_data(self):
        """Pre: normal legacy DB migrated into isolated profile. Steps: initialize+complete vault. Expect: encrypted equivalent DB, no plaintext working copy."""
        current = runtime("legacy-normal", self.root)
        before = probe(current, "snapshot")["database"]
        result = probe(current, "onboard")
        self.assertEqual(200, result["onboarding"]["http_status"])
        self.assertEqual(200, result["completion"]["http_status"])
        self.assertTrue(current.encrypted_file.is_file())
        self.assertEqual(core_sha256(before), core_sha256(result["decrypted_snapshot"]))
        plaintext_files = {
            "working_db": current.db_file.exists(),
            "plain_backup": Path(str(current.db_file) + ".plain-backup").exists(),
        }
        self.assertEqual({"working_db": False, "plain_backup": False}, plaintext_files,
                         "locked vault must not leave plaintext SQLite files")

    def test_MIG_010_close_and_reunlock_restores_encrypted_data(self):
        """Pre: vault completed and locked. Steps: remove only isolated plaintext working DB, restart+unlock. Expect: all data restored."""
        current = runtime("legacy-normal", self.root)
        onboard = probe(current, "onboard")
        self.assertEqual(200, onboard["completion"]["http_status"])
        expected = core_sha256(onboard["decrypted_snapshot"])
        # Remove remaining temporary legacy source so only the encrypted snapshot can restore.
        (current.app_dir / "data" / "library.sqlite3").unlink(missing_ok=True)
        (current.app_dir / "data" / "library.sqlite3-wal").unlink(missing_ok=True)
        (current.app_dir / "data" / "library.sqlite3-shm").unlink(missing_ok=True)
        current.db_file.unlink(missing_ok=True)
        result = probe(current, "unlock")
        self.assertEqual(200, result["unlock"]["http_status"])
        self.assertTrue(result["vault_unlocked"])
        actual = {
            "db_restored": result["unlock"]["body"].get("db_restored"),
            "core_sha256": core_sha256(result["database_after_unlock"]),
            "folders": result["database_after_unlock"]["counts"]["folders"],
            "videos": result["database_after_unlock"]["counts"]["videos"],
        }
        self.assertEqual({"db_restored": True, "core_sha256": expected,
                          "folders": 10, "videos": 500}, actual)

    def test_MIG_011_wrong_password_is_rejected(self):
        """Pre: initialized vault. Steps: restart with wrong password. Expect: HTTP 403 and still locked."""
        current = runtime("legacy-empty", self.root, copy_legacy_db=False)
        onboard = probe(current, "onboard")
        self.assertEqual(200, onboard["completion"]["http_status"])
        wrong = probe(current, "unlock", password="Definitely.Wrong.Passphrase")
        self.assertEqual(403, wrong["unlock"]["http_status"])
        self.assertFalse(wrong["vault_unlocked"])
        bad_recovery = probe(current, "unlock", password="",
                             recovery_code="AAAA-BBBB-CCCC-DDDD-EEEE")
        self.assertEqual(403, bad_recovery["unlock"]["http_status"])
        self.assertFalse(bad_recovery["vault_unlocked"])

    def test_MIG_012_recovery_code_unlocks_vault(self):
        """Pre: initialized vault recovery code captured in memory. Steps: unlock by code. Expect: logical DB restored."""
        current = runtime("legacy-normal", self.root)
        onboard = probe(current, "onboard")
        recovery_code = onboard["recovery_code"]
        self.assertTrue(recovery_code)
        (current.app_dir / "data" / "library.sqlite3").unlink(missing_ok=True)
        (current.app_dir / "data" / "library.sqlite3-wal").unlink(missing_ok=True)
        (current.app_dir / "data" / "library.sqlite3-shm").unlink(missing_ok=True)
        current.db_file.unlink(missing_ok=True)
        result = probe(current, "unlock", password="", recovery_code=recovery_code)
        self.assertEqual(200, result["unlock"]["http_status"])
        self.assertTrue(result["vault_unlocked"])
        self.assertEqual(core_sha256(onboard["decrypted_snapshot"]),
                         core_sha256(result["database_after_unlock"]))

    def test_MIG_013_corrupted_encrypted_file_fails_safely(self):
        """Pre: valid encrypted snapshot. Steps: flip final ciphertext bit and unlock with no plaintext source. Expect: reject and preserve no blank replacement."""
        current = runtime("legacy-normal", self.root)
        onboard = probe(current, "onboard")
        self.assertEqual(200, onboard["completion"]["http_status"])
        damaged = bytearray(current.encrypted_file.read_bytes())
        damaged[-1] ^= 0x01
        current.encrypted_file.write_bytes(damaged)
        (current.app_dir / "data" / "library.sqlite3").unlink(missing_ok=True)
        (current.app_dir / "data" / "library.sqlite3-wal").unlink(missing_ok=True)
        (current.app_dir / "data" / "library.sqlite3-shm").unlink(missing_ok=True)
        current.db_file.unlink(missing_ok=True)
        result = probe(current, "unlock")
        actual = {
            "http_status": result["unlock"]["http_status"],
            "vault_unlocked": result["vault_unlocked"],
            "db_exists": current.db_file.exists(),
            "folders": result["database_after_unlock"]["counts"]["folders"],
            "videos": result["database_after_unlock"]["counts"]["videos"],
            "selftest": result["unlock"]["body"].get("selftest"),
        }
        self.assertEqual({"http_status": 403, "vault_unlocked": False, "db_exists": False,
                          "folders": 10, "videos": 500,
                          "selftest": {"ok": False, "detail": "encrypted snapshot corruption"}}, actual)


if __name__ == "__main__":
    unittest.main()
