import unittest
from unittest.mock import patch

import server
import store


FOLDERS = [
    {"media_id": "1", "title": "默认收藏夹", "count": 3},
    {"media_id": "2", "title": "硬件", "count": 4},
    {"media_id": "3", "title": "生活", "count": 5},
]


class DefaultFolderDetectionTests(unittest.TestCase):
    def test_detected_by_title(self):
        self.assertEqual("1", server._default_folder(FOLDERS)["media_id"])
        self.assertEqual({"1"}, server._default_folder_ids(FOLDERS))
        self.assertEqual({"默认收藏夹"}, server._default_folder_names(FOLDERS))

    def test_detected_by_attr_bit_when_renamed(self):
        # B 站 attr 位域 bit1=0 表示默认收藏夹（docs/fav/info.md）
        rows = [
            {"media_id": "9", "title": "改过名", "count": 2, "attr": 1},
            {"media_id": "2", "title": "硬件", "count": 3, "attr": 3},
        ]
        self.assertEqual("9", server._default_folder(rows)["media_id"])
        self.assertEqual({"改过名"}, server._default_folder_names(rows))

    def test_unknown_returns_none_instead_of_guessing(self):
        # 识别不出时不能随便挑一个夹当默认夹，否则会误拦正常的移入
        rows = [
            {"media_id": "2", "title": "硬件", "count": 3},
            {"media_id": "3", "title": "生活", "count": 5},
        ]
        self.assertIsNone(server._default_folder(rows))
        self.assertEqual(set(), server._default_folder_names(rows))
        self.assertEqual(set(), server._default_folder_ids(rows))

    def test_attr_without_match_does_not_flag_every_folder(self):
        # attr 语义判反的保险：只有一个夹被标记才认，否则不认
        rows = [
            {"media_id": "2", "title": "硬件", "count": 3, "attr": 2},
            {"media_id": "3", "title": "生活", "count": 5, "attr": 2},
        ]
        self.assertIsNone(server._default_folder(rows))


class DefaultFolderDestinationGuardTests(unittest.TestCase):
    def test_rejects_moving_into_default_folder(self):
        err = server._default_folder_destination_error(
            [{"action": "move_to_existing", "target_folder": "默认收藏夹"}])
        self.assertIn("默认收藏夹", err)

    def test_rejects_renaming_to_default_folder(self):
        err = server._default_folder_destination_error(
            [{"action": "create_new", "create_new_name": "默认收藏夹"}])
        self.assertIn("默认收藏夹", err)

    def test_allows_normal_targets(self):
        self.assertEqual("", server._default_folder_destination_error(
            [{"action": "move_to_existing", "target_folder": "硬件"},
             {"action": "create_new", "create_new_name": "新主题"}]))

    def test_no_default_folder_means_no_block(self):
        with patch.object(server.store, "load_folders",
                          return_value=[{"media_id": "2", "title": "硬件", "count": 3}]):
            self.assertEqual("", server._default_folder_destination_error(
                [{"action": "move_to_existing", "target_folder": "硬件"}]))


class ReadinessInboxTests(unittest.TestCase):
    def _readiness(self, folders, profiles, scans, counts):
        with patch.object(store, "load_folders", return_value=folders), \
             patch.object(store, "load_folder_profiles", return_value=profiles), \
             patch.object(store, "load_folder_scan_states", return_value=scans), \
             patch.object(store, "load_folder_item_counts", return_value=counts):
            return server._organization_profile_readiness()

    def test_default_folder_is_not_required_to_have_a_profile(self):
        profiles = {"2": {"summary": "硬件", "topics": ["a", "b"], "typical_content": [],
                          "out_of_scope": [], "coherence": "coherent", "confidence": 0.8,
                          "folder_name": "硬件", "source_item_count": 4,
                          "source_snapshot_at": "s2", "profile_revision": "r2"},
                    "3": {"summary": "生活", "topics": ["a", "b"], "typical_content": [],
                          "out_of_scope": [], "coherence": "coherent", "confidence": 0.8,
                          "folder_name": "生活", "source_item_count": 5,
                          "source_snapshot_at": "s3", "profile_revision": "r3"}}
        scans = {"2": {"status": "complete", "expected_count": 4, "snapshot_completed_at": "s2"},
                 "3": {"status": "complete", "expected_count": 5, "snapshot_completed_at": "s3"},
                 "1": {"status": "never"}}
        counts = {"2": 4, "3": 5, "1": 0}
        result = self._readiness(FOLDERS, profiles, scans, counts)
        self.assertTrue(result["ready"], result["message"])
        self.assertEqual(["1"], result["inbox_folder_ids"])
        self.assertEqual(1, result["inbox_count"])
        self.assertEqual(2, result["required_count"])

    def test_inbox_only_content_is_still_ready(self):
        # 只有收件箱有内容时也要能分析：LLM 仍可建议「新建收藏夹」
        folders = [{"media_id": "1", "title": "默认收藏夹", "count": 3}]
        result = self._readiness(folders, {}, {"1": {"status": "never"}}, {"1": 3})
        self.assertTrue(result["ready"], result["message"])
        self.assertEqual(0, result["required_count"])
        self.assertEqual(1, result["inbox_count"])


class DeleteProfileTests(unittest.TestCase):
    def test_leftover_default_folder_profile_can_be_deleted(self):
        # 默认夹不生成画像，但历史遗留的画像要能被清掉
        folders = [{"media_id": "1", "title": "默认收藏夹", "count": 3}]
        with patch.object(store, "load_folders", return_value=folders), \
             patch.object(store, "delete_folder_profile", return_value=True) as dele:
            result = server.folder_profile_delete("1")
        self.assertTrue(result["ok"])
        dele.assert_called_once_with("1")

    def test_delete_requires_existing_profile(self):
        folders = [{"media_id": "2", "title": "硬件", "count": 3}]
        with patch.object(store, "load_folders", return_value=folders), \
             patch.object(store, "delete_folder_profile", return_value=False):
            result = server.folder_profile_delete("2")
        self.assertEqual(404, result.status_code)

    def test_delete_unknown_folder_is_404(self):
        with patch.object(store, "load_folders", return_value=[]):
            result = server.folder_profile_delete("nope")
        self.assertEqual(404, result.status_code)


if __name__ == "__main__":
    unittest.main()
