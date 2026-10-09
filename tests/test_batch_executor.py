import time
import unittest
from unittest.mock import patch

import server


class FakeBiliSession:
    def __init__(self):
        self.write_interval = 0
        self.calls = []

    def get_mid(self):
        return "123"

    def list_folders(self):
        return [
            {"title": "源夹", "media_id": "10", "count": 0},
            {"title": "现有夹", "media_id": "20", "count": 0},
        ]

    def batch_delete(self, media_id, aids, **kwargs):
        self.calls.append(("delete", str(media_id), list(aids)))
        return len(aids)

    def move_batch(self, src, target, aids, **kwargs):
        self.calls.append(("move", str(src), str(target), list(aids)))
        return len(aids)

    def create_folder(self, name):
        self.calls.append(("create", name))
        return "30"


class BatchExecutorTests(unittest.TestCase):
    def _run(self, plan, videos, fake):
        server.APP["session"] = fake
        server.APP["apply_run"] = None
        folders = fake.list_folders()
        profile_contexts = [
            {"id": str(f["media_id"]), "name": f["title"], "revision": "1"}
            for f in folders
        ]
        statuses = {bvid: "current" for bvid in plan}
        with patch.object(server.store, "load_plan_raw", return_value=plan), \
             patch.object(server.store, "load_videos", return_value=videos), \
             patch.object(server.store, "load_apply_state", return_value={}), \
             patch.object(server.store, "load_folders", return_value=folders), \
             patch.object(server.store, "save_plan"), \
             patch.object(server.store, "save_videos"), \
             patch.object(server.store, "save_folders"), \
             patch.object(server.store, "analysis_status_map", return_value=statuses), \
             patch.object(server, "refresh_analysis_statuses", return_value={}), \
             patch.object(server, "_organization_profile_readiness",
                          return_value={"ready": True, "message": "ok"}), \
             patch.object(server, "_current_folder_profile_contexts",
                          return_value=profile_contexts), \
             patch.object(server, "_refresh_folder_directory", return_value=None), \
             patch.object(server, "load_config",
                          return_value={"write_interval": 0, "apply_batch": 1000}):
            result = server.apply_start()
            deadline = time.time() + 2
            while (server.APP["apply_run"] and server.APP["apply_run"].get("running")
                   and time.time() < deadline):
                time.sleep(0.01)
        return result

    def test_groups_and_executes_in_batches(self):
        plan = {
            "d": {"action": "delete_invalid", "status": "pending"},
            "m1": {"action": "move_to_existing", "target_folder": "现有夹", "status": "pending"},
            "m2": {"action": "move_to_existing", "target_folder": "现有夹", "status": "failed"},
            "n": {"action": "create_new", "create_new_name": "新夹", "status": "pending"},
            "s": {"action": "skip", "status": "pending"},
            "bad": {"action": "move_to_existing", "target_folder": "现有夹", "status": "pending"},
            "u": {"action": "move_to_existing", "target_folder": "现有夹", "status": "unknown"},
        }
        videos = [
            {"bvid": "d", "aid": 1, "title": "已失效视频", "source_folder_id": "10",
             "folder_ids": ["10"]},
            {"bvid": "m1", "aid": 2, "title": "a", "source_folder_id": "10",
             "folder_ids": ["10"]},
            {"bvid": "m2", "aid": 3, "title": "b", "source_folder_id": "10",
             "folder_ids": ["10"]},
            {"bvid": "n", "aid": 4, "title": "c", "source_folder_id": "10",
             "folder_ids": ["10"]},
            {"bvid": "s", "aid": 5, "title": "d", "source_folder_id": "10",
             "folder_ids": ["10"]},
            {"bvid": "bad", "aid": 6, "title": "e", "source_folder_id": "10",
             "folder_ids": ["99"]},
            {"bvid": "u", "aid": 7, "title": "f", "source_folder_id": "10",
             "folder_ids": ["10"]},
        ]
        fake = FakeBiliSession()
        result = self._run(plan, videos, fake)
        self.assertTrue(result["started"])

        self.assertFalse(server.APP["apply_run"]["running"])
        self.assertEqual(fake.calls[0], ("delete", "10", [1]))
        self.assertEqual(fake.calls[1], ("move", "10", "20", [2, 3]))
        self.assertEqual(fake.calls[2], ("create", "新夹"))
        self.assertEqual(fake.calls[3], ("move", "10", "30", [4]))
        self.assertEqual(plan["d"]["status"], "done")
        self.assertEqual(plan["m1"]["status"], "done")
        self.assertEqual(plan["m2"]["status"], "done")
        self.assertEqual(plan["n"]["status"], "done")
        self.assertEqual(plan["s"]["status"], "done")
        self.assertEqual(plan["bad"]["status"], "failed")
        self.assertEqual(plan["u"]["status"], "unknown")

    def test_uncertain_batch_stops_and_requires_review(self):
        class UncertainSession(FakeBiliSession):
            def list_folders(self):
                return [
                    {"title": "源夹", "media_id": "10", "count": 0},
                    {"title": "甲", "media_id": "20", "count": 0},
                    {"title": "乙", "media_id": "21", "count": 0},
                ]

            def move_batch(self, src, target, aids, **kwargs):
                self.calls.append(("move", str(src), str(target), list(aids)))
                raise server.bili_api.WriteUncertainError("timeout")

        plan = {
            "a": {"action": "move_to_existing", "target_folder": "甲", "status": "pending"},
            "b": {"action": "move_to_existing", "target_folder": "乙", "status": "pending"},
        }
        videos = [
            {"bvid": "a", "aid": 1, "title": "a", "source_folder_id": "10",
             "folder_ids": ["10"]},
            {"bvid": "b", "aid": 2, "title": "b", "source_folder_id": "10",
             "folder_ids": ["10"]},
        ]
        fake = UncertainSession()
        self._run(plan, videos, fake)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(plan["a"]["status"], "unknown")
        self.assertEqual(plan["b"]["status"], "pending")
        self.assertIn("人工复核", server.APP["apply_run"]["error"])


if __name__ == "__main__":
    unittest.main()
