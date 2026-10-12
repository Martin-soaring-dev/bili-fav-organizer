from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import store
from core.database import (
    ActiveConnectionsError,
    DatabaseAlreadyInitializedError,
    DatabaseLifecycle,
    DatabaseLifecycleError,
    DatabaseNotInitializedError,
    DatabaseRecoveryRequiredError,
    DatabaseState,
    IntegrityValidationError,
    validate_database,
)


class StoreLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bfo-database-lifecycle-")
        self.root = Path(self._tmp.name)
        self._old = (store.DATA_DIR, store.DB_FILE, store.LEGACY_DB_FILE,
                     store.LEGACY_DATA_DIR)
        store.DATA_DIR = self.root / "data"
        store.DB_FILE = store.DATA_DIR / "library.sqlite3"
        store.LEGACY_DB_FILE = self.root / "legacy" / "library.sqlite3"
        store.LEGACY_DATA_DIR = self.root / "legacy" / "data"
        store.DATA_DIR.mkdir(parents=True)

    def tearDown(self):
        (store.DATA_DIR, store.DB_FILE, store.LEGACY_DB_FILE,
         store.LEGACY_DATA_DIR) = self._old
        self._tmp.cleanup()

    def test_normal_connection_never_creates_a_missing_database(self):
        self.assertEqual("UNINITIALIZED", store.database_state())
        for mode in ("ro", "rw"):
            with self.subTest(mode=mode):
                with self.assertRaises(DatabaseNotInitializedError):
                    store._connect(mode=mode)
                self.assertFalse(store.DB_FILE.exists())

    def test_importing_store_does_not_initialize_a_blank_profile(self):
        import os

        isolated_root = self.root / "import-profile"
        environment = dict(os.environ)
        environment["BILI_FAV_ORGANIZER_DATA_DIR"] = str(isolated_root / "data")
        environment["LOCALAPPDATA"] = str(isolated_root)
        result = subprocess.run(
            [sys.executable, "-c",
             "import json,store; print(json.dumps({'exists': store.DB_FILE.exists(), "
             "'state': store.database_state()}))"],
            cwd=Path(__file__).resolve().parents[2], env=environment,
            check=True, capture_output=True, text=True,
        )
        self.assertIn('"exists": false', result.stdout)
        self.assertIn('"state": "UNINITIALIZED"', result.stdout)

    def test_explicit_initialization_creates_valid_schema_and_is_non_destructive(self):
        result = store.initialize_new_database()
        self.assertEqual(["ok"], result["integrity_check"])
        self.assertEqual(store._SCHEMA_VERSION, result["schema_version"])
        self.assertEqual("READY", store.database_state())

        store.save_folders([{"media_id": "keep-me", "title": "preserved"}])
        with self.assertRaises(DatabaseAlreadyInitializedError):
            store.initialize_new_database()
        store._initialize()
        self.assertEqual("preserved", store.load_folders()[0]["title"])
        self.assertTrue((store.DATA_DIR / "database-manifest.json").is_file())

    def test_missing_initialized_database_is_a_recovery_error(self):
        store.initialize_new_database()
        store.DB_FILE.unlink()
        with self.assertRaises(DatabaseRecoveryRequiredError):
            store._connect()
        self.assertEqual("RECOVERY_REQUIRED", store.database_state())
        with self.assertRaises(DatabaseRecoveryRequiredError):
            store.initialize_new_database()
        self.assertFalse(store.DB_FILE.exists())

    def test_existing_vault_evidence_blocks_new_empty_database(self):
        (store.DATA_DIR / "library.sqlite3.enc").write_bytes(b"opaque")
        with self.assertRaises(DatabaseRecoveryRequiredError):
            store.initialize_new_database()
        self.assertFalse(store.DB_FILE.exists())

    def test_empty_sqlite_file_is_not_silently_upgraded_into_a_fresh_database(self):
        # SQLite considers a zero-byte file a valid empty database. Once a file
        # exists, the store must require BFO schema evidence before adding tables.
        sqlite3.connect(store.DB_FILE).close()
        with self.assertRaisesRegex(IntegrityValidationError, "schema_migrations"):
            store._initialize()
        self.assertEqual("RECOVERY_REQUIRED", store.database_state())


class CandidateDatabaseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bfo-candidate-db-")
        self.root = Path(self._tmp.name)
        self.active = self.root / "library.sqlite3"
        self.candidate = self.root / "candidate.sqlite3"

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _make_db(path: Path, value: str):
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
            connection.execute("INSERT INTO marker(value) VALUES(?)", (value,))
            connection.commit()
        finally:
            connection.close()

    def test_candidate_is_validated_then_atomically_installed_with_old_copy(self):
        self._make_db(self.active, "old")
        self._make_db(self.candidate, "new")
        lifecycle = DatabaseLifecycle(self.active)
        lifecycle.set_state(DatabaseState.READY)

        result = lifecycle.install_candidate(self.candidate)
        self.assertEqual("READY", lifecycle.state.value)
        self.assertEqual("new", self._read_marker(self.active))
        self.assertTrue(result.backup_path)
        self.assertTrue(Path(result.backup_path).is_file())
        self.assertEqual("old", self._read_marker(Path(result.backup_path)))
        self.assertEqual(("ok",), validate_database(self.active).integrity_check)

    def test_corrupt_candidate_is_rejected_without_touching_active_database(self):
        self._make_db(self.active, "old")
        self.candidate.write_bytes(b"not sqlite")
        lifecycle = DatabaseLifecycle(self.active)
        lifecycle.set_state(DatabaseState.READY)

        with self.assertRaises(IntegrityValidationError):
            lifecycle.install_candidate(self.candidate)
        self.assertEqual("old", self._read_marker(self.active))
        self.assertEqual("READY", lifecycle.state.value)

    def test_live_store_connection_blocks_candidate_switch(self):
        self._make_db(self.active, "old")
        self._make_db(self.candidate, "new")
        lifecycle = DatabaseLifecycle(self.active)
        lifecycle.set_state(DatabaseState.READY)
        connection = lifecycle.connect(mode="rw", owner="test-worker")
        connection.execute("BEGIN IMMEDIATE")
        self.assertEqual(1, lifecycle.active_connection_count)
        self.assertEqual(1, lifecycle.active_write_transactions)
        self.assertEqual("test-worker", lifecycle.active_connections[0]["owner"])
        try:
            with self.assertRaises(ActiveConnectionsError):
                lifecycle.install_candidate(self.candidate)
        finally:
            connection.rollback()
            connection.close()
        self.assertEqual(0, lifecycle.active_connection_count)
        lifecycle.install_candidate(self.candidate)
        self.assertEqual("new", self._read_marker(self.active))

    def test_begin_drain_closes_gate_and_waits_for_existing_connections(self):
        self._make_db(self.active, "old")
        lifecycle = DatabaseLifecycle(self.active)
        lifecycle.set_state(DatabaseState.READY)
        connection = lifecycle.connect(owner="test-reader")
        completed = threading.Event()
        errors = []

        def drain():
            try:
                lifecycle.begin_drain(timeout=2)
            except Exception as exc:
                errors.append(exc)
            finally:
                completed.set()

        thread = threading.Thread(target=drain)
        thread.start()
        deadline = time.monotonic() + 1
        while lifecycle.state is DatabaseState.READY and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(DatabaseState.DRAINING, lifecycle.state)
        with self.assertRaises(DatabaseLifecycleError):
            lifecycle.connect(owner="late-reader")
        self.assertFalse(completed.is_set())

        connection.close()
        self.assertTrue(completed.wait(1))
        thread.join(timeout=1)
        self.assertEqual([], errors)
        self.assertEqual(DatabaseState.DRAINING, lifecycle.state)
        self.assertEqual(0, lifecycle.active_connection_count)

        maintenance = lifecycle.connect_for_drain(owner="test-maintenance")
        self.assertEqual(1, lifecycle.active_connection_count)
        maintenance.close()
        self.assertEqual(0, lifecycle.active_connection_count)

    def test_begin_drain_timeout_restores_ready_without_discarding_connection(self):
        self._make_db(self.active, "old")
        lifecycle = DatabaseLifecycle(self.active)
        lifecycle.set_state(DatabaseState.READY)
        connection = lifecycle.connect(owner="stuck-reader")
        try:
            with self.assertRaises(ActiveConnectionsError):
                lifecycle.begin_drain(timeout=0)
            self.assertEqual(DatabaseState.READY, lifecycle.state)
            self.assertEqual(1, lifecycle.active_connection_count)
        finally:
            connection.close()

    @staticmethod
    def _read_marker(path: Path) -> str:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            return str(connection.execute("SELECT value FROM marker").fetchone()[0])
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
