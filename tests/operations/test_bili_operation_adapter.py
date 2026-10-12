"""Integration-contract tests for the composite Bilibili write adapter."""
from __future__ import annotations

import sqlite3
import unittest

import bili_api
from core.bili_operation_adapter import (
    BiliOperationAdapter,
    OperationExecutionBlocked,
    OperationPreviouslyRejected,
)
from core.remote_operations import (
    OperationStatus,
    RemoteOperationCoordinator,
    SqliteRemoteOperationRepository,
    UnsafeRetry,
)


class FakeBiliSession:
    def __init__(self):
        self.calls = []
        self.failures = {}

    def _call(self, name, result, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        failure = self.failures.get(name)
        if failure:
            if isinstance(failure, list):
                failure = failure.pop(0)
            if failure:
                raise failure
        return result

    def list_folders(self):
        return [{"media_id": "10", "title": "原夹"}]

    def create_folder(self, title):
        return self._call("create_folder", "folder-101", title)

    def rename_folder(self, media_id, title, *, should_stop=None):
        return self._call("rename_folder", None, media_id, title,
                          should_stop=should_stop)

    def update_folder_intro(self, media_id, folder_info, intro, *, should_stop=None):
        return self._call("update_folder_intro", {"updated": True}, media_id,
                          folder_info, intro, should_stop=should_stop)

    def delete_folder(self, media_id, *, should_stop=None):
        return self._call("delete_folder", None, media_id, should_stop=should_stop)

    def clean_invalid_folder(self, media_id, *, should_stop=None):
        return self._call("clean_invalid_folder", None, media_id,
                          should_stop=should_stop)

    def add_with_aid(self, media_id, aid):
        return self._call("add_with_aid", True, media_id, aid)

    def remove_from_folder(self, media_id, aid):
        return self._call("remove_from_folder", True, media_id, aid)

    def move_batch(self, src_media_id, tar_media_id, aids, *, mid="", should_stop=None):
        return self._call("move_batch", len(dict.fromkeys(aids)), src_media_id,
                          tar_media_id, aids, mid=mid, should_stop=should_stop)

    def batch_delete(self, media_id, aids, *, should_stop=None):
        return self._call("batch_delete", len(dict.fromkeys(aids)), media_id,
                          aids, should_stop=should_stop)


class BiliOperationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("CREATE TABLE existing_business_data (value TEXT)")
        SqliteRemoteOperationRepository.create_schema(self.connection)
        self.repo = SqliteRemoteOperationRepository(self.connection)
        self.coordinator = RemoteOperationCoordinator(self.repo)
        self.session = FakeBiliSession()
        self.adapter = BiliOperationAdapter(
            self.session, self.coordinator,
            task_id="task-stable-1", account_id="account-stable-1",
        )

    def tearDown(self):
        self.connection.close()

    def test_all_nine_write_methods_are_logged_and_delegate_original_arguments(self):
        stop = lambda: False
        self.assertEqual(self.adapter.create_folder("新夹"), "folder-101")
        self.assertIsNone(self.adapter.rename_folder("10", "重命名", should_stop=stop))
        intro = "仅保存在业务调用中的简介，不进入操作日志"
        self.assertEqual(
            self.adapter.update_folder_intro("10", {"title": "重命名", "attr": 1},
                                             intro, should_stop=stop),
            {"updated": True},
        )
        self.assertIsNone(self.adapter.delete_folder("11", should_stop=stop))
        self.assertIsNone(self.adapter.clean_invalid_folder("12", should_stop=stop))
        self.assertTrue(self.adapter.add_with_aid("20", 101))
        self.assertTrue(self.adapter.remove_from_folder("20", 102))
        self.assertEqual(self.adapter.move_batch("10", "20", [1, 2, 1],
                                                 mid="123", should_stop=stop), 2)
        self.assertEqual(self.adapter.batch_delete("10", [3, 4, 3],
                                                   should_stop=stop), 2)

        self.assertEqual(
            [call[0] for call in self.session.calls],
            ["create_folder", "rename_folder", "update_folder_intro", "delete_folder",
             "clean_invalid_folder", "add_with_aid", "remove_from_folder", "move_batch",
             "batch_delete"],
        )
        operations = self.repo.list_operations(task_id="task-stable-1")
        self.assertEqual(len(operations), 9)
        self.assertTrue(all(op.status is OperationStatus.CONFIRMED for op in operations))
        intro_op = next(op for op in operations if op.operation_type == "update_folder_intro")
        self.assertEqual(intro_op.request_summary["intro_length"], len(intro))
        self.assertNotIn(intro, str(intro_op))
        self.assertEqual(self.adapter.list_folders(), [{"media_id": "10", "title": "原夹"}])

    def test_confirmed_intent_returns_saved_result_without_sending_twice(self):
        self.assertEqual(self.adapter.create_folder("复用结果"), "folder-101")
        self.assertEqual(self.adapter.create_folder("复用结果"), "folder-101")
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(self.repo.get(self.adapter.last_operation_id).attempt, 1)

    def test_uncertain_exception_is_persisted_and_replay_is_blocked(self):
        self.session.failures["move_batch"] = bili_api.WriteUncertainError("no response")
        with self.assertRaises(bili_api.WriteUncertainError):
            self.adapter.move_batch("10", "20", [1, 2])
        operation_id = self.adapter.last_operation_id
        self.assertEqual(self.repo.get(operation_id).status,
                         OperationStatus.NEEDS_RECONCILIATION)
        with self.assertRaises(OperationExecutionBlocked):
            self.adapter.move_batch("10", "20", [1, 2])
        self.assertEqual(len(self.session.calls), 1)

    def test_retry_requires_reconciliation_fresh_validation_and_same_id(self):
        self.session.failures["move_batch"] = [bili_api.WriteUncertainError("lost"), None]
        with self.assertRaises(bili_api.WriteUncertainError):
            self.adapter.move_batch("10", "20", [1, 2])
        operation_id = self.adapter.last_operation_id
        self.repo.record_reconciliation(
            operation_id, decision="not_applied", preconditions_valid=True,
            evidence={"remote_source_contains": [1, 2]},
        )
        with self.assertRaises(UnsafeRetry):
            self.adapter.move_batch("10", "20", [1, 2], retry=True)
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(self.adapter.move_batch(
            "10", "20", [1, 2], retry=True,
            validate_retry=lambda operation: operation.preconditions["source_contains_all"],
        ), 2)
        self.assertEqual(self.adapter.last_operation_id, operation_id)
        self.assertEqual(self.repo.get(operation_id).attempt, 2)
        self.assertEqual(self.repo.get(operation_id).status, OperationStatus.CONFIRMED)

    def test_explicitly_authorized_not_applied_intent_can_retry_on_resume(self):
        self.session.failures["move_batch"] = [bili_api.WriteUncertainError("lost"), None]
        with self.assertRaises(bili_api.WriteUncertainError):
            self.adapter.move_batch("10", "20", [1, 2])
        operation_id = self.adapter.last_operation_id
        self.repo.record_reconciliation(
            operation_id, decision="not_applied", preconditions_valid=True,
            evidence={"checked": "source still contains both items"},
        )
        self.adapter.authorize_retry(operation_id)
        self.assertEqual(self.adapter.move_batch("10", "20", [1, 2]), 2)
        self.assertEqual(self.adapter.last_operation_id, operation_id)
        self.assertEqual(self.repo.get(operation_id).attempt, 2)
        self.assertEqual(self.repo.get(operation_id).status, OperationStatus.CONFIRMED)

    def test_only_explicit_api_response_evidence_is_rejected(self):
        self.session.failures["rename_folder"] = bili_api.BiliApiError(
            "B站返回 code=-400：参数被拒绝"
        )
        with self.assertRaises(bili_api.BiliApiError):
            self.adapter.rename_folder("10", "无效名")
        rejected_id = self.adapter.last_operation_id
        self.assertEqual(self.repo.get(rejected_id).status, OperationStatus.REJECTED)
        with self.assertRaises(OperationPreviouslyRejected):
            self.adapter.rename_folder("10", "无效名")
        self.assertEqual(len(self.session.calls), 1)

        self.session.failures["create_folder"] = bili_api.BiliApiError(
            "请求失败：https://api.example/write (connection reset)"
        )
        with self.assertRaises(bili_api.BiliApiError):
            self.adapter.create_folder("网络不明")
        self.assertEqual(self.repo.get(self.adapter.last_operation_id).status,
                         OperationStatus.NEEDS_RECONCILIATION)

    def test_false_add_and_remove_results_are_recorded_as_rejected(self):
        self.session.add_with_aid = lambda media_id, aid: self.session._call(
            "add_with_aid", False, media_id, aid
        )
        self.session.remove_from_folder = lambda media_id, aid: self.session._call(
            "remove_from_folder", False, media_id, aid
        )
        self.assertFalse(self.adapter.add_with_aid("10", 501))
        add_id = self.adapter.last_operation_id
        self.assertEqual(self.repo.get(add_id).status, OperationStatus.REJECTED)
        self.assertFalse(self.adapter.add_with_aid("10", 501))
        self.assertFalse(self.adapter.remove_from_folder("10", 501))
        remove_id = self.adapter.last_operation_id
        self.assertEqual(self.repo.get(remove_id).status, OperationStatus.REJECTED)
        self.assertFalse(self.adapter.remove_from_folder("10", 501))
        self.assertEqual(len(self.session.calls), 2)

    def test_response_mismatch_is_unknown_instead_of_confirmed(self):
        self.session.move_batch = lambda src, dst, aids, **kwargs: self.session._call(
            "move_batch", 1, src, dst, aids, **kwargs
        )
        with self.assertRaises(OperationExecutionBlocked):
            self.adapter.move_batch("10", "20", [1, 2])
        self.assertEqual(self.repo.get(self.adapter.last_operation_id).status,
                         OperationStatus.NEEDS_RECONCILIATION)

    def test_new_task_id_creates_distinct_operation_identity(self):
        self.adapter.create_folder("同名夹")
        first_id = self.adapter.last_operation_id
        another_task = BiliOperationAdapter(
            self.session, self.coordinator,
            task_id="task-stable-2", account_id="account-stable-1",
        )
        another_task.create_folder("同名夹")
        self.assertNotEqual(first_id, another_task.last_operation_id)
        self.assertEqual(len(self.session.calls), 2)


if __name__ == "__main__":
    unittest.main()
