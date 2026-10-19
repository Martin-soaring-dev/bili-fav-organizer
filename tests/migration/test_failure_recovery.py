"""Controlled fault injection for copy and encryption interruption handling."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from migration.support import core_sha256, fixture, probe, runtime


class FailureRecoveryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bfo-mig-failure-")
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_MIG_014_database_copy_interruption_preserves_source_and_retry(self):
        """Pre: source DB exists in isolated tree. Steps: inject backup OSError, then retry. Expect: source intact and retry succeeds."""
        current = runtime("legacy-normal", self.root)
        source_manifest = fixture("legacy-normal")
        source = Path(source_manifest["db_file"])
        target_dir = self.root / "copy-interruption-target"
        env = dict(current.env)
        env["BFO_COPY_SOURCE"] = str(source)
        env["BFO_COPY_TARGET_DIR"] = str(target_dir)
        result = __import__("subprocess").run(
            [__import__("sys").executable, "migration_probe.py", "interrupt-copy"],
            cwd=current.app_dir, env=env, text=True, encoding="utf-8", errors="replace",
            capture_output=True, check=True,
        )
        payload = next(line for line in reversed(result.stdout.splitlines())
                       if line.startswith("MIGRATION_PROBE:")).removeprefix("MIGRATION_PROBE:")
        import json
        data = json.loads(payload)
        self.assertTrue(data["injected"])
        self.assertIn("synthetic interruption", data["first_error"])
        self.assertFalse(data["target_exists_after_failure"])
        self.assertEqual([], data["temporary_files_after_failure"])
        self.assertEqual(["ok"], data["source_after_failure"]["integrity_check"])
        self.assertTrue(data["target_exists_after_retry"])
        self.assertEqual(data["source_after_failure"]["sha256"], data["target_after_retry"]["sha256"])

    def test_MIG_014_interrupted_encrypted_write_never_commits_partial_candidate(self):
        """Pre: synthetic plain DB and unlocked vault. Steps: interrupt candidate write, retry. Expect: source survives, partial ciphertext stays uncommitted, retry succeeds."""
        current = runtime("legacy-normal", self.root)
        before = probe(current, "snapshot")["database"]
        result = probe(current, "partial-encryption-write")
        self.assertFalse(result["partial_file_exists"])
        self.assertEqual([], result["candidate_files_after_failure"])
        self.assertIsNone(result["first_attempt"])
        self.assertEqual(before["sha256"], result["source_db_after_failure"]["sha256"])
        self.assertTrue(result["encrypted_file_after_retry"])
        self.assertEqual("已继承本地数据并完成加密", result["retry_attempt"])
        self.assertTrue(probe(current, "selftest")["selftest"]["ok"])

    def test_MIG_014_corrupt_legacy_database_fails_without_replacing_source(self):
        """Pre: isolated legacy DB copy. Steps: damage only its header and launch. Expect: diagnostic failure, no target or temp overwrite."""
        current = runtime("legacy-normal", self.root)
        source = current.app_dir / "data" / "library.sqlite3"
        damaged_bytes = b"NOT A SQLITE DATABASE; SYNTHETIC CORRUPTION"
        source.write_bytes(damaged_bytes)
        with self.assertRaisesRegex(RuntimeError, "file is not a database"):
            probe(current, "snapshot")
        self.assertEqual(damaged_bytes, source.read_bytes())
        self.assertFalse(current.db_file.exists())
        self.assertEqual([], list(current.data_dir.glob("*.migrating")))

    def test_MIG_014_existing_encrypted_snapshot_does_not_reinitialize_or_overwrite(self):
        """Pre: initialized vault and encrypted DB. Steps: invoke onboarding twice. Expect: existing DEK/data never silently replaced."""
        current = runtime("legacy-normal", self.root)
        first = probe(current, "onboard")
        meta_before = (current.local_app_data / "vault.json").read_bytes()
        result = probe(current, "onboard")
        meta_after = (current.local_app_data / "vault.json").read_bytes()
        self.assertEqual(meta_before, meta_after)
        self.assertEqual(409, result["onboarding"]["http_status"])
        self.assertFalse(result["onboarding"]["body"]["ok"])
        unlocked = probe(current, "unlock")
        self.assertEqual(200, unlocked["unlock"]["http_status"], unlocked)
        self.assertEqual(core_sha256(first["decrypted_snapshot"]),
                         core_sha256(unlocked["database_after_unlock"]))


if __name__ == "__main__":
    unittest.main()
