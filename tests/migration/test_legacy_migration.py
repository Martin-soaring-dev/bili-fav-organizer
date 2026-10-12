"""Compatibility tests for databases produced by the real v0.208 store.py."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from migration.support import (
    SOURCE_REPOSITORY,
    core_sha256,
    database_snapshot,
    fixture,
    probe,
    runtime,
)


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bfo-mig-legacy-")
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_MIG_001_empty_legacy_database_migrates(self):
        """Pre: v0.208 initializes an empty DB. Steps: import current store. Expect: valid empty DB."""
        old = fixture("legacy-empty")
        self.assertEqual(0, old["folder_count"])
        self.assertEqual(0, old["video_count"])
        current = runtime("legacy-empty", self.root)
        result = probe(current, "snapshot")
        migrated = result["database"]
        self.assertTrue(migrated["exists"])
        self.assertEqual(["ok"], migrated["integrity_check"])
        self.assertEqual([], migrated["foreign_key_violations"])
        self.assertEqual(0, migrated["counts"]["folders"])
        self.assertEqual(0, migrated["counts"]["videos"])
        self.assertEqual(8, max(row["version"] for row in migrated["logical_rows"]["schema_migrations"]))

    def test_MIG_002_normal_legacy_dataset_migrates(self):
        """Pre: synthetic v0.208 has 10 folders/500 videos. Steps: current import. Expect: counts preserved."""
        old = fixture("legacy-normal")
        self.assertEqual(10, old["folder_count"])
        self.assertEqual(500, old["video_count"])
        current = runtime("legacy-normal", self.root)
        result = probe(current, "snapshot")
        self.assertEqual(10, result["database"]["counts"]["folders"])
        self.assertEqual(500, result["database"]["counts"]["videos"])
        self.assertEqual(1000, result["database"]["counts"]["folder_items"])

    def test_MIG_003_edge_state_and_wal_migrate(self):
        """Pre: v0.208 DB has interrupted scan, alias and captured WAL. Steps: migrate. Expect: committed state retained."""
        old = fixture("legacy-edge")
        wal_dir = Path(old["wal_capture_dir"])
        self.assertGreater((wal_dir / "library.sqlite3-wal").stat().st_size, 32)
        current = runtime("legacy-edge", self.root)
        result = probe(current, "snapshot")
        snapshot = result["database"]
        self.assertEqual(["ok"], snapshot["integrity_check"])
        self.assertEqual([], snapshot["foreign_key_violations"])
        self.assertEqual(12, snapshot["counts"]["folders"])
        self.assertEqual(48, snapshot["counts"]["videos"])
        self.assertGreater(snapshot["counts"]["folder_items"], 48)
        self.assertIn("synthetic_wal_marker", [r["name"] for r in snapshot["logical_rows"]["app_state"]])
        self.assertEqual(1, snapshot["counts"]["scan_stage"])

    def test_MIG_004_integrity_and_foreign_keys(self):
        """Pre: migrated normal and edge DBs. Steps: run SQLite checks. Expect: integrity ok, no FK violations."""
        for case in ("legacy-normal", "legacy-edge"):
            with self.subTest(case=case):
                current = runtime(case, self.root / case)
                probe(current, "snapshot")
                snap = database_snapshot(current.db_file)
                self.assertEqual(["ok"], snap["integrity_check"])
                self.assertEqual([], snap["foreign_key_violations"])

    def test_MIG_005_folder_video_and_relationships_preserved(self):
        """Pre: normal synthetic dataset has two folders per video. Steps: inspect logical tables. Expect: stable IDs and links."""
        old = fixture("legacy-normal")
        current = runtime("legacy-normal", self.root)
        probe(current, "snapshot")
        snap = database_snapshot(current.db_file)
        self.assertEqual(10, old["folder_count"])
        self.assertEqual(500, old["video_count"])
        self.assertEqual(1000, snap["counts"]["folder_items"])
        self.assertEqual(500, len(snap["logical_rows"]["videos"]))
        self.assertEqual(10, len(snap["logical_rows"]["folders"]))
        by_key = {row["resource_key"]: row for row in snap["logical_rows"]["videos"]}
        self.assertEqual("Synthetic video 0000",
                         json.loads(by_key["BVTEST00000000"]["record_json"])["title"])
        foreign = {row["resource_key"] for row in snap["logical_rows"]["folder_items"]}
        known = {row["resource_key"] for row in snap["logical_rows"]["videos"]}
        self.assertEqual(known, foreign)

    def test_MIG_006_profiles_plans_scans_and_history_preserved(self):
        """Pre: normal fixture has profiles, analysis, plans and scan state. Steps: compare rows. Expect: all preserved."""
        old = fixture("legacy-normal")
        current = runtime("legacy-normal", self.root)
        probe(current, "snapshot")
        snap = database_snapshot(current.db_file)
        self.assertEqual(old["schema_version"], 7)
        self.assertEqual(10, snap["counts"]["folder_profiles"])
        self.assertEqual(4, snap["counts"]["analyses"])
        self.assertEqual(3, snap["counts"]["plans"])
        self.assertEqual(10, snap["counts"]["folder_scan_state"])
        self.assertGreaterEqual(snap["counts"]["app_state"], 6)

    def test_MIG_007_provider_configuration_and_synthetic_credentials_preserved(self):
        """Pre: fixtures contain invalid placeholder keys/cookie. Steps: current settings migration. Expect: readable values retained."""
        normal = runtime("legacy-normal", self.root / "normal")
        normal_result = probe(normal, "snapshot")
        self.assertTrue(normal_result["config"]["cookie_preserved"])
        self.assertTrue(normal_result["config"]["provider_key_preserved"])
        self.assertEqual(1, normal_result["config"]["provider_count"])
        self.assertEqual(1, normal_result["config"]["model_count"])

        edge = runtime("legacy-edge", self.root / "edge")
        edge_result = probe(edge, "snapshot")
        self.assertTrue(edge_result["config"]["cookie_preserved"])
        self.assertTrue(edge_result["config"]["legacy_config_key_migrated"])
        self.assertFalse(edge_result["config"]["api_key_remains_in_secrets"])

    def test_MIG_008_repeated_migration_is_idempotent(self):
        """Pre: legacy DB exists. Steps: launch current import twice. Expect: same canonical business SHA and counts."""
        current = runtime("legacy-normal", self.root)
        first = probe(current, "snapshot")["database"]
        second = probe(current, "snapshot")["database"]
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["counts"], second["counts"])
        self.assertEqual(core_sha256(first), core_sha256(second))
        self.assertTrue(Path(fixture("legacy-normal")["db_file"]).is_file(),
                        "legacy source must remain available after migration")

    def test_data_dir_override_is_scoped_to_sqlite_only(self):
        """Pre: isolated profile and legacy DB. Steps: set BFO data override. Expect: SQLite moves, config/vault stay under profile root."""
        override = self.root / "sqlite-override"
        current = runtime("legacy-normal", self.root, data_override=override)
        result = probe(current, "snapshot")
        self.assertEqual(override, Path(result["store_data_dir"]))
        self.assertEqual(current.local_app_data, Path(result["user_data_dir"]))
        self.assertEqual(override / "library.sqlite3", Path(result["db_file"]))
        self.assertTrue((current.local_app_data / "config.json").is_file())
        self.assertFalse((override / "config.json").exists())
        probe(current, "onboard")
        self.assertTrue((current.local_app_data / "vault.json").is_file())
        self.assertFalse((override / "vault.json").exists())

    def test_MIG_015_legacy_and_neo_default_data_path_contract(self):
        """Pre: both launchers are source-visible. Steps: compare path construction. Expect: same per-user data root."""
        current = runtime("legacy-normal", self.root)
        probe_result = probe(current, "snapshot")
        js = (SOURCE_REPOSITORY / "electron" / "main.js").read_text(encoding="utf-8")
        store_source = (SOURCE_REPOSITORY / "store.py").read_text(encoding="utf-8")
        self.assertIn("path.join(process.env.LOCALAPPDATA || app.getPath('appData'), 'BiliFavOrganizer')", js)
        self.assertIn('Path(os.environ["LOCALAPPDATA"]) / "BiliFavOrganizer"', store_source)
        self.assertEqual(current.local_app_data, Path(probe_result["user_data_dir"]))
        # A GUI/package launch is tracked as NOT_RUN in the report when not executed.

    def test_MIG_016_v0208_rollback_reads_and_plain_backup_restores_data(self):
        """Pre: current onboarding created a plain safety backup. Steps: restore copy and run v0.208 reader. Expect: equivalent rows."""
        current = runtime("legacy-normal", self.root)
        probe(current, "snapshot")
        before = database_snapshot(current.db_file)
        onboarded = probe(current, "onboard")
        # Roll back by intentionally decrypting a verified snapshot to an isolated
        # legacy-readable location, not by retaining an unencrypted safety backup.
        self.assertFalse(Path(str(current.db_file) + ".plain-backup").exists())
        unlocked = probe(current, "unlock")
        self.assertEqual(200, unlocked["unlock"]["http_status"])
        restored = self.root / "rollback" / "library.sqlite3"
        restored.parent.mkdir()
        restored.write_bytes(current.db_file.read_bytes())
        after = database_snapshot(restored)
        self.assertEqual(core_sha256(before), core_sha256(after))
        self.assertTrue(onboarded["encrypted_file"] if "encrypted_file" in onboarded else current.encrypted_file.is_file())

        old_store = self.root / "rollback-app"
        old_store.mkdir()
        source = __import__("subprocess").run(
            ["git", "show", "v0.208:store.py"], cwd=SOURCE_REPOSITORY,
            text=True, encoding="utf-8", errors="replace",
            stdout=__import__("subprocess").PIPE,
            stderr=__import__("subprocess").PIPE, check=True,
        ).stdout
        (old_store / "store.py").write_text(source, encoding="utf-8")
        reader = old_store / "read_legacy.py"
        reader.write_text(
            "import json, store\n"
            "print(json.dumps({'folders': len(store.load_folders(include_archived=True)), "
            "'videos': len(store.load_videos_raw())}))\n",
            encoding="utf-8",
        )
        import os
        import subprocess
        env = dict(current.env)
        env["PYTHONPATH"] = str(old_store)
        env["BILI_FAV_ORGANIZER_DATA_DIR"] = str(restored.parent)
        check = subprocess.run([__import__("sys").executable, str(reader)], cwd=old_store,
                               env=env, text=True, encoding="utf-8", errors="replace",
                               capture_output=True, check=True)
        self.assertIn('"folders": 10', check.stdout)
        self.assertIn('"videos": 500', check.stdout)

    def test_MIG_017_fresh_install_starts_independently(self):
        """A fresh import waits; explicit onboarding creates and seals the empty DB."""
        current = runtime("legacy-empty", self.root, legacy_config=False, copy_legacy_db=False)
        result = probe(current, "snapshot")
        self.assertFalse(result["database"]["exists"])
        self.assertFalse(current.db_file.exists())
        self.assertFalse(current.encrypted_file.exists())

        onboarded = probe(current, "onboard")
        self.assertEqual(200, onboarded["onboarding"]["http_status"])
        self.assertEqual(200, onboarded["completion"]["http_status"])
        self.assertTrue(current.encrypted_file.is_file())
        self.assertFalse(current.db_file.exists())
        self.assertFalse(onboarded["decrypted_snapshot"]["counts"]["folders"])
        self.assertFalse(onboarded["decrypted_snapshot"]["counts"]["videos"])
        self.assertTrue(onboarded["vault_locked_after_completion"])
        self.assertTrue(str(current.local_app_data).startswith(str(self.root)))

    def test_MIG_018_second_instance_with_active_writer_fails_safely_and_recovers(self):
        """Pre: isolated current DB. Steps: hold one writer while a second instance starts, release and retry. Expect: no loss/corruption."""
        current = runtime("legacy-empty", self.root, copy_legacy_db=False, legacy_config=False)
        onboarded = probe(current, "onboard")
        self.assertEqual(200, onboarded["completion"]["http_status"])
        unlocked = probe(current, "unlock")
        self.assertEqual(200, unlocked["unlock"]["http_status"])
        import subprocess
        import sys
        holder_env = dict(current.env)
        holder_env["BFO_PROBE_PASSWORD"] = "Synthetic.Passphrase.42"
        lock_code = (
            "import json,os,server,store,sys; "
            "server.VAULT.load_meta(); "
            "server.VAULT.unlock_with_password(os.environ['BFO_PROBE_PASSWORD']); "
            "store._initialize(); "
            "c=store._connect(); c.execute('BEGIN IMMEDIATE'); "
            "c.execute('INSERT OR REPLACE INTO app_state(name,value_json) VALUES(?,?)', "
            "('synthetic_active_writer',json.dumps({'state':'held'}))); "
            "print('LOCK_HELD',flush=True); sys.stdin.readline(); c.commit(); c.close()"
        )
        lock_holder = subprocess.Popen(
            [sys.executable, "-c", lock_code], cwd=current.app_dir, env=holder_env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual("LOCK_HELD", lock_holder.stdout.readline().strip())
        second_code = """\
import sqlite3
import os
import sys
real_connect = sqlite3.connect
def fast_connect(*args, **kwargs):
    kwargs['timeout'] = 0.05
    return real_connect(*args, **kwargs)
sqlite3.connect = fast_connect
try:
    import server, store
    server.VAULT.load_meta()
    server.VAULT.unlock_with_password(os.environ['BFO_PROBE_PASSWORD'])
    store._initialize()
    store.save_folders([{'media_id': 'second-instance', 'title': 'second-instance',
                         'media_count': 0, 'attr': 0}])
except sqlite3.OperationalError as exc:
    print('DB_BUSY:' + str(exc))
    sys.exit(3)
"""
        second_process = None
        second_timed_out_while_lock_held = False
        try:
            second_process = subprocess.Popen(
                [sys.executable, "-c", second_code], cwd=current.app_dir,
                env=holder_env, text=True, encoding="utf-8", errors="replace",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                second_stdout, second_stderr = second_process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                second_timed_out_while_lock_held = True
        finally:
            lock_holder.stdin.write("release\n")
            lock_holder.stdin.close()
            holder_stdout = lock_holder.stdout.read()
            holder_stderr = lock_holder.stderr.read()
            holder_code = lock_holder.wait(timeout=10)
            lock_holder.stdout.close()
            lock_holder.stderr.close()
        self.assertEqual(0, holder_code, holder_stdout + holder_stderr)
        if second_timed_out_while_lock_held:
            second_stdout, second_stderr = second_process.communicate(timeout=30)
        second = subprocess.CompletedProcess(
            args=second_process.args, returncode=second_process.returncode,
            stdout=second_stdout, stderr=second_stderr,
        )
        safe_busy = second.returncode == 3 and "database is locked" in second.stdout.lower()
        self.assertTrue(second.returncode == 0 or safe_busy,
                        f"second instance failed unexpectedly: {second.stdout}\n{second.stderr}")
        if safe_busy:
            retry = subprocess.run(
                [sys.executable, "-c", second_code], cwd=current.app_dir,
                env=holder_env, text=True, encoding="utf-8", errors="replace",
                capture_output=True, timeout=30, check=False,
            )
            self.assertEqual(0, retry.returncode, f"startup retry failed: {retry.stdout}\n{retry.stderr}")
        first = probe(current, "unlock")["database_after_unlock"]
        second_snapshot = probe(current, "unlock")["database_after_unlock"]
        folder_ids = {row["media_id"] for row in first["logical_rows"]["folders"]}
        self.assertIn("second-instance", folder_ids)
        self.assertIn("synthetic_active_writer", [row["name"] for row in first["logical_rows"]["app_state"]])
        self.assertEqual(first["sha256"], second_snapshot["sha256"])
        self.assertEqual(["ok"], first["integrity_check"])
        self.assertEqual([], first["foreign_key_violations"])


if __name__ == "__main__":
    unittest.main()
