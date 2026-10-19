from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import store
from core.database import DatabaseState, IntegrityValidationError


class SchemaMigrationTests(unittest.TestCase):
    """Schema upgrades run on a private copy and install only after validation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bfo-schema-migration-")
        self.root = Path(self._tmp.name)
        self.data_dir = self.root / "data"
        self.db_file = self.data_dir / "library.sqlite3"
        self._old_paths = (store.DATA_DIR, store.DB_FILE, store.LEGACY_DB_FILE,
                           store.LEGACY_DATA_DIR, store._LEGACY_FILES,
                           store._lifecycle_instance, store._lifecycle_key)
        store.DATA_DIR = self.data_dir
        store.DB_FILE = self.db_file
        store.LEGACY_DB_FILE = self.root / "legacy" / "library.sqlite3"
        store.LEGACY_DATA_DIR = self.root / "legacy" / "data"
        store._LEGACY_FILES = {}
        store._lifecycle_instance = None
        store._lifecycle_key = None
        self.data_dir.mkdir(parents=True)

    def tearDown(self):
        (store.DATA_DIR, store.DB_FILE, store.LEGACY_DB_FILE,
         store.LEGACY_DATA_DIR, store._LEGACY_FILES,
         store._lifecycle_instance, store._lifecycle_key) = self._old_paths
        self._tmp.cleanup()

    @staticmethod
    def _create_schema_database(path: Path, schema_version: int = 7) -> None:
        """Create a synthetic database with stable rows and a requested ledger head.

        Start from the current DDL so the fixture remains portable, then remove
        version-8 extension tables for pre-v8 ledgers. This is useful for
        validating lifecycle behavior; it is not a substitute for a historical
        v0.209 database fixture.
        """
        connection = sqlite3.connect(path)
        try:
            store._initialize_schema(connection)
            if schema_version < 8:
                connection.execute("PRAGMA foreign_keys=OFF")
                for table in (
                    "remote_operation_events", "remote_operations",
                    "remote_operation_schema_version", "bfo_scheduler_tasks",
                    "bfo_scheduler_meta",
                ):
                    connection.execute(f'DROP TABLE IF EXISTS "{table}"')
            connection.execute("DELETE FROM schema_migrations WHERE version > ?",
                               (schema_version,))
            connection.execute(
                "INSERT INTO folders(media_id,title,record_json) VALUES(?,?,?)",
                ("SYN-FOLDER", "Preserve this folder", json.dumps({"marker": "keep"})),
            )
            connection.execute(
                "INSERT INTO videos(resource_key,bvid,resource_id,record_json,updated_at) "
                "VALUES(?,?,?,?,?)",
                ("BVTEST0001", "BVTEST0001", "901", json.dumps({"title": "keep video"}),
                 "2026-01-02 03:04:05"),
            )
            connection.execute(
                "INSERT INTO folder_items(media_id,resource_key,fav_time) VALUES(?,?,?)",
                ("SYN-FOLDER", "BVTEST0001", 1700000000),
            )
            connection.execute(
                "INSERT INTO app_state(name,value_json) VALUES(?,?)",
                ("schema_migration_marker", json.dumps({"value": "preserve"})),
            )
            connection.commit()
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint and int(checkpoint[0]) != 0:
                raise AssertionError("test fixture WAL checkpoint failed")
        finally:
            connection.close()

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _business_rows(path: Path) -> dict[str, list[tuple]]:
        connection = sqlite3.connect(store.sqlite_uri(path, "ro"), uri=True)
        try:
            queries = {
                "folders": "SELECT media_id,title,remote_count,record_json,status,"
                           "archived_at,directory_order FROM folders ORDER BY media_id",
                "videos": "SELECT resource_key,bvid,resource_id,resource_type,record_json,"
                          "updated_at FROM videos ORDER BY resource_key",
                "folder_items": "SELECT media_id,resource_key,fav_time,scan_run_id "
                                "FROM folder_items ORDER BY media_id,resource_key",
                "app_state": "SELECT name,value_json FROM app_state ORDER BY name",
                "analyses": "SELECT resource_key,record_json,status,dependent_ids FROM analyses "
                            "ORDER BY resource_key",
                "plans": "SELECT resource_key,record_json FROM plans ORDER BY resource_key",
                "folder_scan_state": "SELECT media_id,status,strategy,expected_count,fetched_count,"
                                     "cursor_page,snapshot_started_at,snapshot_completed_at,last_error "
                                     "FROM folder_scan_state ORDER BY media_id",
                "scan_stage": "SELECT scan_run_id,media_id,resource_key,record_json FROM scan_stage "
                              "ORDER BY scan_run_id,media_id,resource_key",
                "folder_profiles": "SELECT media_id,profile_json,updated_at FROM folder_profiles "
                                   "ORDER BY media_id",
                "providers": "SELECT id,name,base_url,api_key,created_at,updated_at FROM providers "
                             "ORDER BY id",
                "models": "SELECT id,provider_id,name,context_tokens,max_output_tokens,"
                          "capabilities_json,thinking_effort,test_status,test_message,test_at,"
                          "created_at,updated_at,context_source,output_source FROM models ORDER BY id",
            }
            return {name: [tuple(row) for row in connection.execute(query)]
                    for name, query in queries.items()}
        finally:
            connection.close()

    def _assert_rejected_without_source_change(self, expected_message: str) -> None:
        source_hash = self._sha256(self.db_file)
        with self.assertRaisesRegex(IntegrityValidationError, expected_message):
            store._initialize_existing_database()
        self.assertEqual(source_hash, self._sha256(self.db_file))
        self.assertEqual(DatabaseState.RECOVERY_REQUIRED, store._lifecycle().state)

    def test_schema7_database_migrates_on_copy_and_installs_atomically(self):
        self._create_schema_database(self.db_file)
        source_validation = store.validate_database_candidate(self.db_file)
        self.assertEqual(7, source_validation["schema_version"])

        store._initialize_existing_database()

        self.assertEqual(DatabaseState.READY, store._lifecycle().state)
        active = store.validate_database_candidate(self.db_file)
        self.assertEqual(store._SCHEMA_VERSION, active["schema_version"])
        with closing(sqlite3.connect(self.db_file)) as connection:
            self.assertEqual(
                "Preserve this folder",
                connection.execute(
                    "SELECT title FROM folders WHERE media_id='SYN-FOLDER'"
                ).fetchone()[0],
            )
            self.assertEqual(
                "keep video",
                json.loads(connection.execute(
                    "SELECT record_json FROM videos WHERE resource_key='BVTEST0001'"
                ).fetchone()[0])["title"],
            )
            self.assertEqual(
                ("BVTEST0001",),
                connection.execute(
                    "SELECT resource_key FROM folder_items WHERE media_id='SYN-FOLDER'"
                ).fetchone(),
            )
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='bfo_scheduler_tasks'"
            ).fetchone())

        recovery_copies = sorted(
            path for path in self.data_dir.glob("library.sqlite3.recovery-*")
            if not path.name.endswith(("-wal", "-shm", "-journal"))
        )
        self.assertEqual(1, len(recovery_copies))
        recovery = store.validate_database_candidate(recovery_copies[0])
        self.assertEqual(7, recovery["schema_version"])
        with closing(sqlite3.connect(recovery_copies[0])) as connection:
            self.assertEqual(
                "Preserve this folder",
                connection.execute(
                    "SELECT title FROM folders WHERE media_id='SYN-FOLDER'"
                ).fetchone()[0],
            )
        manifest = json.loads((self.data_dir / "database-manifest.json").read_text(
            encoding="utf-8"))
        self.assertEqual(store._SCHEMA_VERSION, manifest["schema_version"])
        self.assertEqual([], list(self.data_dir.glob(".*.migrating")))

    def test_historical_v0209_schema7_database_migrates_without_logical_data_loss(self):
        fixture = Path(__file__).parent / "fixtures" / "v0209_schema7.sqlite3"
        self.assertTrue(fixture.is_file())
        self.assertEqual(
            "511f66afc63562f8a102a49150a55a22a839c0340fa70a9c64a0690692e1b273",
            self._sha256(fixture),
            "historical fixture must match its documented synthetic source",
        )
        self.db_file.write_bytes(fixture.read_bytes())
        before = self._business_rows(self.db_file)
        self.assertEqual(7, store.validate_database_candidate(self.db_file)["schema_version"])
        self.assertEqual("preserve-v0209", json.loads(
            dict(before["app_state"])["v0209_fixture_marker"])["value"])

        store._initialize_existing_database()

        self.assertEqual(DatabaseState.READY, store._lifecycle().state)
        self.assertEqual(store._SCHEMA_VERSION,
                         store.validate_database_candidate(self.db_file)["schema_version"])
        self.assertEqual(before, self._business_rows(self.db_file))

    def test_partial_schema_failure_leaves_original_database_file_unchanged(self):
        self._create_schema_database(self.db_file)
        original_hash = self._sha256(self.db_file)

        def fail_after_partial_candidate_change(connection):
            connection.execute("CREATE TABLE candidate_only_marker(value TEXT)")
            raise RuntimeError("injected candidate migration failure")

        with mock.patch.object(store, "_initialize_schema",
                               side_effect=fail_after_partial_candidate_change):
            with self.assertRaisesRegex(RuntimeError, "injected candidate migration failure"):
                store._initialize_existing_database()

        self.assertEqual(original_hash, self._sha256(self.db_file))
        self.assertEqual(7, store.validate_database_candidate(self.db_file)["schema_version"])
        with closing(sqlite3.connect(self.db_file)) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='candidate_only_marker'"
            ).fetchone())
        self.assertEqual(DatabaseState.RECOVERY_REQUIRED, store._lifecycle().state)
        self.assertEqual([], list(self.data_dir.glob(".*.migrating")))
        self.assertEqual([], list(self.data_dir.glob("library.sqlite3.recovery-*")))

    def test_legacy_location_migration_cleans_candidate_sidecars_after_success(self):
        store.LEGACY_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._create_schema_database(store.LEGACY_DB_FILE, schema_version=7)
        validate = store.validate_database_candidate

        def create_sidecars(path):
            result = validate(path)
            for suffix in ("-wal", "-shm", "-journal"):
                Path(str(path) + suffix).write_bytes(b"candidate sidecar")
            return result

        with mock.patch.object(store, "validate_database_candidate", side_effect=create_sidecars):
            store._migrate_database_location()

        self.assertTrue(self.db_file.is_file())
        self.assertEqual(7, store.validate_database_candidate(self.db_file)["schema_version"])
        self.assertEqual([], list(self.data_dir.glob(".library-*.migrating*")))
        self.assertTrue(store.LEGACY_DB_FILE.is_file(), "legacy source must remain untouched")

    def test_legacy_location_migration_cleans_candidate_sidecars_after_validation_failure(self):
        store.LEGACY_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._create_schema_database(store.LEGACY_DB_FILE, schema_version=7)
        validate = store.validate_database_candidate

        def create_sidecars_then_fail(path):
            validate(path)
            for suffix in ("-wal", "-shm", "-journal"):
                Path(str(path) + suffix).write_bytes(b"candidate sidecar")
            raise IntegrityValidationError("injected candidate rejection")

        with mock.patch.object(store, "validate_database_candidate",
                               side_effect=create_sidecars_then_fail):
            with self.assertRaisesRegex(IntegrityValidationError, "injected candidate rejection"):
                store._migrate_database_location()

        self.assertFalse(self.db_file.exists())
        self.assertTrue(store.LEGACY_DB_FILE.is_file(), "legacy source must remain untouched")
        self.assertEqual([], list(self.data_dir.glob(".library-*.migrating*")))

    def test_unsupported_schema_is_rejected_without_modifying_source(self):
        self._create_schema_database(self.db_file)
        with closing(sqlite3.connect(self.db_file)) as connection:
            connection.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES(99,'synthetic')")
            connection.commit()
        source_hash = self._sha256(self.db_file)

        with self.assertRaisesRegex(IntegrityValidationError, "高于当前支持版本"):
            store._initialize_existing_database()

        self.assertEqual(source_hash, self._sha256(self.db_file))
        self.assertEqual(DatabaseState.RECOVERY_REQUIRED, store._lifecycle().state)
        self.assertEqual([], list(self.data_dir.glob(".*.migrating")))

    def test_schema8_missing_runtime_column_is_rejected_without_source_change(self):
        self._create_schema_database(self.db_file, schema_version=8)
        with closing(sqlite3.connect(self.db_file)) as connection:
            connection.execute("DROP INDEX IF EXISTS idx_videos_bvid")
            connection.execute("DROP TABLE videos")
            connection.execute(
                "CREATE TABLE videos (resource_key TEXT PRIMARY KEY, bvid TEXT, "
                "resource_id TEXT NOT NULL DEFAULT '', resource_type INTEGER NOT NULL "
                "DEFAULT 2, updated_at TEXT NOT NULL)"
            )
            connection.execute("CREATE INDEX idx_videos_bvid ON videos(bvid)")
            connection.commit()

        self._assert_rejected_without_source_change("列|字段|schema")

    def test_schema7_ledger_gap_is_rejected_without_source_change(self):
        self._create_schema_database(self.db_file, schema_version=7)
        with closing(sqlite3.connect(self.db_file)) as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version=4")
            connection.commit()

        self._assert_rejected_without_source_change("迁移链|ledger|版本")

    def test_schema8_ledger_gap_is_rejected_without_source_change(self):
        self._create_schema_database(self.db_file, schema_version=8)
        with closing(sqlite3.connect(self.db_file)) as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version=4")
            connection.commit()

        self._assert_rejected_without_source_change("迁移链|ledger|版本")

    def test_schema8_remote_operation_version_marker_mismatch_is_rejected(self):
        self._create_schema_database(self.db_file, schema_version=8)
        with closing(sqlite3.connect(self.db_file)) as connection:
            connection.execute("UPDATE remote_operation_schema_version SET version=99")
            connection.commit()

        self._assert_rejected_without_source_change("远端操作日志 schema 版本")

    def test_schema8_fractional_extension_version_marker_is_rejected(self):
        self._create_schema_database(self.db_file, schema_version=8)
        with closing(sqlite3.connect(self.db_file)) as connection:
            connection.execute("UPDATE remote_operation_schema_version SET version=1.5")
            connection.commit()

        self._assert_rejected_without_source_change("远端操作日志 schema 版本")

    def test_fractional_ledger_version_is_rejected(self):
        self._create_schema_database(self.db_file, schema_version=8)
        with closing(sqlite3.connect(self.db_file)) as connection:
            connection.execute("ALTER TABLE schema_migrations RENAME TO old_schema_migrations")
            connection.execute(
                "CREATE TABLE schema_migrations(version NUMERIC PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
                [(1, "fixture"), (2, "fixture"), (3, "fixture"), (4, "fixture"),
                 (5, "fixture"), (6, "fixture"), (7.5, "fixture"), (8, "fixture")],
            )
            connection.execute("DROP TABLE old_schema_migrations")
            connection.commit()

        self._assert_rejected_without_source_change("整数|类型|版本")

    def test_schema8_missing_scheduler_idempotency_unique_key_is_rejected(self):
        self._create_schema_database(self.db_file, schema_version=8)
        with closing(sqlite3.connect(self.db_file)) as connection:
            connection.execute("DROP TABLE bfo_scheduler_tasks")
            connection.execute("""
                CREATE TABLE bfo_scheduler_tasks (
                    task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL,
                    task_schema_version INTEGER NOT NULL,
                    checkpoint_schema_version INTEGER NOT NULL, status TEXT NOT NULL,
                    parameters_json TEXT NOT NULL, checkpoint_json TEXT,
                    progress_json TEXT NOT NULL, error_json TEXT, resume_policy TEXT NOT NULL,
                    pause_reason TEXT, resume_intent TEXT NOT NULL,
                    attempt INTEGER NOT NULL, owner_epoch INTEGER NOT NULL,
                    resource_keys_json TEXT NOT NULL, idempotency_key TEXT,
                    reconciliation_required INTEGER NOT NULL,
                    reconciliation_reason TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, revision INTEGER NOT NULL
                )
            """)
            connection.commit()

        self._assert_rejected_without_source_change("唯一约束")

    def test_schema8_missing_declared_foreign_key_is_rejected(self):
        self._create_schema_database(self.db_file, schema_version=8)
        with closing(sqlite3.connect(self.db_file)) as connection:
            connection.execute("DROP TABLE models")
            connection.execute("""
                CREATE TABLE models (
                    id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, name TEXT NOT NULL,
                    context_tokens INTEGER NOT NULL, max_output_tokens INTEGER NOT NULL,
                    capabilities_json TEXT NOT NULL, thinking_effort TEXT NOT NULL,
                    test_status TEXT NOT NULL, test_message TEXT, test_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    context_source TEXT NOT NULL, output_source TEXT NOT NULL
                )
            """)
            connection.commit()

        self._assert_rejected_without_source_change("外键结构")

    def test_schema_below_confirmed_supported_range_is_rejected_without_source_change(self):
        # The supported floor is schema 5 (v0.200 was the first release that
        # shipped a ledger, at version 5). Anything below it has no verifiable
        # historical source and must not be guessed at or auto-upgraded.
        self._create_schema_database(self.db_file, schema_version=4)

        self._assert_rejected_without_source_change("支持|schema|版本")

    def _historical_fixture_rows(self, path: Path) -> dict:
        """Business rows that exist unchanged in schema 5, 6, 7 and 8."""
        connection = sqlite3.connect(store.sqlite_uri(path, "ro"), uri=True)
        try:
            return {
                "folders": connection.execute(
                    "SELECT media_id,title,remote_count,record_json FROM folders "
                    "ORDER BY media_id").fetchall(),
                "videos": connection.execute(
                    "SELECT resource_key,bvid,resource_id,record_json FROM videos "
                    "ORDER BY resource_key").fetchall(),
                "folder_items": connection.execute(
                    "SELECT media_id,resource_key,fav_time FROM folder_items "
                    "ORDER BY media_id,resource_key").fetchall(),
                "plans": connection.execute(
                    "SELECT resource_key,record_json FROM plans ORDER BY resource_key").fetchall(),
            }
        finally:
            connection.close()

    def _assert_historical_schema_migrates(self, fixture_name: str, expected_version: int,
                                           expected_sha256: str) -> None:
        fixture = Path(__file__).parent / "fixtures" / fixture_name
        self.assertTrue(fixture.is_file())
        self.assertEqual(expected_sha256, self._sha256(fixture),
                         "historical fixture must match its documented synthetic source")
        self.db_file.write_bytes(fixture.read_bytes())
        before = self._historical_fixture_rows(self.db_file)
        self.assertEqual(
            expected_version,
            store.validate_database_candidate(self.db_file)["schema_version"])

        store._initialize_existing_database()

        self.assertEqual(DatabaseState.READY, store._lifecycle().state)
        self.assertEqual(store._SCHEMA_VERSION,
                         store.validate_database_candidate(self.db_file)["schema_version"])
        self.assertEqual(before, self._historical_fixture_rows(self.db_file))
        with closing(sqlite3.connect(self.db_file)) as connection:
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='bfo_scheduler_tasks'").fetchone())

    def test_historical_v0200_schema5_database_migrates_without_logical_data_loss(self):
        self._assert_historical_schema_migrates(
            "v0200_schema5.sqlite3", 5,
            "2613f5b9315af1b36c60a0c311d76b1ea3ebed5ac7fc9ea190bd8cf325cb95d5")

    def test_historical_v0203_schema6_database_migrates_without_logical_data_loss(self):
        self._assert_historical_schema_migrates(
            "v0203_schema6.sqlite3", 6,
            "2a5a31ac3b9d248a19f7f049af3a076dc8614e51647d14bebb438d3b70a53a9d")

    def test_committed_uncheckpointed_wal_rows_survive_migration_and_repeat_init_is_idempotent(self):
        self._create_schema_database(self.db_file, schema_version=7)
        wal_writer = (
            "import os, sqlite3, sys; "
            "conn=sqlite3.connect(sys.argv[1]); "
            "conn.execute('PRAGMA journal_mode=WAL'); "
            "conn.execute('PRAGMA wal_autocheckpoint=0'); "
            "conn.execute(\"INSERT INTO app_state(name,value_json) VALUES("
            "'committed_in_wal','{\\\"marker\\\":\\\"keep-wal-row\\\"}')\"); "
            "conn.commit(); os._exit(0)"
        )
        subprocess.run([sys.executable, "-c", wal_writer, str(self.db_file)],
                       check=True)
        source_wal = Path(str(self.db_file) + "-wal")
        self.assertTrue(source_wal.is_file())
        self.assertGreater(source_wal.stat().st_size, 32)

        source_rows = self._business_rows(self.db_file)
        source_digest = hashlib.sha256(json.dumps(
            source_rows, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

        store._initialize_existing_database()

        self.assertEqual(DatabaseState.READY, store._lifecycle().state)
        self.assertEqual(source_digest, hashlib.sha256(json.dumps(
            self._business_rows(self.db_file), sort_keys=True,
            ensure_ascii=False).encode("utf-8")).hexdigest())
        self.assertIn(("committed_in_wal", '{"marker":"keep-wal-row"}'),
                      self._business_rows(self.db_file)["app_state"])
        recovery_copies = list(self.data_dir.glob("library.sqlite3.recovery-*"))
        self.assertEqual(1, len([path for path in recovery_copies
                                if not path.name.endswith(("-wal", "-shm", "-journal"))]))

        # A second startup on schema 8 must leave business rows and recovery
        # artifacts unchanged rather than installing another candidate copy.
        store._initialize_existing_database()
        self.assertEqual(DatabaseState.READY, store._lifecycle().state)
        self.assertEqual(source_digest, hashlib.sha256(json.dumps(
            self._business_rows(self.db_file), sort_keys=True,
            ensure_ascii=False).encode("utf-8")).hexdigest())
        self.assertEqual(1, len([path for path in self.data_dir.glob(
            "library.sqlite3.recovery-*")
                                if not path.name.endswith(("-wal", "-shm", "-journal"))]))
        with closing(sqlite3.connect(self.db_file)) as connection:
            self.assertEqual(
                '{"marker":"keep-wal-row"}',
                connection.execute(
                    "SELECT value_json FROM app_state WHERE name='committed_in_wal'"
                ).fetchone()[0],
            )


if __name__ == "__main__":
    unittest.main()
