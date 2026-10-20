"""HTTP contract coverage for remote-operation reconciliation actions."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from core.remote_operations import (
    OperationIntent,
    OperationStatus,
    RemoteOperationCoordinator,
    SqliteRemoteOperationRepository,
)


class RemoteOperationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "operations.sqlite3"
        self.connection = sqlite3.connect(self.db_path)
        SqliteRemoteOperationRepository.create_schema(self.connection)
        self.repo = SqliteRemoteOperationRepository(self.connection)
        intent = OperationIntent(
            task_id="api-review-task", account_id="api-review-account",
            operation_type="update_folder_intro", target={"media_id": "10"},
            preconditions={"title": "收藏夹", "privacy": 0},
            postconditions={"intro_sha256": "desired"},
            request_summary={"intro_length": 5},
        )
        operation = self.repo.prepare(intent)
        try:
            RemoteOperationCoordinator(self.repo).execute(
                operation.operation_id,
                lambda _current: (_ for _ in ()).throw(TimeoutError("lost response")),
            )
        except TimeoutError:
            pass
        self.operation_id = operation.operation_id

    def tearDown(self):
        self.connection.close()
        self.temp_dir.cleanup()

    def _post_reconcile(self, *, decision, evidence, preconditions_valid=None):
        body = server.RemoteOperationReconcileIn(
            decision=decision,
            evidence=evidence,
            preconditions_valid=preconditions_valid,
        )
        with patch.object(server, "_ensure_task_scheduler", return_value=None), \
             patch.object(server, "_TASK_SCHEDULER", None), \
             patch.object(server.store, "_connect",
                          side_effect=lambda **_kwargs: sqlite3.connect(self.db_path)):
            return server.remote_operation_reconcile(self.operation_id, body)

    def test_manual_review_is_reachable_and_can_resolve_with_evidence(self):
        missing = self._post_reconcile(decision="manual_review", evidence="   ")
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(self.repo.get(self.operation_id).status,
                         OperationStatus.NEEDS_RECONCILIATION)

        not_applied = self._post_reconcile(
            decision="not_applied", preconditions_valid=True,
            evidence="已在 B 站核对来源状态仍保留目标内容",
        )
        self.assertTrue(not_applied["ok"])
        self.assertEqual(not_applied["operation"]["status"], "NOT_APPLIED")

        manual = self._post_reconcile(
            decision="manual_review", evidence="仍无法判断，转人工复核",
        )
        self.assertTrue(manual["ok"])
        self.assertEqual(manual["operation"]["status"], "MANUAL_REVIEW")

        confirmed = self._post_reconcile(
            decision="confirmed", evidence="重新打开 B 站后已确认简介与目标完全一致",
        )
        self.assertTrue(confirmed["ok"])
        self.assertEqual(confirmed["operation"]["status"], "CONFIRMED")
        self.assertEqual(
            [event.to_status for event in self.repo.events(self.operation_id)][-3:],
            [OperationStatus.NOT_APPLIED, OperationStatus.MANUAL_REVIEW,
             OperationStatus.CONFIRMED],
        )

    def test_manual_review_can_return_to_retry_after_evidenced_precondition_check(self):
        self._post_reconcile(decision="manual_review", evidence="需要进一步复核")
        rejected = self._post_reconcile(
            decision="not_applied", preconditions_valid=False,
            evidence="远端前置状态没有通过检查",
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(self.repo.get(self.operation_id).status,
                         OperationStatus.MANUAL_REVIEW)

        retryable = self._post_reconcile(
            decision="not_applied", preconditions_valid=True,
            evidence="已重新检查，原来源集合完整且目标仍未改变",
        )
        self.assertTrue(retryable["ok"])
        self.assertEqual(retryable["operation"]["status"], "NOT_APPLIED")
        self.assertEqual(retryable["operation"]["operation_id"], self.operation_id)

    def test_public_operation_returns_redacted_reconciliation_without_response(self):
        self._post_reconcile(decision="manual_review", evidence="cookie=DO_NOT_EXPOSE")
        public = server._public_remote_operation(self.repo.get(self.operation_id))
        self.assertEqual(public["reconciliation"]["decision"], "manual_review")
        self.assertEqual(public["reconciliation"]["evidence"]["user_note"],
                         "cookie=[REDACTED]")
        self.assertNotIn("remote_response", public)
        self.assertNotIn("DO_NOT_EXPOSE", str(public))


if __name__ == "__main__":
    unittest.main()
