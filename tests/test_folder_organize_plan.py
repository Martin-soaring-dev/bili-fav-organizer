import unittest
from unittest.mock import patch

import server
import llm_analyzer


class FolderOrganizePlanTests(unittest.TestCase):
    folders = [
        {"media_id": "1", "title": "目标", "count": 3},
        {"media_id": "2", "title": "来源A", "count": 4},
        {"media_id": "3", "title": "来源B", "count": 5},
    ]

    def test_rejects_target_as_source(self):
        body = server.FolderMergePlanIn(groups=[server.FolderMergeGroupIn(
            target_id="1", source_ids=["1"], final_name="合并")])
        with patch.object(server.store, "load_folders", return_value=self.folders), \
             patch.object(server.store, "load_folder_merge_plan", return_value=[]):
            result = server.folder_organize_plan(body)
        self.assertEqual(400, result.status_code)

    def test_preserves_completed_identical_group(self):
        old = [{"target_id": "1", "source_ids": ["2", "3"], "final_name": "合并",
                "status": "done", "results": [{"status": "merged_and_deleted"}]}]
        saved = {}
        body = server.FolderMergePlanIn(groups=[server.FolderMergeGroupIn(
            target_id="1", source_ids=["3", "2"], final_name="合并")])
        with patch.object(server.store, "load_folders", return_value=self.folders), \
             patch.object(server.store, "load_folder_merge_plan", return_value=old), \
             patch.object(server.store, "save_folder_merge_plan",
                          side_effect=lambda groups: saved.setdefault("groups", groups)):
            result = server.folder_organize_plan(body)
        self.assertTrue(result["ok"])
        self.assertEqual("done", saved["groups"][0]["status"])

    def test_folder_merge_llm_uses_structured_result(self):
        class Response:
            def raise_for_status(self): pass
            def json(self):
                return {"choices": [{"message": {"content":
                    '{"groups":[{"final_name":"技术","target_id":"1",'
                    '"source_ids":["2"],"reason":"主题重合","confidence":0.9}]}'}}]}
        cfg = llm_analyzer.LLMConfig(base_url="http://model", model="test")
        with patch.object(llm_analyzer.requests, "post", return_value=Response()):
            groups = llm_analyzer.suggest_folder_merges(cfg, [{"id": "1", "name": "A"}])
        self.assertEqual("1", groups[0]["target_id"])
        self.assertEqual(["2"], groups[0]["source_ids"])


if __name__ == "__main__":
    unittest.main()
