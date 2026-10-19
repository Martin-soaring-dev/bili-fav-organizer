"""Vault onboarding, lock, recovery and damaged ciphertext regression tests."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
import hashlib
from pathlib import Path

from core.database import DatabaseLifecycle, DatabaseState
from core.database.profile_lock import ProfileLockTimeoutError
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
        self.assertEqual(200, result["unlock"]["http_status"], result)
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
        self.assertEqual(200, result["unlock"]["http_status"], result)
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
                          "folders": 0, "videos": 0,
                          "selftest": {"ok": False, "detail": "encrypted snapshot corruption"}}, actual)
        # Data cannot be read from an absent plaintext DB. The encrypted evidence
        # must remain intact and must never be replaced by an empty SQLite database.
        self.assertEqual(bytes(damaged), current.encrypted_file.read_bytes())

    def test_MIG_023_interrupted_candidate_residue_blocks_lock_without_cleanup(self):
        """Pre: isolated READY DB. Steps: leave a restore candidate and request lock. Expect: fail closed, preserve sources and residue."""
        current = runtime("legacy-normal", self.root)
        result = probe(current, "seal-with-residue")
        residue = current.data_dir / ".vault-restore-interrupted.sqlite3"
        candidate_sidecar = current.data_dir / ".library.sqlite3.interrupted.candidate-shm"
        self.assertEqual(500, result["completion"]["http_status"])
        self.assertFalse(result["vault_locked"])
        self.assertTrue(residue.is_file())
        self.assertEqual(b"SYNTHETIC_INTERRUPTED_CANDIDATE", residue.read_bytes())
        self.assertTrue(candidate_sidecar.is_file())
        self.assertEqual(b"SYNTHETIC_CANDIDATE_SIDECAR", candidate_sidecar.read_bytes())
        self.assertTrue(current.db_file.is_file())
        self.assertTrue(all(result["sources_after_lock"].values()))
        self.assertEqual("READY", result["database_state"])

    def test_MIG_024_failed_candidate_encryption_preserves_old_ciphertext_and_retries(self):
        """Pre: valid encrypted snapshot. Steps: inject partial candidate write, then retry. Expect: old bytes survive and retry verifies."""
        current = runtime("legacy-normal", self.root)
        result = probe(current, "candidate-encryption-failure")
        self.assertTrue(result["injected"]["raised"])
        self.assertTrue(result["ciphertext_preserved_after_failure"])
        self.assertEqual([], result["candidate_residue_after_failure"])
        self.assertTrue(result["retry_succeeded"])
        self.assertTrue(current.encrypted_file.is_file())

    def test_MIG_025_missing_committed_manifest_fails_closed(self):
        """Pre: successfully locked v1 vault. Steps: remove manifest and unlock. Expect: no DB, recovery required, ciphertexts unchanged."""
        current = runtime("legacy-normal", self.root)
        onboard = probe(current, "onboard")
        self.assertEqual(200, onboard["completion"]["http_status"])
        archive_dir = current.data_dir / "vault-archives"
        manifest_path = archive_dir / "lock-manifest.enc"
        manifest_path.unlink()
        enc_before = current.encrypted_file.read_bytes()
        archive_before = {p.name: p.read_bytes() for p in archive_dir.glob("archive-*.enc")}
        result = probe(current, "unlock")
        self.assertEqual(500, result["unlock"]["http_status"], result)
        self.assertFalse(result["vault_unlocked"])
        self.assertFalse(current.db_file.exists(), result)
        self.assertEqual("RECOVERY_REQUIRED", result["database_state"])
        self.assertEqual(enc_before, current.encrypted_file.read_bytes())
        self.assertEqual(archive_before,
                         {p.name: p.read_bytes() for p in archive_dir.glob("archive-*.enc")})
        self.assertFalse(manifest_path.exists())

    def test_MIG_026_tampered_committed_manifest_fails_closed(self):
        """Pre: successfully locked v1 vault. Steps: alter manifest ciphertext and unlock. Expect: reject without modifying any encrypted evidence."""
        current = runtime("legacy-normal", self.root)
        onboard = probe(current, "onboard")
        self.assertEqual(200, onboard["completion"]["http_status"])
        archive_dir = current.data_dir / "vault-archives"
        manifest_path = archive_dir / "lock-manifest.enc"
        damaged = bytearray(manifest_path.read_bytes())
        damaged[-1] ^= 1
        manifest_path.write_bytes(damaged)
        enc_before = current.encrypted_file.read_bytes()
        archive_before = {p.name: p.read_bytes() for p in archive_dir.glob("archive-*.enc")}
        result = probe(current, "unlock")
        self.assertEqual(500, result["unlock"]["http_status"], result)
        self.assertFalse(result["vault_unlocked"])
        self.assertFalse(current.db_file.exists())
        self.assertEqual("RECOVERY_REQUIRED", result["database_state"])
        self.assertEqual(enc_before, current.encrypted_file.read_bytes())
        self.assertEqual(bytes(damaged), manifest_path.read_bytes())
        self.assertEqual(archive_before,
                         {p.name: p.read_bytes() for p in archive_dir.glob("archive-*.enc")})

    def test_MIG_027_lock_seals_legacy_json_and_managed_backups(self):
        """Pre: isolated legacy sources and managed backups. Steps: complete vault lock. Expect: verified encrypted manifest covers every plaintext source."""
        current = runtime("legacy-normal", self.root)
        result = probe(current, "seal-sources")
        self.assertEqual(200, result["completion"]["http_status"])
        self.assertTrue(result["vault_locked"])
        self.assertTrue(all(not exists for exists in result["sources_after_lock"].values()))
        self.assertFalse(current.db_file.exists())
        profile_lock = current.data_dir / ".bfo-profile.lock"
        self.assertTrue(profile_lock.is_file(), "the stable OS-lock rendezvous file must survive vault sealing")
        manifest = result["manifest"]
        self.assertEqual("locked", manifest["state"])
        self.assertEqual(set(manifest["archive_files"]), set(manifest["archive_hashes"]))
        sources = {entry["source"] for entry in manifest["entries"]}
        self.assertFalse(any(source.endswith("/.bfo-profile.lock") for source in sources))
        self.assertGreaterEqual(len(sources), 6)
        self.assertTrue(any("library.sqlite3" in source and "current-app" in source
                            for source in sources))
        self.assertTrue(any("pre-sqlite-synthetic" in source for source in sources))
        self.assertTrue(any("bili-data-synthetic" in source for source in sources))
        self.assertTrue(all(len(digest) == 64 for digest in manifest["archive_hashes"].values()))

    def test_MIG_028_repeated_lock_unlock_cycles_keep_manifest_file_set_stable(self):
        """Pre: locked vault. Steps: unlock/lock twice. Expect: same archive inventory and verified hashes each cycle."""
        current = runtime("legacy-normal", self.root)
        first = probe(current, "onboard")
        self.assertEqual(200, first["completion"]["http_status"])
        first_unlock = probe(current, "unlock")
        self.assertEqual(200, first_unlock["unlock"]["http_status"],
                         first_unlock.get("probe_stderr"))
        first_lock = probe(current, "explicit-lock")
        self.assertEqual(200, first_lock["lock"]["http_status"], {
            "lock": first_lock["lock"],
            "database_state": first_lock["database_state"],
            "database_sidecars_at_probe_start": first_lock[
                "database_sidecars_at_probe_start"],
            "open_sqlite_descriptors_at_probe_end": first_lock[
                "open_sqlite_descriptors_at_probe_end"],
            "probe_stderr": first_lock.get("probe_stderr"),
        })
        self.assertFalse(first_lock["plain_db_exists_after_lock"], first_lock)
        self.assertEqual([], first_lock["database_sidecars_after_lock"], first_lock)
        self.assertFalse(first_lock["database_file_exists_at_probe_end"], {
            "plain_db_exists_after_lock": first_lock["plain_db_exists_after_lock"],
            "final_database": first_lock["final_database"]["exists"],
            "database_sidecars_at_probe_end": first_lock["database_sidecars_at_probe_end"],
            "open_sqlite_descriptors_at_probe_end": first_lock[
                "open_sqlite_descriptors_at_probe_end"],
        })
        self.assertEqual(
            first_lock["manifest"]["active_ciphertext_sha256"],
            hashlib.sha256(current.encrypted_file.read_bytes()).hexdigest())
        manifest_path = current.data_dir / "vault-archives" / "lock-manifest.enc"
        self.assertEqual(first_lock["manifest"]["manifest_sha256"],
                         hashlib.sha256(manifest_path.read_bytes()).hexdigest())
        second_unlock = probe(current, "unlock")
        diagnostics = {
            "first_lock_after_route": first_lock["plain_db_exists_after_lock"],
            "first_lock_at_probe_end": first_lock["database_file_exists_at_probe_end"],
            "first_lock_sidecars_at_end": first_lock["database_sidecars_at_probe_end"],
            "first_lock_open_sqlite_fds": first_lock[
                "open_sqlite_descriptors_at_probe_end"],
            "locked_database_exists": second_unlock["database"]["exists"],
            "sidecars": second_unlock["database_sidecars_at_probe_start"],
            "encrypted_sha256": second_unlock["encrypted_file_sha256_at_probe_start"],
            "manifest_sha256": first_lock["manifest"]["manifest_sha256"],
            "probe_stderr": second_unlock.get("probe_stderr"),
        }
        self.assertFalse(second_unlock["database"]["exists"], diagnostics)
        self.assertEqual([], second_unlock["database_sidecars_at_probe_start"], diagnostics)
        self.assertEqual(200, second_unlock["unlock"]["http_status"], diagnostics)
        second_lock = probe(current, "explicit-lock")
        self.assertEqual(200, second_lock["lock"]["http_status"], {
            "lock": second_lock["lock"],
            "database_state": second_lock["database_state"],
            "database_sidecars_at_probe_start": second_lock[
                "database_sidecars_at_probe_start"],
            "open_sqlite_descriptors_at_probe_end": second_lock[
                "open_sqlite_descriptors_at_probe_end"],
            "probe_stderr": second_lock.get("probe_stderr"),
        })
        self.assertFalse(second_lock["plain_db_exists_after_lock"], second_lock)
        self.assertEqual([], second_lock["database_sidecars_after_lock"], second_lock)
        a = first_lock["manifest"]
        b = second_lock["manifest"]
        self.assertEqual(a["archive_files"], b["archive_files"])
        self.assertEqual(a["archive_hashes"], b["archive_hashes"])
        self.assertEqual(a["entries"], b["entries"])
        self.assertEqual([entry["archive"] for entry in a["entries"]],
                         [entry["archive"] for entry in b["entries"]])
        self.assertEqual("locked", b["state"])

    def test_MIG_029_second_process_cannot_open_profile_during_vault_sealing(self):
        """The exclusive profile lease must cover archive creation through plaintext removal."""
        current = runtime("legacy-normal", self.root)
        marker = self.root / "seal-window-ready"
        release = self.root / "seal-window-release"
        result = {}
        errors = []

        def run_seal():
            try:
                result.update(probe(
                    current, "seal-paused", timeout=45,
                    env_overrides={
                        "BFO_SEAL_PAUSE_MARKER": str(marker),
                        "BFO_SEAL_RELEASE_MARKER": str(release),
                    }))
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=run_seal, daemon=True)
        worker.start()
        deadline = time.monotonic() + 25
        try:
            while not marker.is_file() and worker.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.is_file(), {
                "error": "vault sealing did not reach plaintext removal",
                "completion": result.get("completion"),
                "probe_errors": [str(error) for error in errors],
            })

            contender = DatabaseLifecycle(
                current.db_file,
                profile_lock_path=current.data_dir / ".bfo-profile.lock")
            contender.set_state(DatabaseState.READY)
            with self.assertRaises(ProfileLockTimeoutError):
                contender.connect(profile_lock_timeout=0.2)
        finally:
            release.write_text("release", encoding="ascii")
            worker.join(45)

        self.assertFalse(worker.is_alive(), "vault sealing probe did not finish")
        self.assertEqual([], errors)
        self.assertEqual(200, result["completion"]["http_status"])
        self.assertFalse(current.db_file.exists())
        self.assertTrue(result["vault_locked"])

    def test_MIG_030_probe_snapshot_does_not_create_profile_sidecars(self):
        """A diagnostic snapshot must not create WAL/SHM beside the tested profile DB."""
        current = runtime("legacy-normal", self.root)
        onboard = probe(current, "onboard")
        self.assertEqual(200, onboard["completion"]["http_status"])

        unlocked = probe(current, "unlock")
        self.assertEqual(200, unlocked["unlock"]["http_status"], unlocked)
        self.assertTrue(unlocked["database_file_exists_at_probe_end"])
        self.assertEqual([], unlocked["database_sidecars_at_probe_start"])
        self.assertEqual([], unlocked["database_sidecars_at_probe_end"])


if __name__ == "__main__":
    unittest.main()
