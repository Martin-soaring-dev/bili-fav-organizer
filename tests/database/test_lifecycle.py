from __future__ import annotations

import sqlite3
import multiprocessing
import os
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
    open_readonly_session,
    validate_database,
)
from core.database.profile_lock import ProfileLockTimeoutError


def _hold_profile_shared_connection(path, ready, release):
    lifecycle = DatabaseLifecycle(path)
    lifecycle.set_state(DatabaseState.READY)
    connection = lifecycle.connect(owner="multiprocess-shared-holder")
    ready.set()
    try:
        if not release.wait(15):
            raise TimeoutError("test parent did not release shared connection")
    finally:
        connection.close()


def _attempt_profile_connection(path, attempted, results):
    lifecycle = DatabaseLifecycle(path)
    lifecycle.set_state(DatabaseState.READY)
    attempted.set()
    try:
        connection = lifecycle.connect(owner="multiprocess-late-reader",
                                       profile_lock_timeout=0.25)
    except ProfileLockTimeoutError:
        results.put("timed_out")
    else:
        connection.close()
        results.put("connected")


def _exit_while_holding_maintenance(path, acquired):
    lifecycle = DatabaseLifecycle(path)
    with lifecycle.maintenance(timeout=5):
        acquired.set()
        os._exit(0)


class StoreLifecycleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bfo-database-lifecycle-")
        self.root = Path(self._tmp.name)
        self._old = (store.DATA_DIR, store.DB_FILE, store.LEGACY_DB_FILE,
                     store.LEGACY_DATA_DIR, store._LEGACY_FILES)
        store.DATA_DIR = self.root / "data"
        store.DB_FILE = store.DATA_DIR / "library.sqlite3"
        store.LEGACY_DB_FILE = self.root / "legacy" / "library.sqlite3"
        store.LEGACY_DATA_DIR = self.root / "legacy" / "data"
        # _LEGACY_FILES 是导入时按 LEGACY_DATA_DIR 展开的绝对路径，不跟着换就会
        # 指向仓库里的旧 JSON，初始化会被正确地判成「有既有数据」而拒绝。
        store._LEGACY_FILES = {}
        store.DATA_DIR.mkdir(parents=True)

    def tearDown(self):
        (store.DATA_DIR, store.DB_FILE, store.LEGACY_DB_FILE,
         store.LEGACY_DATA_DIR, store._LEGACY_FILES) = self._old
        self._tmp.cleanup()

    def test_transient_candidate_file_does_not_orphan_a_ready_gate(self):
        """初始化窗口内的并发状态读取不能让门闩换成卡在 OPENING 的新实例。

        启动初始化会在 DATA_DIR 里创建再删除 ``*.initializing`` 候选文件。前端会
        同时轮询 /api/status（读 database_state()），如果缓存键包含这个瞬时文件，
        并发读取就会缓存第二个门闩；等文件消失，真正的调用方又换到第三个实例，
        于是数据库明明已经就绪，业务接口却一直 503。
        """
        store.initialize_new_database()
        self.assertEqual("READY", store.database_state())

        transient = store.DATA_DIR / ".library.sqlite3.abc123.initializing"
        transient.write_bytes(b"")
        try:
            self.assertEqual("READY", store.database_state())
        finally:
            transient.unlink()

        self.assertEqual("READY", store.database_state())
        store.save_folders([{"media_id": "10", "title": "仍可写"}])
        self.assertEqual("仍可写", store.load_folders()[0]["title"])

    def test_store_mutex_uses_profile_lock_before_local_mutex(self):
        """A writer cannot strand a store-lock owner behind the profile gate."""
        store.initialize_new_database()
        lifecycle = store._lifecycle()
        profile_state = lifecycle._profile_lock._state
        attempting = threading.Event()
        entered_store = threading.Event()
        failures = []

        def maintain_profile():
            try:
                attempting.set()
                with lifecycle.maintenance(timeout=3):
                    with store._lock:
                        entered_store.set()
            except Exception as exc:
                failures.append(exc)

        worker = threading.Thread(target=maintain_profile, daemon=True)
        with store._lock:
            worker.start()
            self.assertTrue(attempting.wait(1), "maintenance worker did not start")

            # Wait until the writer is either queued (new lock order) or has
            # acquired the exclusive lease (the old, deadlocking order).
            deadline = time.monotonic() + 2
            writer_pending = False
            while time.monotonic() < deadline:
                with profile_state.condition:
                    writer_pending = (profile_state.writer is not None or
                                      profile_state.waiting_writers > 0)
                if writer_pending:
                    break
                time.sleep(0.001)
            self.assertTrue(writer_pending, "maintenance worker never requested the profile lock")

            # Existing readers may nest a shared lease while a writer waits.
            # If the writer acquired exclusive while this thread held the
            # store mutex, this connection times out and reproduces the cycle.
            connection = lifecycle.connect(
                owner="store-lock-order-regression",
                profile_lock_timeout=0.25)
            connection.close()

        worker.join(timeout=3)
        self.assertFalse(worker.is_alive(), "maintenance/store mutex lock order deadlocked")
        self.assertEqual([], failures)
        self.assertTrue(entered_store.is_set())

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

    def test_existing_database_validation_waits_for_cross_process_shared_lease(self):
        store.initialize_new_database()
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        process = context.Process(
            target=_hold_profile_shared_connection,
            args=(str(store.DB_FILE), ready, release))
        process.start()
        try:
            self.assertTrue(ready.wait(10), "child did not acquire the shared profile lease")
            completed = threading.Event()
            errors = []

            def validate_existing_profile():
                try:
                    store._initialize()
                except Exception as exc:
                    errors.append(exc)
                finally:
                    completed.set()

            thread = threading.Thread(target=validate_existing_profile)
            thread.start()
            time.sleep(0.2)
            self.assertFalse(completed.is_set(),
                             "schema validation must wait for cross-process profile readers")
            release.set()
            thread.join(10)
            self.assertFalse(thread.is_alive(), "schema validation did not resume after release")
            self.assertEqual([], errors)
            self.assertEqual("READY", store.database_state())
        finally:
            release.set()
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(5)
        self.assertEqual(0, process.exitcode)

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
    def _make_db(path: Path, value: str, *, wal: bool = False):
        connection = sqlite3.connect(path)
        try:
            if wal:
                connection.execute("PRAGMA journal_mode=WAL").fetchone()
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

    def test_application_validator_checks_candidate_staged_backup_and_installed_files(self):
        self._make_db(self.active, "old")
        self._make_db(self.candidate, "new")
        lifecycle = DatabaseLifecycle(self.active)
        lifecycle.set_state(DatabaseState.READY)
        validated_values = []

        def validate_marker(path):
            validated_values.append(self._read_marker(Path(path)))

        result = lifecycle.install_candidate(self.candidate, validator=validate_marker)

        self.assertEqual(["new", "new", "old", "new"], validated_values)
        self.assertEqual("new", self._read_marker(self.active))
        self.assertEqual("old", self._read_marker(Path(result.backup_path)))

    def test_corrupt_candidate_is_rejected_without_touching_active_database(self):
        self._make_db(self.active, "old")
        self.candidate.write_bytes(b"not sqlite")
        lifecycle = DatabaseLifecycle(self.active)
        lifecycle.set_state(DatabaseState.READY)

        with self.assertRaises(IntegrityValidationError):
            lifecycle.install_candidate(self.candidate)
        self.assertEqual("old", self._read_marker(self.active))
        self.assertEqual("READY", lifecycle.state.value)

    def test_failed_post_replace_validation_restores_the_previous_database(self):
        self._make_db(self.active, "old")
        self._make_db(self.candidate, "new")
        lifecycle = DatabaseLifecycle(self.active)
        lifecycle.set_state(DatabaseState.READY)

        def fail_only_after_replace(path):
            if Path(path) == self.active:
                raise IntegrityValidationError("synthetic installed-file validation failure")
            return validate_database(path)

        with self.assertRaisesRegex(IntegrityValidationError, "installed-file"):
            lifecycle.install_candidate(self.candidate, validator=fail_only_after_replace)

        self.assertEqual("old", self._read_marker(self.active))
        self.assertEqual(DatabaseState.READY, lifecycle.state)
        recovery_copies = list(self.root.glob("library.sqlite3.recovery-*"))
        self.assertEqual(1, len(recovery_copies))
        self.assertEqual("old", self._read_marker(recovery_copies[0]))

    def test_failed_wal_candidate_rollback_removes_candidate_sidecars(self):
        self._make_db(self.active, "old", wal=True)
        self._make_db(self.candidate, "new", wal=True)
        lifecycle = DatabaseLifecycle(self.active)
        lifecycle.set_state(DatabaseState.READY)

        def fail_after_replace(path):
            if Path(path) == self.active:
                # Model SQLite leaving WAL-family files beside the rejected
                # installed candidate while its validation fails. The file
                # moves in the rollback path are deliberately tested without
                # keeping native SQLite handles open (which Windows forbids).
                for suffix in ("-wal", "-shm", "-journal"):
                    Path(str(path) + suffix).write_bytes(b"rejected-candidate-sidecar")
                raise IntegrityValidationError("synthetic WAL candidate failure")
            return validate_database(path)

        with self.assertRaisesRegex(IntegrityValidationError, "WAL candidate"):
            lifecycle.install_candidate(self.candidate, validator=fail_after_replace)

        self.assertEqual([], [Path(str(self.active) + suffix)
                             for suffix in ("-wal", "-shm", "-journal")
                             if Path(str(self.active) + suffix).exists()])
        self.assertEqual([], list(self.root.glob("library.sqlite3.failed-candidate-*")))
        self.assertEqual([], list(self.root.glob("library.sqlite3.failed-candidate-*-wal")))
        self.assertEqual(DatabaseState.READY, lifecycle.state)
        self.assertEqual("old", self._read_marker(self.active))

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

    def test_subprocess_shared_connection_blocks_candidate_install_without_touching_source(self):
        self._make_db(self.active, "old")
        self._make_db(self.candidate, "new")
        lifecycle = DatabaseLifecycle(self.active)
        lifecycle.set_state(DatabaseState.READY)
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        process = context.Process(
            target=_hold_profile_shared_connection,
            args=(str(self.active), ready, release))
        process.start()
        try:
            self.assertTrue(ready.wait(10), "child did not acquire the shared connection")
            with self.assertRaises(ProfileLockTimeoutError):
                lifecycle.install_candidate(self.candidate, maintenance_timeout=0.2)
            self.assertEqual("old", self._read_marker(self.active))
            self.assertEqual(DatabaseState.READY, lifecycle.state)
        finally:
            release.set()
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(5)
        self.assertEqual(0, process.exitcode)
        lifecycle.install_candidate(self.candidate, maintenance_timeout=2)
        self.assertEqual("new", self._read_marker(self.active))

    def test_exclusive_maintenance_rejects_a_new_subprocess_connection(self):
        self._make_db(self.active, "old")
        lifecycle = DatabaseLifecycle(self.active)
        lifecycle.set_state(DatabaseState.READY)
        context = multiprocessing.get_context("spawn")
        attempted = context.Event()
        results = context.Queue()
        with lifecycle.maintenance(timeout=2):
            process = context.Process(
                target=_attempt_profile_connection,
                args=(str(self.active), attempted, results))
            process.start()
            try:
                self.assertTrue(attempted.wait(10), "child did not attempt a connection")
                process.join(5)
                self.assertFalse(process.is_alive(), "child connection did not fail closed")
                self.assertEqual("timed_out", results.get(timeout=2))
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            self.assertEqual("old", self._read_marker(self.active))
        self.assertEqual(0, process.exitcode)

    def test_nested_borrowed_shared_connection_does_not_release_outer_maintenance(self):
        self._make_db(self.active, "old")
        lifecycle = DatabaseLifecycle(self.active)
        lifecycle.set_state(DatabaseState.READY)
        with lifecycle.maintenance(timeout=2):
            connection = lifecycle.connect(owner="same-thread-maintenance-read")
            connection.close()
            self.assertEqual(0, lifecycle.active_connection_count)
            with lifecycle.maintenance(timeout=0.2):
                pass

    def test_candidate_install_removes_staged_sqlite_sidecars(self):
        self._make_db(self.active, "old")
        self._make_db(self.candidate, "new")
        lifecycle = DatabaseLifecycle(self.active)
        lifecycle.set_state(DatabaseState.READY)

        def leave_staged_sidecars(path):
            if Path(path).name.endswith(".candidate"):
                for suffix in ("-wal", "-shm", "-journal"):
                    Path(str(path) + suffix).write_bytes(b"stale staged sidecar")

        lifecycle.install_candidate(self.candidate, validator=leave_staged_sidecars)
        self.assertEqual("new", self._read_marker(self.active))
        self.assertEqual([], list(self.root.glob(".*.candidate-*")))

    def test_process_exit_releases_kernel_maintenance_lock(self):
        self._make_db(self.active, "old")
        context = multiprocessing.get_context("spawn")
        acquired = context.Event()
        process = context.Process(
            target=_exit_while_holding_maintenance,
            args=(str(self.active), acquired))
        process.start()
        self.assertTrue(acquired.wait(10), "child did not acquire maintenance lock")
        process.join(10)
        self.assertEqual(0, process.exitcode)
        lifecycle = DatabaseLifecycle(self.active)
        with lifecycle.maintenance(timeout=2):
            self.assertEqual("old", self._read_marker(self.active))

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



class ReadOnlySessionTests(unittest.TestCase):
    """Read-only inspection of a live profile DB must not leave WAL/SHM behind.

    A ``mode=ro`` handle cannot delete the sidecars it creates, so closing one
    left an empty ``-wal`` and a ``-shm`` beside the database. Everything that
    may point at the active database now uses a writable handle with
    ``PRAGMA query_only`` instead.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bfo-readonly-session-")
        self.root = Path(self._tmp.name)
        self.db = self.root / "library.sqlite3"
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("PRAGMA journal_mode=WAL").fetchone()
            connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
            connection.execute("INSERT INTO marker(value) VALUES('seed')")
            connection.commit()
        finally:
            connection.close()

    def tearDown(self):
        self._tmp.cleanup()

    def _sidecars(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir()
                      if p.name.endswith(("-wal", "-shm", "-journal")))

    def test_open_readonly_session_is_read_only_and_leaves_no_sidecars(self):
        connection = open_readonly_session(self.db)
        try:
            count = connection.execute("SELECT count(*) FROM marker").fetchone()[0]
            self.assertEqual(1, count)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("INSERT INTO marker(value) VALUES('blocked')")
        finally:
            connection.close()
        self.assertEqual([], self._sidecars(),
                         "read-only session left WAL/SHM beside the database")

    def test_validate_database_leaves_no_sidecars(self):
        validation = validate_database(self.db)
        self.assertEqual(("ok",), validation.integrity_check)
        self.assertEqual((), validation.foreign_key_violations)
        self.assertEqual([], self._sidecars(),
                         "validate_database left WAL/SHM beside the database")


if __name__ == "__main__":
    unittest.main()
