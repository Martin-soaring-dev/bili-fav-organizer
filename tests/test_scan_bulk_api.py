"""收藏夹 API 合约：完整 IDs 一次，缺失明细一次，不把缺明细当成少收藏。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from bili_api import BiliApiError, BiliSession, CookieInfo


class BulkFavoriteApiTests(unittest.TestCase):
    def session(self, ids, infos):
        session = BiliSession(CookieInfo("synthetic", "synthetic", "1"))
        session._throttle_read = Mock()
        responses = [SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                                     json=lambda payload=payload: {"code": 0, "data": payload})
                     for payload in (ids, infos)]
        session.session.request = Mock(side_effect=responses)
        return session

    def resources(self, count):
        ids = [{"id": n, "type": 2, "bv_id": f"BVtest{n}"} for n in range(1, count + 1)]
        infos = [{"id": n, "type": 2, "bv_id": f"BVtest{n}", "title": f"视频 {n}"}
                 for n in range(1, count + 1)]
        return ids, infos

    def test_1000_items_use_one_id_call_and_one_info_call(self):
        ids, infos = self.resources(1000)
        session = self.session(ids, list(reversed(infos)))
        records = session.get_folder_videos_bulk("10", 1000)
        calls = session.session.request.call_args_list
        self.assertEqual(2, len(calls))
        self.assertTrue(calls[0].args[1].endswith("/x/v3/fav/resource/ids"))
        self.assertTrue(calls[1].args[1].endswith("/x/v3/fav/resource/infos"))
        self.assertNotIn("pn", calls[0].kwargs["params"])
        self.assertEqual(1000, len(calls[1].kwargs["params"]["resources"].split(",")))
        self.assertEqual(1000, len(records))
        self.assertEqual(["BVtest1", "BVtest1000"], [records[0]["bvid"], records[-1]["bvid"]])
        self.assertEqual(2, session._throttle_read.call_count, "每个 HTTP 读请求只节流一次")

    def test_316_ids_and_315_infos_keep_the_missing_id_explicitly(self):
        ids, infos = self.resources(316)
        session = self.session(ids, infos[:-1])
        events = []
        records = session.get_folder_videos_bulk("10", 316, on_event=events.append)
        self.assertEqual(316, len(records))
        self.assertEqual(2, session.session.request.call_count)
        missing = records[-1]
        self.assertEqual(("316", 2, "BVtest316"), (missing["id"], missing["type"], missing["bvid"]))
        self.assertEqual("unavailable", missing["metadata_status"])
        self.assertEqual("", missing["title"], "不能捏造失效标题或视频明细")
        self.assertEqual(0, missing["attr"])
        self.assertEqual(1, next(e["count"] for e in events if e["type"] == "metadata_gap"))

    def test_empty_folder_needs_no_content_request(self):
        session = self.session([], [])
        self.assertEqual([], session.get_folder_videos_bulk("10", 0))
        session.session.request.assert_not_called()

    def test_types_and_ids_are_matched_instead_of_response_order(self):
        ids = [{"id": 8, "type": 2}, {"id": 8, "type": 12}, {"id": 9, "type": 21}]
        infos = [{"id": 9, "type": 21, "title": "合集"},
                 {"id": 8, "type": 12, "title": "音频"},
                 {"id": 8, "type": 2, "title": "视频", "bv_id": "BVvideo"}]
        session = self.session(ids, infos)
        records = session.get_folder_videos_bulk("10", 3)
        self.assertEqual(["2:8", "12:8", "21:9"], [v["resource_key"] for v in records])
        self.assertEqual(["视频", "音频", "合集"], [v["title"] for v in records])

    def test_duplicate_or_short_id_catalog_is_not_a_complete_snapshot(self):
        for ids in ([{"id": 1, "type": 2}],
                    [{"id": 1, "type": 2}, {"id": 1, "type": 2}]):
            with self.subTest(ids=ids):
                session = self.session(ids, [])
                with self.assertRaises(BiliApiError):
                    session.get_folder_videos_bulk("10", 2)
                self.assertEqual(1, session.session.request.call_count)

    def test_cancel_before_content_read_sends_no_request(self):
        session = self.session([], [])
        self.assertEqual([], session.get_folder_videos_bulk("10", 1, should_stop=lambda: True))
        session.session.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
