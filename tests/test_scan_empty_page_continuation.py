"""Regression: an empty intermediate Bilibili page must not truncate a scan."""
import unittest
from unittest.mock import Mock

from bili_api import BiliSession


class EmptyPageContinuationTests(unittest.TestCase):
    def test_has_more_true_empty_page_continues(self):
        session = BiliSession.__new__(BiliSession)
        session._wapi = 'https://api.bilibili.com'
        session._throttle_read = Mock()
        session._wbi_sign = lambda params: params
        responses = [
            {"medias": [{"id": 10, "bvid": "BV1a", "upper": {}}], "has_more": True},
            {"medias": [], "has_more": True},
            {"medias": [{"id": 11, "bvid": "BV1b", "upper": {}}], "has_more": False},
        ]
        requested_pages = []
        def get(_method, _url, params, **_kw):
            requested_pages.append(params["pn"])
            return responses.pop(0)
        session._request = get
        events = []
        got = list(session.iter_folder_videos("1000", 3, on_event=events.append))
        self.assertEqual([1, 2, 3], requested_pages)
        self.assertEqual(2, len(got))
        self.assertTrue(any(e.get("type") == "empty_page" for e in events))

    def test_empty_last_page_stops(self):
        session = BiliSession.__new__(BiliSession)
        session._wapi = 'https://api.bilibili.com'
        session._throttle_read = Mock()
        session._wbi_sign = lambda params: params
        session._request = lambda *args, **kwargs: {"medias": [], "has_more": False}
        self.assertEqual([], list(session.iter_folder_videos("1000", 0)))


if __name__ == "__main__":
    unittest.main()
