from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from core.scheduler import (
    AdmissionClosedError,
    CheckpointValidation,
    DatabaseNotFoundError,
    InMemoryTaskStore,
    InvalidTaskTransitionError,
    PauseReason,
    PauseRequested,
    ReconciliationResult,
    ResumeIntent,
    ResumePolicy,
    SQLiteTaskStore,
    SchedulerOwnershipError,
    TaskRecord,
    TaskScheduler,
    TaskStatus,
)


class _OwnerLock:
    def __init__(self):
        self.held = False

    def acquire(self):
        if self.held:
            raise RuntimeError("lock held")
        self.held = True

    def release(self):
        self.held = False


class _CompletingWorker:
    def __init__(self):
        self.calls = 0

    def execute(self, context):
        self.calls += 1
        context.save_checkpoint({"cursor": 3}, progress={"done": 3, "total": 3})


class _CheckpointWorker:
    def __init__(self):
        self.seen = []

    def validate_checkpoint(self, context):
        checkpoint = context.task.checkpoint
        return CheckpointValidation.accept() if checkpoint == {"cursor": 4} else CheckpointValidation.reject(
            "checkpoint signature mismatch"
        )

    def execute(self, context):
        self.seen.append(context.task.checkpoint)
        context.save_checkpoint({"cursor": 5}, progress={"done": 5})


class _PausingWorker:
    def __init__(self):
        self.started = threading.Event()
        self.exited = threading.Event()
        self.runs = 0

    def validate_checkpoint(self, context):
        return CheckpointValidation.accept()

    def execute(self, context):
        self.runs += 1
        self.started.set()
        if self.runs > 1:
            context.save_checkpoint({"cursor": self.runs}, progress={"done": self.runs})
            self.exited.set()
            return
        while True:
            if context.pause_requested:
                context.save_checkpoint({"cursor": self.runs}, progress={"done": self.runs})
                try:
                    context.raise_if_control_requested()
                except PauseRequested:
                    self.exited.set()
                    raise
            context.raise_if_control_requested()
            time.sleep(0.005)


class _WaitingWorker:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.exited = threading.Event()

    def execute(self, context):
        self.started.set()
        while not self.release.wait(0.005):
            context.raise_if_control_requested()
        self.exited.set()
        context.save_checkpoint({"released": True})


class _ResumableWorker:
    """Pauses at the first safe boundary, then runs until released."""

    def __init__(self):
        self.started = threading.Event()
        self.resumed = threading.Event()
        self.release = threading.Event()
        self.exited = threading.Event()
        self.runs = 0

    def validate_checkpoint(self, context):
        return CheckpointValidation.accept()

    def execute(self, context):
        self.runs += 1
        if self.runs == 1:
            self.started.set()
            while True:
                context.save_checkpoint({"runs": self.runs})
                context.raise_if_control_requested()
                time.sleep(0.005)
        self.resumed.set()
        self.release.wait(2)
        context.save_checkpoint({"runs": self.runs}, progress={"steps": {"done": 1, "total": 1}})
        self.exited.set()


class SchedulerTests(unittest.TestCase):
    def tearDown(self):
        for scheduler in list(getattr(self, "_schedulers", [])):
            if scheduler._started:
                try:
                    scheduler.shutdown(wait=True, timeout=2, pause_running=True)
                except Exception:
                    pass

    def _scheduler(self, store=None, workers=None, **kwargs):
        scheduler = TaskScheduler(
            store or InMemoryTaskStore(), workers=workers or {},
            owner_lock=kwargs.pop("owner_lock", None), **kwargs
        )
        if not hasattr(self, "_schedulers"):
            self._schedulers = []
        self._schedulers.append(scheduler)
        return scheduler

    def test_task_record_persists_completion_and_checkpoint(self):
        worker = _CompletingWorker()
        scheduler = self._scheduler(workers={"scan": worker})
        scheduler.start()
        task = scheduler.submit("scan", {"folder_ids": ["1"]}, resource_keys=["account:7"])

        done = scheduler.wait_for_status(task.task_id, {TaskStatus.COMPLETED}, timeout=2)
        self.assertEqual(done.status, TaskStatus.COMPLETED)
        self.assertEqual(done.checkpoint, {"cursor": 3})
        self.assertEqual(done.progress, {"done": 3, "total": 3})
        self.assertEqual(done.attempt, 1)
        self.assertEqual(done.owner_epoch, scheduler.owner_epoch)
        self.assertEqual(worker.calls, 1)

    def test_pause_is_not_reported_complete_until_worker_exits_and_checkpoint_is_saved(self):
        worker = _PausingWorker()
        scheduler = self._scheduler(workers={"scan": worker})
        scheduler.start()
        task = scheduler.submit("scan", resume_policy=ResumePolicy.AUTO_SAFE)
        self.assertTrue(worker.started.wait(1))

        requested = scheduler.pause_task(task.task_id, reason=PauseReason.VAULT_LOCK)
        self.assertEqual(requested.status, TaskStatus.PAUSING)
        self.assertTrue(worker.exited.wait(1))
        paused = scheduler.wait_for_status(task.task_id, {TaskStatus.PAUSED}, timeout=1)
        self.assertEqual(paused.checkpoint, {"cursor": 1})
        self.assertEqual(paused.resume_intent, ResumeIntent.AFTER_VAULT_UNLOCK)
        self.assertTrue(scheduler.wait_for_idle(timeout=1))

    def test_cancel_is_distinct_from_pause_and_does_not_resume(self):
        worker = _PausingWorker()
        scheduler = self._scheduler(workers={"scan": worker})
        scheduler.start()
        task = scheduler.submit("scan")
        self.assertTrue(worker.started.wait(1))

        requested = scheduler.cancel_task(task.task_id)
        self.assertEqual(requested.status, TaskStatus.CANCELLING)
        cancelled = scheduler.wait_for_status(task.task_id, {TaskStatus.CANCELLED}, timeout=1)
        self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
        with self.assertRaises(InvalidTaskTransitionError):
            scheduler.resume_task(task.task_id)
        self.assertEqual(worker.runs, 1)

    def test_resume_drops_the_stale_pause_reason(self):
        worker = _ResumableWorker()
        scheduler = self._scheduler(workers={"scan": worker})
        scheduler.start()
        task = scheduler.submit("scan", resume_policy=ResumePolicy.AUTO_SAFE)
        self.assertTrue(worker.started.wait(2))

        requested = scheduler.pause_task(task.task_id, reason=PauseReason.USER)
        self.assertEqual(requested.status, TaskStatus.PAUSING)
        self.assertEqual(requested.pause_reason, PauseReason.USER)
        paused = scheduler.wait_for_status(task.task_id, {TaskStatus.PAUSED}, timeout=2)
        # 暂停期间必须显示正确原因，否则用户不知道是谁按下的暂停。
        self.assertEqual(paused.pause_reason, PauseReason.USER)

        resumed = scheduler.resume_task(task.task_id)
        self.assertEqual(resumed.status, TaskStatus.RESUMING)
        self.assertIsNone(resumed.pause_reason)
        self.assertTrue(worker.resumed.wait(2))
        running = scheduler.wait_for_status(task.task_id, {TaskStatus.RUNNING}, timeout=2)
        self.assertEqual(running.status, TaskStatus.RUNNING)
        # 恢复运行后不能再带着上一次的暂停原因。
        self.assertIsNone(running.pause_reason)
        worker.release.set()
        done = scheduler.wait_for_status(
            task.task_id, {TaskStatus.COMPLETED}, timeout=3)
        self.assertEqual(done.status, TaskStatus.COMPLETED)
        self.assertIsNone(done.pause_reason)

    def test_cancelling_a_paused_task_emits_one_cancel_event_and_clears_the_reason(self):
        worker = _ResumableWorker()
        scheduler = self._scheduler(workers={"scan": worker})
        emissions = []
        scheduler.subscribe(emissions.append)
        scheduler.start()
        task = scheduler.submit("scan", resume_policy=ResumePolicy.AUTO_SAFE)
        self.assertTrue(worker.started.wait(2))
        scheduler.pause_task(task.task_id, reason=PauseReason.USER)
        scheduler.wait_for_status(task.task_id, {TaskStatus.PAUSED}, timeout=2)

        first = scheduler.cancel_task(task.task_id)
        self.assertEqual(first.status, TaskStatus.CANCELLED)
        # 重复取消只回读同一行，不能再发一次终态事件。
        second = scheduler.cancel_task(task.task_id)
        self.assertEqual(second.status, TaskStatus.CANCELLED)

        final = scheduler.get_task(task.task_id)
        self.assertEqual(final.status, TaskStatus.CANCELLED)
        self.assertIsNone(final.error)
        self.assertIsNone(final.pause_reason)
        cancel_events = [record for record in emissions
                         if record.task_id == task.task_id
                         and record.status is TaskStatus.CANCELLED]
        # 暂停时发过的事件不算取消事件；取消转换本身只允许有一次终态事件。
        self.assertEqual(1, len(cancel_events))
        self.assertEqual(1, worker.runs)

    def test_repeated_idempotency_key_returns_original_task(self):
        worker = _WaitingWorker()
        scheduler = self._scheduler(workers={"scan": worker})
        first = scheduler.submit("scan", {"range": "all"}, idempotency_key="request-1")
        duplicate = scheduler.submit("scan", {"range": "all"}, idempotency_key="request-1")
        self.assertEqual(first.task_id, duplicate.task_id)
        self.assertEqual(len(scheduler.list_tasks()), 1)
        scheduler.close_admission()
        with self.assertRaises(AdmissionClosedError):
            scheduler.submit("scan", idempotency_key="request-2")

    def test_resource_keys_serialize_conflicting_tasks(self):
        first = _WaitingWorker()
        second = _WaitingWorker()
        scheduler = self._scheduler(
            workers={"write": lambda: first if not first.started.is_set() else second},
            max_workers=2,
        )
        # This factory returns the first worker to the first dispatch and the
        # second worker to the next dispatch; both tasks share one account key.
        scheduler.start()
        one = scheduler.submit("write", resource_keys=["bili-account:42"])
        self.assertTrue(first.started.wait(1))
        two = scheduler.submit("write", resource_keys=["bili-account:42"])
        time.sleep(0.08)
        self.assertFalse(second.started.is_set())
        first.release.set()
        self.assertTrue(second.started.wait(1))
        second.release.set()
        self.assertEqual(
            scheduler.wait_for_status(one.task_id, {TaskStatus.COMPLETED}, timeout=1).status,
            TaskStatus.COMPLETED,
        )
        self.assertEqual(
            scheduler.wait_for_status(two.task_id, {TaskStatus.COMPLETED}, timeout=1).status,
            TaskStatus.COMPLETED,
        )

    def test_startup_marks_running_as_interrupted_and_validates_checkpoint_before_resume(self):
        store = InMemoryTaskStore()
        stale = TaskRecord(
            task_type="scan", status=TaskStatus.RUNNING,
            checkpoint={"cursor": 4}, resume_policy=ResumePolicy.AUTO_SAFE,
        )
        stale, _ = store.create(stale)
        worker = _CheckpointWorker()
        scheduler = self._scheduler(store, {"scan": worker})
        scheduler.start()
        interrupted = scheduler.get_task(stale.task_id)
        self.assertEqual(interrupted.status, TaskStatus.INTERRUPTED)
        self.assertEqual(interrupted.error["category"], "process_interrupted")

        resumed = scheduler.resume_task(stale.task_id)
        self.assertEqual(resumed.status, TaskStatus.RESUMING)
        done = scheduler.wait_for_status(stale.task_id, {TaskStatus.COMPLETED}, timeout=2)
        self.assertEqual(done.checkpoint, {"cursor": 5})
        self.assertEqual(worker.seen, [{"cursor": 4}])
        self.assertEqual(done.attempt, 1)

    def test_invalid_checkpoint_becomes_failed_without_discarding_checkpoint(self):
        store = InMemoryTaskStore()
        stale, _ = store.create(TaskRecord(
            task_type="scan", status=TaskStatus.RUNNING, checkpoint={"cursor": 9},
            resume_policy=ResumePolicy.AUTO_SAFE,
        ))
        worker = _CheckpointWorker()
        scheduler = self._scheduler(store, {"scan": worker})
        scheduler.start()
        scheduler.resume_task(stale.task_id)
        failed = scheduler.wait_for_status(stale.task_id, {TaskStatus.FAILED}, timeout=2)
        self.assertEqual(failed.error["category"], "checkpoint_invalid")
        self.assertEqual(failed.checkpoint, {"cursor": 9})
        self.assertEqual(worker.seen, [])

    def test_crashed_cancel_request_stays_cancelled_and_uncertain_write_needs_reconciliation(self):
        store = InMemoryTaskStore()
        cancelling, _ = store.create(TaskRecord(task_type="scan", status=TaskStatus.CANCELLING))
        uncertain, _ = store.create(TaskRecord(
            task_type="write", status=TaskStatus.RUNNING,
            reconciliation_required=True, reconciliation_reason="request outcome unknown",
        ))
        worker = _CheckpointWorker()
        scheduler = self._scheduler(store, {"scan": worker, "write": worker})
        scheduler.start()

        self.assertEqual(scheduler.get_task(cancelling.task_id).status, TaskStatus.CANCELLED)
        needs = scheduler.get_task(uncertain.task_id)
        self.assertEqual(needs.status, TaskStatus.NEEDS_RECONCILIATION)
        self.assertEqual(needs.reconciliation_reason, "request outcome unknown")
        self.assertEqual(worker.seen, [])

    def test_reconciliation_hook_must_confirm_before_resume(self):
        class ReconcileWorker:
            def __init__(self):
                self.reconcile_calls = 0
                self.execute_calls = 0

            def reconcile(self, context):
                self.reconcile_calls += 1
                context.save_checkpoint({"remote_confirmed": True})
                return ReconciliationResult(True, True, "已核对远端状态")

            def validate_checkpoint(self, context):
                return context.task.checkpoint == {"remote_confirmed": True}

            def execute(self, context):
                self.execute_calls += 1

        store = InMemoryTaskStore()
        task, _ = store.create(TaskRecord(
            task_type="write", status=TaskStatus.NEEDS_RECONCILIATION,
            reconciliation_required=True, reconciliation_reason="response lost",
        ))
        worker = ReconcileWorker()
        scheduler = self._scheduler(store, {"write": worker})
        scheduler.start()
        scheduler.resume_task(task.task_id)
        done = scheduler.wait_for_status(task.task_id, {TaskStatus.COMPLETED}, timeout=2)
        self.assertEqual(done.status, TaskStatus.COMPLETED)
        self.assertEqual(worker.reconcile_calls, 1)
        self.assertEqual(worker.execute_calls, 1)

    def test_vault_pause_barrier_and_auto_resume_excludes_user_pause(self):
        worker = _PausingWorker()
        scheduler = self._scheduler(workers={"scan": worker})
        scheduler.start()
        vault_task = scheduler.submit("scan", resume_policy=ResumePolicy.AUTO_SAFE)
        self.assertTrue(worker.started.wait(1))
        scheduler.close_admission()
        scheduler.request_pause_all(
            reason=PauseReason.VAULT_LOCK,
            resume_intent=ResumeIntent.AFTER_VAULT_UNLOCK,
        )
        self.assertTrue(scheduler.wait_for_idle(timeout=1))
        self.assertEqual(scheduler.get_task(vault_task.task_id).status, TaskStatus.PAUSED)

        scheduler.reopen_admission()
        resumed = scheduler.resume_after_vault_unlock()
        self.assertEqual([row.task_id for row in resumed], [vault_task.task_id])
        final = scheduler.wait_for_status(vault_task.task_id, {TaskStatus.COMPLETED}, timeout=2)
        self.assertEqual(final.status, TaskStatus.COMPLETED)

    def test_vault_barrier_rejects_task_resume_while_admission_is_closed(self):
        store = InMemoryTaskStore()
        worker = _CompletingWorker()
        scheduler = self._scheduler(store, {"scan": worker})
        scheduler.start()
        task, _ = store.create(TaskRecord(
            task_type="scan", status=TaskStatus.PAUSED,
            resume_policy=ResumePolicy.AUTO_SAFE,
            pause_reason=PauseReason.VAULT_LOCK,
            resume_intent=ResumeIntent.AFTER_VAULT_UNLOCK,
            owner_epoch=scheduler.owner_epoch,
        ))
        scheduler.close_admission()

        with self.assertRaises(AdmissionClosedError):
            scheduler.resume_task(task.task_id)
        self.assertEqual(scheduler.get_task(task.task_id).status, TaskStatus.PAUSED)
        self.assertEqual(worker.calls, 0)

    def test_restart_marks_old_owner_paused_tasks_interrupted_and_preserves_checkpoints(self):
        store = InMemoryTaskStore()
        old_owner_epoch = store.claim_owner_epoch("previous-process")
        stale_vault_pause, _ = store.create(TaskRecord(
            task_type="scan", status=TaskStatus.PAUSED,
            resume_policy=ResumePolicy.AUTO_SAFE,
            pause_reason=PauseReason.VAULT_LOCK,
            resume_intent=ResumeIntent.AFTER_VAULT_UNLOCK,
            owner_epoch=old_owner_epoch,
            checkpoint={"version": 1, "done": 5},
        ))
        user_pause, _ = store.create(TaskRecord(
            task_type="scan", status=TaskStatus.PAUSED,
            resume_policy=ResumePolicy.AUTO_SAFE,
            pause_reason=PauseReason.USER,
            resume_intent=ResumeIntent.MANUAL,
            owner_epoch=old_owner_epoch,
        ))
        scheduler = self._scheduler(store, {"scan": _CheckpointWorker()})
        scheduler.start()

        interrupted = scheduler.get_task(stale_vault_pause.task_id)
        self.assertEqual(interrupted.status, TaskStatus.INTERRUPTED)
        self.assertEqual(interrupted.checkpoint, {"version": 1, "done": 5})
        self.assertEqual(interrupted.error["category"], "paused_task_after_restart")
        self.assertEqual(interrupted.pause_reason, PauseReason.VAULT_LOCK)

        interrupted_user_pause = scheduler.get_task(user_pause.task_id)
        self.assertEqual(interrupted_user_pause.status, TaskStatus.INTERRUPTED)
        self.assertEqual(interrupted_user_pause.pause_reason, PauseReason.USER)
        self.assertEqual(interrupted_user_pause.error["category"],
                         "paused_task_after_restart")

    def test_second_scheduler_owner_is_rejected(self):
        store = InMemoryTaskStore()
        first = self._scheduler(store=store)
        second = self._scheduler(store=store)
        first.start()
        with self.assertRaises(SchedulerOwnershipError):
            second.start()


class SQLiteTaskStoreTests(unittest.TestCase):
    @staticmethod
    def _install_schema(database_path):
        # Simulate store._initialize_schema(conn): migration owns DDL and commit.
        conn = sqlite3.connect(database_path)
        try:
            SQLiteTaskStore.create_schema(conn)
            conn.commit()
        finally:
            conn.close()

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "missing.sqlite3"
            store = SQLiteTaskStore.for_existing_database_for_tests(database_path)
            with self.assertRaises(DatabaseNotFoundError):
                store.ensure_schema()
            self.assertFalse(database_path.exists())

    def test_uses_injected_factory_and_closes_each_short_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "existing.sqlite3"
            sqlite3.connect(database_path).close()
            self._install_schema(database_path)
            opened = []

            def factory():
                conn = sqlite3.connect(
                    f"file:{database_path.as_posix()}?mode=rw", uri=True,
                    isolation_level=None,
                )
                opened.append(conn)
                return conn

            store = SQLiteTaskStore(
                connection_factory=factory,
                database_exists=database_path.is_file,
            )
            store.ensure_schema()
            record, created = store.create(TaskRecord(
                task_type="scan", parameters={"folder": "5"},
                idempotency_key="scan-5",
            ))
            self.assertTrue(created)
            store.update(
                TaskRecord(
                    task_type=record.task_type,
                    task_id=record.task_id,
                    parameters=record.parameters,
                    checkpoint={"page": 2},
                    status=TaskStatus.PAUSED,
                    idempotency_key=record.idempotency_key,
                    revision=record.revision,
                ),
                expected_revision=record.revision,
            )
            loaded = store.get(record.task_id)
            duplicate, was_created = store.create(TaskRecord(
                task_type="scan", idempotency_key="scan-5",
            ))
            self.assertFalse(was_created)
            self.assertEqual(duplicate.task_id, record.task_id)
            self.assertEqual(loaded.checkpoint, {"page": 2})
            self.assertGreaterEqual(len(opened), 4)
            for conn in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")

    def test_connection_setup_failure_releases_lifecycle_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "existing.sqlite3"
            sqlite3.connect(database_path).close()

            class TrackingLifecycle:
                def __init__(self):
                    self.path = database_path
                    self.leases = 0

                def connect(self, *, mode, owner):
                    self.leases += 1
                    lifecycle = self

                    class FailingPragmaConnection(sqlite3.Connection):
                        def __init__(self, *args, **kwargs):
                            kwargs.pop("factory", None)
                            super().__init__(*args, **kwargs)
                            self._released = False

                        def execute(self, sql, *args, **kwargs):
                            if sql.strip().lower() == "pragma foreign_keys=on":
                                raise sqlite3.DatabaseError("injected PRAGMA failure")
                            return super().execute(sql, *args, **kwargs)

                        def close(self):
                            try:
                                return super().close()
                            finally:
                                if not self._released:
                                    self._released = True
                                    lifecycle.leases -= 1

                    return sqlite3.connect(
                        f"file:{database_path.as_posix()}?mode={mode}",
                        uri=True,
                        factory=FailingPragmaConnection,
                    )

            lifecycle = TrackingLifecycle()
            task_store = SQLiteTaskStore.from_lifecycle(lifecycle)
            with self.assertRaisesRegex(sqlite3.DatabaseError, "injected PRAGMA failure"):
                task_store._connect()
            self.assertEqual(0, lifecycle.leases)

    def test_ensure_schema_only_verifies_migration_installed_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "existing.sqlite3"
            sqlite3.connect(database_path).close()
            store = SQLiteTaskStore.for_existing_database_for_tests(database_path)
            with self.assertRaisesRegex(RuntimeError, "Schema 尚未安装"):
                store.ensure_schema()
            conn = sqlite3.connect(database_path)
            try:
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                conn.close()
            self.assertNotIn("bfo_scheduler_tasks", tables)

    def test_create_schema_uses_the_callers_migration_connection(self):
        connection = sqlite3.connect(":memory:")
        try:
            SQLiteTaskStore.create_schema(connection)
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("bfo_scheduler_tasks", tables)
            self.assertIn("bfo_scheduler_meta", tables)
            # The caller's connection remains open and controls commit/rollback.
            connection.execute("INSERT INTO bfo_scheduler_meta(key,value) VALUES('probe','1')")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
