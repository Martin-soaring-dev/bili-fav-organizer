"""写路径安全契约：apply 执行器在启动前与执行中的硬约束。"""
import json
import time
import unittest
from unittest.mock import patch

import server


def _payload(result):
    if isinstance(result, dict):
        return result
    return json.loads(bytes(result.body).decode("utf-8"))


class FakeBiliSession:
    def __init__(self, folders=None):
        self.write_interval = 0
        self.calls = []
        self._folders = folders or [
            {"title": "源夹", "media_id": "10", "count": 0},
            {"title": "现有夹", "media_id": "20", "count": 0},
        ]

    def get_mid(self):
        return "123"

    def list_folders(self):
        return list(self._folders)

    def batch_delete(self, media_id, aids, **kwargs):
        self.calls.append(("delete", str(media_id), list(aids)))
        return len(aids)

    def move_batch(self, src, target, aids, **kwargs):
        self.calls.append(("move", str(src), str(target), list(aids)))
        return len(aids)

    def create_folder(self, name):
        self.calls.append(("create", name))
        return "30"


def _video(bvid, aid, title="a", source="10", folder_ids=None):
    return {
        "bvid": bvid, "aid": aid, "title": title,
        "source_folder_id": source,
        "folder_ids": list(folder_ids if folder_ids is not None else [source]),
    }


class ApplySafetyContractTests(unittest.TestCase):
    def setUp(self):
        server.APP["session"] = None
        server.APP["apply_run"] = None

    def _start(self, plan, videos, fake, *, ready=True, statuses=None,
               profile_contexts=None):
        folders = fake.list_folders()
        if profile_contexts is None:
            profile_contexts = [
                {"id": str(f["media_id"]), "name": f["title"], "revision": "1"}
                for f in folders
            ]
        if statuses is None:
            statuses = {b: "current" for b in plan}
        server.APP["session"] = fake
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
                          return_value={"ready": ready, "message": "画像未就绪"}), \
             patch.object(server, "_current_folder_profile_contexts",
                          return_value=profile_contexts), \
             patch.object(server, "_refresh_folder_directory", return_value=None), \
             patch.object(server, "load_config",
                          return_value={"write_interval": 0, "apply_batch": 1000}):
            result = server.apply_start()
            if isinstance(result, dict) and result.get("started"):
                deadline = time.time() + 2
                while (server.APP["apply_run"] and server.APP["apply_run"].get("running")
                       and time.time() < deadline):
                    time.sleep(0.01)
        return result

    def test_refuses_start_when_profiles_not_ready(self):
        plan = {"m1": {"action": "move_to_existing", "target_folder": "现有夹",
                       "status": "pending"}}
        videos = [_video("m1", 1)]
        fake = FakeBiliSession()
        result = self._start(plan, videos, fake, ready=False)
        body = _payload(result)
        self.assertFalse(body.get("started"))
        self.assertIsNone(server.APP["apply_run"])
        self.assertEqual(fake.calls, [])

    def test_refuses_start_when_analysis_is_stale(self):
        plan = {"m1": {"action": "move_to_existing", "target_folder": "现有夹",
                       "status": "pending"}}
        videos = [_video("m1", 1)]
        fake = FakeBiliSession()
        result = self._start(plan, videos, fake,
                             statuses={"m1": "stale"})
        body = _payload(result)
        self.assertFalse(body.get("started"))
        self.assertIsNone(server.APP["apply_run"])
        self.assertEqual(fake.calls, [])

    def test_refuses_default_folder_as_move_target(self):
        folders = [
            {"title": "源夹", "media_id": "10", "count": 0},
            {"title": server.DEFAULT_FAVORITE_NAME, "media_id": "20", "count": 0},
        ]
        plan = {"m1": {"action": "move_to_existing",
                       "target_folder": server.DEFAULT_FAVORITE_NAME,
                       "status": "pending"}}
        videos = [_video("m1", 1)]
        fake = FakeBiliSession(folders=folders)
        result = self._start(plan, videos, fake)
        body = _payload(result)
        self.assertFalse(body.get("started"))
        self.assertEqual(fake.calls, [])
        self.assertIn("默认收藏夹", body.get("error", ""))

    def test_refuses_default_folder_as_create_target(self):
        folders = [
            {"title": "源夹", "media_id": "10", "count": 0},
            {"title": server.DEFAULT_FAVORITE_NAME, "media_id": "20", "count": 0},
        ]
        plan = {"n1": {"action": "create_new",
                       "create_new_name": server.DEFAULT_FAVORITE_NAME,
                       "status": "pending"}}
        videos = [_video("n1", 1)]
        fake = FakeBiliSession(folders=folders)
        result = self._start(plan, videos, fake)
        body = _payload(result)
        self.assertFalse(body.get("started"))
        self.assertEqual(fake.calls, [])

    def test_unknown_items_are_not_auto_retried(self):
        plan = {
            "u": {"action": "move_to_existing", "target_folder": "现有夹",
                  "status": "unknown"},
            "m1": {"action": "move_to_existing", "target_folder": "现有夹",
                   "status": "pending"},
        }
        videos = [_video("u", 1), _video("m1", 2)]
        fake = FakeBiliSession()
        result = self._start(plan, videos, fake)
        self.assertTrue(_payload(result).get("started"))
        self.assertEqual(plan["u"]["status"], "unknown")
        self.assertEqual(plan["m1"]["status"], "done")
        # 只重发了 pending 的 m1，unknown 未再次请求
        self.assertEqual(fake.calls, [("move", "10", "20", [2])])

    def test_business_failure_marks_batch_failed_and_continues(self):
        class FailFirst(FakeBiliSession):
            def move_batch(self, src, target, aids, **kwargs):
                self.calls.append(("move", str(src), str(target), list(aids)))
                if not getattr(self, "_failed_once", False):
                    self._failed_once = True
                    raise server.bili_api.BiliApiError("业务失败")
                return len(aids)

        plan = {
            "a": {"action": "move_to_existing", "target_folder": "现有夹",
                  "status": "pending"},
            "b": {"action": "move_to_existing", "target_folder": "现有夹",
                  "status": "pending"},
        }
        videos = [_video("a", 1), _video("b", 2)]
        fake = FailFirst()
        result = self._start(plan, videos, fake)
        self.assertTrue(_payload(result).get("started"))
        # 同组同批：一次调用业务失败后整批 failed
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(plan["a"]["status"], "failed")
        self.assertEqual(plan["b"]["status"], "failed")

    def test_rate_limit_stops_run_and_keeps_batch_pending(self):
        class RateLimited(FakeBiliSession):
            def move_batch(self, src, target, aids, **kwargs):
                self.calls.append(("move", str(src), str(target), list(aids)))
                raise server.bili_api.RateLimitedError("风控")

        plan = {
            "a": {"action": "move_to_existing", "target_folder": "现有夹",
                  "status": "pending"},
            "b": {"action": "move_to_existing", "target_folder": "现有夹",
                  "status": "pending"},
        }
        videos = [_video("a", 1), _video("b", 2)]
        fake = RateLimited()
        result = self._start(plan, videos, fake)
        self.assertTrue(_payload(result).get("started"))
        self.assertEqual(len(fake.calls), 1)
        # 风控：整批保留待处理，不标记 done/failed，不再继续
        self.assertEqual(plan["a"]["status"], "pending")
        self.assertEqual(plan["b"]["status"], "pending")
        self.assertIn("风控", server.APP["apply_run"]["error"] or "")

    def test_unexpected_exception_stops_instead_of_continuing_writes(self):
        class Boom(FakeBiliSession):
            def move_batch(self, src, target, aids, **kwargs):
                self.calls.append(("move", str(src), str(target), list(aids)))
                raise RuntimeError("未预期故障")

        plan = {
            "a": {"action": "move_to_existing", "target_folder": "现有夹",
                  "status": "pending"},
            "b": {"action": "move_to_existing", "target_folder": "现有夹",
                  "status": "pending"},
        }
        videos = [_video("a", 1), _video("b", 2)]
        fake = Boom()
        self._start(plan, videos, fake)
        # 未预期异常：停止整次执行，不继续打第二枪
        self.assertEqual(len(fake.calls), 1)
        self.assertFalse(server.APP["apply_run"]["running"])
        self.assertIn("执行失败", server.APP["apply_run"]["error"] or "")

    def test_business_failure_continues_next_group(self):
        class FailFirstTarget(FakeBiliSession):
            def move_batch(self, src, target, aids, **kwargs):
                self.calls.append(("move", str(src), str(target), list(aids)))
                if str(target) == "20":
                    raise server.bili_api.BiliApiError("业务失败")
                return len(aids)

        folders = [
            {"title": "源夹", "media_id": "10", "count": 0},
            {"title": "现有夹", "media_id": "20", "count": 0},
            {"title": "备用夹", "media_id": "21", "count": 0},
        ]
        plan = {
            "a": {"action": "move_to_existing", "target_folder": "现有夹",
                  "status": "pending"},
            "b": {"action": "move_to_existing", "target_folder": "备用夹",
                  "status": "pending"},
        }
        videos = [_video("a", 1), _video("b", 2)]
        fake = FailFirstTarget(folders=folders)
        result = self._start(plan, videos, fake)
        self.assertTrue(_payload(result).get("started"))
        self.assertEqual(plan["a"]["status"], "failed")
        self.assertEqual(plan["b"]["status"], "done")
        self.assertEqual(len(fake.calls), 2)

    def test_unexpected_exception_does_not_write_next_group(self):
        class BoomFirstTarget(FakeBiliSession):
            def move_batch(self, src, target, aids, **kwargs):
                self.calls.append(("move", str(src), str(target), list(aids)))
                if str(target) == "20":
                    raise RuntimeError("未预期故障")
                return len(aids)

        folders = [
            {"title": "源夹", "media_id": "10", "count": 0},
            {"title": "现有夹", "media_id": "20", "count": 0},
            {"title": "备用夹", "media_id": "21", "count": 0},
        ]
        plan = {
            "a": {"action": "move_to_existing", "target_folder": "现有夹",
                  "status": "pending"},
            "b": {"action": "move_to_existing", "target_folder": "备用夹",
                  "status": "pending"},
        }
        videos = [_video("a", 1), _video("b", 2)]
        fake = BoomFirstTarget(folders=folders)
        self._start(plan, videos, fake)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(plan["a"]["status"], "pending")
        self.assertEqual(plan["b"]["status"], "pending")
        self.assertFalse(server.APP["apply_run"]["running"])

    def test_local_persist_failure_keeps_remote_success_as_done(self):
        plan = {"m1": {"action": "move_to_existing", "target_folder": "现有夹",
                       "status": "pending"}}
        videos = [_video("m1", 1)]
        fake = FakeBiliSession()
        folders = fake.list_folders()
        profile_contexts = [
            {"id": str(f["media_id"]), "name": f["title"], "revision": "1"}
            for f in folders
        ]
        server.APP["session"] = fake
        server.APP["apply_run"] = None

        save_calls = {"n": 0}

        def boom_save_plan(*_a, **_k):
            save_calls["n"] += 1
            # 首次为分组阶段预存；之后（远端已成功后的记账）才失败
            if save_calls["n"] > 1:
                raise OSError("disk full")

        with patch.object(server.store, "load_plan_raw", return_value=plan), \
             patch.object(server.store, "load_videos", return_value=videos), \
             patch.object(server.store, "load_apply_state", return_value={}), \
             patch.object(server.store, "load_folders", return_value=folders), \
             patch.object(server.store, "save_plan", side_effect=boom_save_plan), \
             patch.object(server.store, "save_videos"), \
             patch.object(server.store, "save_folders"), \
             patch.object(server.store, "analysis_status_map",
                          return_value={"m1": "current"}), \
             patch.object(server, "refresh_analysis_statuses", return_value={}), \
             patch.object(server, "_organization_profile_readiness",
                          return_value={"ready": True, "message": "ok"}), \
             patch.object(server, "_current_folder_profile_contexts",
                          return_value=profile_contexts), \
             patch.object(server, "_refresh_folder_directory", return_value=None), \
             patch.object(server, "load_config",
                          return_value={"write_interval": 0, "apply_batch": 1000}):
            server.apply_start()
            deadline = time.time() + 2
            while (server.APP["apply_run"] and server.APP["apply_run"].get("running")
                   and time.time() < deadline):
                time.sleep(0.01)
        self.assertEqual(fake.calls, [("move", "10", "20", [1])])
        self.assertEqual(plan["m1"]["status"], "done")
        err = server.APP["apply_run"]["error"] or ""
        self.assertIn("落库失败", err)

    def test_move_updates_membership_before_reporting_done(self):
        plan = {"m1": {"action": "move_to_existing", "target_folder": "现有夹",
                       "status": "pending"}}
        videos = [_video("m1", 1, folder_ids=["10", "20"])]
        fake = FakeBiliSession()
        self._start(plan, videos, fake)
        self.assertEqual(plan["m1"]["status"], "done")
        self.assertEqual(videos[0]["source_folder_id"], "20")
        self.assertEqual(set(videos[0]["folder_ids"]), {"20"})


if __name__ == "__main__":
    unittest.main()
