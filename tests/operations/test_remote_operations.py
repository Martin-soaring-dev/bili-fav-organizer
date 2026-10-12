"""Safety tests for the first remote-operation persistence contract."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.remote_operations import (
    ConflictingOperation,
    InvalidOperationTransition,
    OperationIdentityConflict,
    OperationIntent,
    OperationStatus,
    RemoteOperationCoordinator,
    RepositoryStateError,
    SqliteRemoteOperationRepository,
    UnsafeRetry,
    stable_operation_id,
)


class RemoteOperationTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("CREATE TABLE business_effects (value TEXT NOT NULL)")
        self.repo = SqliteRemoteOperationRepository(self.connection)

    def tearDown(self):
        self.connection.close()

    def install_schema(self):
        SqliteRemoteOperationRepository.create_schema(self.connection)

    def intent(self, **overrides):
        values = {
            "task_id": "task-1",
            "account_id": "account-123",
            "operation_type": "move_videos",
            "target": {"source_folder_id": "10", "target_folder_id": "20"},
            "entity_ids": [101, 102],
            "preconditions": {"source_contains_all": True},
            "postconditions": {"target_contains_all": True},
            "request_summary": {"batch_size": 2},
        }
        values.update(overrides)
        return OperationIntent(**values)

    def test_repository_construction_does_not_install_tables(self):
        tables = {
            row[0] for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertEqual(tables, {"business_effects"})

    def test_schema_version_mismatch_is_rejected_before_table_changes(self):
        self.connection.execute(
            "CREATE TABLE remote_operation_schema_version (version INTEGER NOT NULL)"
        )
        self.connection.execute(
            "INSERT INTO remote_operation_schema_version(version) VALUES (2)"
        )
        self.connection.commit()
        with self.assertRaisesRegex(RepositoryStateError, "schema 版本不兼容"):
            SqliteRemoteOperationRepository.create_schema(self.connection)
        tables = {
            row[0] for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertEqual(tables, {"business_effects", "remote_operation_schema_version"})

    def test_stable_id_is_canonical_and_scoped_to_task(self):
        first = stable_operation_id(
            task_id="task-1", account_id="a", operation_type="move",
            target={"src": "1", "dst": "2"}, entity_ids=[3, 2, 3],
            request_summary={"count": 2, "csrf": "never-hash-this"},
        )
        reordered = stable_operation_id(
            task_id="task-1", account_id="a", operation_type="move",
            target={"dst": "2", "src": "1"}, entity_ids=[2, 3],
            request_summary={"csrf": "different-secret", "count": 2},
        )
        another_task = stable_operation_id(
            task_id="task-2", account_id="a", operation_type="move",
            target={"src": "1", "dst": "2"}, entity_ids=[2, 3],
            request_summary={"count": 2},
        )
        self.assertEqual(first, reordered)
        self.assertNotEqual(first, another_task)

    def test_prepare_is_idempotent_and_rejects_identity_reuse(self):
        self.install_schema()
        intent = self.intent()
        first = self.repo.prepare(intent)
        second = self.repo.prepare(intent)
        self.assertEqual(first.operation_id, second.operation_id)
        self.assertEqual(len(self.repo.events(first.operation_id)), 1)
        changed = self.intent(request_summary={"batch_size": 7})
        self.assertNotEqual(first.operation_id, changed.operation_id)
        self.assertNotEqual(self.repo.prepare(changed).operation_id, first.operation_id)
        # Simulate corrupted/hand-edited persisted identity under the original
        # primary key; it must never be silently replaced by prepare().
        self.connection.execute(
            "UPDATE remote_operations SET intent_json='{}' WHERE operation_id=?",
            (first.operation_id,),
        )
        self.connection.commit()
        with self.assertRaises(OperationIdentityConflict):
            self.repo.prepare(intent)

    def test_dispatch_is_committed_before_network_callback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "existing.sqlite3"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE business_effects (value TEXT NOT NULL)")
            SqliteRemoteOperationRepository.create_schema(connection)
            repo = SqliteRemoteOperationRepository(connection)
            coordinator = RemoteOperationCoordinator(repo)
            op = coordinator.prepare(self.intent())

            def sender(dispatching):
                self.assertEqual(dispatching.status, OperationStatus.DISPATCHING)
                # A separate connection sees the committed state before the
                # network callback is allowed to run.
                observer = sqlite3.connect(db_path)
                try:
                    status = observer.execute(
                        "SELECT status FROM remote_operations WHERE operation_id=?",
                        (op.operation_id,),
                    ).fetchone()[0]
                finally:
                    observer.close()
                self.assertEqual(status, "DISPATCHING")
                self.assertFalse(connection.in_transaction)
                return {"accepted": True}

            result = coordinator.execute(
                op.operation_id, sender,
                summarize_response=lambda response: {"accepted": response["accepted"]},
            )
            self.assertEqual(result, {"accepted": True})
            self.assertEqual(repo.get(op.operation_id).status, OperationStatus.CONFIRMED)
            connection.close()

    def test_sender_exception_becomes_unknown_and_cannot_be_replayed(self):
        self.install_schema()
        coordinator = RemoteOperationCoordinator(self.repo)
        op = coordinator.prepare(self.intent())
        calls = []

        def sender(_):
            calls.append("sent")
            raise TimeoutError("response lost")

        with self.assertRaises(TimeoutError):
            coordinator.execute(op.operation_id, sender)
        self.assertEqual(self.repo.get(op.operation_id).status,
                         OperationStatus.NEEDS_RECONCILIATION)
        with self.assertRaises(InvalidOperationTransition):
            coordinator.execute(op.operation_id, sender)
        self.assertEqual(calls, ["sent"])

    def test_retry_requires_reconciliation_and_reuses_same_operation_id(self):
        self.install_schema()
        coordinator = RemoteOperationCoordinator(self.repo)
        op = coordinator.prepare(self.intent())
        with self.assertRaises(TimeoutError):
            coordinator.execute(op.operation_id, lambda _: (_ for _ in ()).throw(TimeoutError()))
        with self.assertRaises(UnsafeRetry):
            coordinator.execute(op.operation_id, lambda _: "should-not-run", retry=True)
        self.repo.record_reconciliation(
            op.operation_id, decision="not_applied", preconditions_valid=True,
            evidence={"source_still_contains": ["101", "102"]},
        )
        seen = []
        coordinator.execute(
            op.operation_id,
            lambda dispatched: seen.append(dispatched.operation_id) or {"ok": True},
            retry=True,
            validate_retry=lambda current: current.preconditions["source_contains_all"],
        )
        final = self.repo.get(op.operation_id)
        self.assertEqual(seen, [op.operation_id])
        self.assertEqual(final.operation_id, op.operation_id)
        self.assertEqual(final.attempt, 2)
        self.assertEqual(final.status, OperationStatus.CONFIRMED)

    def test_manual_review_is_evidenced_and_can_be_resolved(self):
        self.install_schema()
        coordinator = RemoteOperationCoordinator(self.repo)
        operation = coordinator.prepare(self.intent())
        with self.assertRaises(TimeoutError):
            coordinator.execute(operation.operation_id,
                                lambda _: (_ for _ in ()).throw(TimeoutError()))

        with self.assertRaises(UnsafeRetry):
            self.repo.record_reconciliation(
                operation.operation_id, decision="manual_review", evidence="  "
            )
        with self.assertRaises(UnsafeRetry):
            self.repo.record_reconciliation(
                operation.operation_id, decision="manual_review",
                evidence={"user_note": "  "},
            )
        with self.assertRaises(UnsafeRetry):
            self.repo.record_reconciliation(
                operation.operation_id, decision="confirmed", evidence={},
            )
        not_applied = self.repo.record_reconciliation(
            operation.operation_id, decision="not_applied",
            preconditions_valid=True,
            evidence={"source_still_contains": [101, 102]},
        )
        self.assertEqual(not_applied.status, OperationStatus.NOT_APPLIED)
        with self.assertRaises(InvalidOperationTransition):
            self.repo.record_reconciliation(
                operation.operation_id, decision="confirmed",
                evidence={"checked": "must not skip the manual-review state"},
            )
        manual = self.repo.record_reconciliation(
            operation.operation_id, decision="manual_review",
            evidence={"review": "仍无法判断，转人工复核"},
        )
        self.assertEqual(manual.status, OperationStatus.MANUAL_REVIEW)

        # MANUAL_REVIEW continues to hold the resource until an evidenced
        # resolution; after a human confirms the remote effect, new writes can
        # proceed without repeating the old request.
        with self.assertRaises(ConflictingOperation):
            self.repo.prepare(self.intent(task_id="task-2"))
        confirmed = self.repo.record_reconciliation(
            operation.operation_id, decision="confirmed",
            evidence={"checked": "B 站收藏夹内容已显示目标结果"},
        )
        self.assertEqual(confirmed.status, OperationStatus.CONFIRMED)
        self.assertIsNotNone(confirmed.resolved_at)
        self.assertEqual(
            [event.to_status for event in self.repo.events(operation.operation_id)],
            [OperationStatus.PREPARED, OperationStatus.DISPATCHING,
             OperationStatus.NEEDS_RECONCILIATION, OperationStatus.NOT_APPLIED,
             OperationStatus.MANUAL_REVIEW, OperationStatus.CONFIRMED],
        )
        self.repo.prepare(self.intent(task_id="task-2"))

    def test_manual_review_can_return_to_safe_retry_path(self):
        self.install_schema()
        coordinator = RemoteOperationCoordinator(self.repo)
        operation = coordinator.prepare(self.intent())
        with self.assertRaises(TimeoutError):
            coordinator.execute(operation.operation_id,
                                lambda _: (_ for _ in ()).throw(TimeoutError()))
        self.repo.record_reconciliation(
            operation.operation_id, decision="manual_review",
            evidence={"review": "需要进一步核对"},
        )
        with self.assertRaises(UnsafeRetry):
            self.repo.record_reconciliation(
                operation.operation_id, decision="not_applied",
                preconditions_valid=False,
                evidence={"checked": "前置条件已变化"},
            )
        self.repo.record_reconciliation(
            operation.operation_id, decision="not_applied",
            preconditions_valid=True,
            evidence={"checked": "来源仍包含全部视频，目标未变化"},
        )
        sent = []
        coordinator.execute(
            operation.operation_id,
            lambda current: sent.append(current.operation_id) or {"ok": True},
            retry=True,
            validate_retry=lambda current: current.preconditions["source_contains_all"],
        )
        self.assertEqual(sent, [operation.operation_id])
        self.assertEqual(self.repo.get(operation.operation_id).attempt, 2)
        self.assertEqual(self.repo.get(operation.operation_id).status,
                         OperationStatus.CONFIRMED)

    def test_new_conflicting_intent_is_rejected_before_prepared_row_is_created(self):
        self.install_schema()
        coordinator = RemoteOperationCoordinator(self.repo)
        first = coordinator.prepare(self.intent())
        with self.assertRaises(TimeoutError):
            coordinator.execute(first.operation_id,
                                lambda _: (_ for _ in ()).throw(TimeoutError()))
        before = self.repo.list_operations(account_id="account-123")
        with self.assertRaises(ConflictingOperation):
            self.repo.prepare(self.intent(
                task_id="new-direct-request", request_summary={"batch_size": 5}
            ))
        after = self.repo.list_operations(account_id="account-123")
        self.assertEqual([row.operation_id for row in before],
                         [row.operation_id for row in after])

    def test_not_applied_without_valid_preconditions_cannot_be_recorded(self):
        self.install_schema()
        coordinator = RemoteOperationCoordinator(self.repo)
        op = coordinator.prepare(self.intent())
        with self.assertRaises(TimeoutError):
            coordinator.execute(op.operation_id, lambda _: (_ for _ in ()).throw(TimeoutError()))
        with self.assertRaises(UnsafeRetry):
            self.repo.record_reconciliation(
                op.operation_id, decision="not_applied", preconditions_valid=False,
            )

    def test_local_commit_and_confirmed_status_share_one_transaction(self):
        self.install_schema()
        coordinator = RemoteOperationCoordinator(self.repo)
        op = coordinator.prepare(self.intent())

        def commit_local(conn, response):
            conn.execute("INSERT INTO business_effects(value) VALUES (?)", (response["id"],))
            return {"rows_written": 1}

        coordinator.execute(
            op.operation_id, lambda _: {"id": "remote-1"},
            local_commit=commit_local,
            summarize_response=lambda response: {"id": response["id"]},
        )
        self.assertEqual(self.connection.execute(
            "SELECT value FROM business_effects"
        ).fetchall(), [("remote-1",)])
        self.assertEqual(self.repo.get(op.operation_id).local_result, {"rows_written": 1})

    def test_failed_local_commit_rolls_back_and_leaves_operation_unresolved(self):
        self.install_schema()
        coordinator = RemoteOperationCoordinator(self.repo)
        op = coordinator.prepare(self.intent())

        def failing_commit(conn, _response):
            conn.execute("INSERT INTO business_effects(value) VALUES ('partial')")
            raise RuntimeError("local commit failed")

        with self.assertRaises(RuntimeError):
            coordinator.execute(op.operation_id, lambda _: {"ok": True},
                                local_commit=failing_commit)
        self.assertEqual(self.connection.execute(
            "SELECT value FROM business_effects"
        ).fetchall(), [])
        self.assertEqual(self.repo.get(op.operation_id).status,
                         OperationStatus.NEEDS_RECONCILIATION)

    def test_explicit_definite_rejection_is_terminal_and_recorded_without_message(self):
        self.install_schema()
        coordinator = RemoteOperationCoordinator(self.repo)
        op = coordinator.prepare(self.intent())
        with self.assertRaisesRegex(ValueError, "private details"):
            coordinator.execute(
                op.operation_id,
                lambda _: (_ for _ in ()).throw(ValueError("private details")),
                is_definite_rejection=lambda exc: isinstance(exc, ValueError),
            )
        record = self.repo.get(op.operation_id)
        self.assertEqual(record.status, OperationStatus.REJECTED)
        self.assertEqual(record.error_code, "remote_rejected:ValueError")
        self.assertNotIn("private details", str(self.repo.events(op.operation_id)))

    def test_write_uncertain_error_cannot_be_misclassified_as_rejection(self):
        self.install_schema()
        coordinator = RemoteOperationCoordinator(self.repo)
        op = coordinator.prepare(self.intent())

        class WriteUncertainError(RuntimeError):
            pass

        with self.assertRaises(WriteUncertainError):
            coordinator.execute(
                op.operation_id,
                lambda _: (_ for _ in ()).throw(WriteUncertainError("lost response")),
                is_definite_rejection=lambda _exc: True,
            )
        self.assertEqual(self.repo.get(op.operation_id).status,
                         OperationStatus.NEEDS_RECONCILIATION)

    def test_startup_recovers_stranded_dispatch_as_reconciliation(self):
        self.install_schema()
        op = self.repo.prepare(self.intent())
        self.repo.begin_dispatch(op.operation_id)
        self.assertEqual(self.repo.recover_stranded_dispatches(), 1)
        self.assertEqual(self.repo.get(op.operation_id).status,
                         OperationStatus.NEEDS_RECONCILIATION)
        self.assertEqual(self.repo.recover_stranded_dispatches(), 0)

    def test_unresolved_write_blocks_conflicting_account_scope(self):
        self.install_schema()
        coordinator = RemoteOperationCoordinator(self.repo)
        first = coordinator.prepare(self.intent())
        coordinator.prepare(self.intent(task_id="task-2", entity_ids=[999]))
        with self.assertRaises(TimeoutError):
            coordinator.execute(first.operation_id, lambda _: (_ for _ in ()).throw(TimeoutError()))
        second = stable_operation_id(
            task_id="task-2", account_id="account-123", operation_type="move_videos",
            target={"source_folder_id": "10", "target_folder_id": "20"},
            entity_ids=[999], preconditions={"source_contains_all": True},
            postconditions={"target_contains_all": True}, request_summary={"batch_size": 2},
        )
        with self.assertRaises(ConflictingOperation):
            coordinator.execute(second, lambda _: "must-not-send")

    def test_retry_requires_fresh_precondition_check(self):
        self.install_schema()
        coordinator = RemoteOperationCoordinator(self.repo)
        op = coordinator.prepare(self.intent())
        with self.assertRaises(TimeoutError):
            coordinator.execute(op.operation_id, lambda _: (_ for _ in ()).throw(TimeoutError()))
        self.repo.record_reconciliation(
            op.operation_id, decision="not_applied", preconditions_valid=True,
            evidence={"checked": "at-reconciliation"},
        )
        calls = []
        with self.assertRaises(UnsafeRetry):
            coordinator.execute(op.operation_id, lambda _: calls.append("sent"), retry=True,
                                validate_retry=lambda _: False)
        self.assertEqual(calls, [])
        with self.assertRaises(UnsafeRetry):
            coordinator.execute(op.operation_id, lambda _: calls.append("sent"), retry=True)
        self.assertEqual(calls, [])

    def test_sensitive_fields_are_redacted_from_persisted_intent_and_response(self):
        self.install_schema()
        coordinator = RemoteOperationCoordinator(self.repo)
        intent = self.intent(request_summary={"csrf": "secret-value", "batch_size": 2})
        self.assertEqual(intent.request_summary["csrf"], "[REDACTED]")
        op = coordinator.prepare(intent)
        coordinator.execute(op.operation_id, lambda _: {"ok": True},
                            summarize_response=lambda _: {"cookie": "secret-value", "ok": True})
        record = self.repo.get(op.operation_id)
        self.assertEqual(record.remote_response["cookie"], "[REDACTED]")
        self.assertNotIn("secret-value", str(record))


if __name__ == "__main__":
    unittest.main()
