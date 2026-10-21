"""Synthetic, offline tests for read-only discrepancy diagnostics."""
import unittest
from core.scan_diagnostics import diagnose_folder


class FakeSession:
    _wapi = "https://api.bilibili.com"
    def __init__(self, page_ids, ids):
        self.page_ids = page_ids
        self.ids = ids
        self.throttles = 0
        self.requests = []
    def _throttle_read(self):
        self.throttles += 1
    def _request(self, method, url, params):
        self.requests.append((method, url, params))
        return self.ids
    def iter_folder_videos(self, media_id, total, on_event=None):
        on_event({"type": "req", "pn": 1})
        for id in self.page_ids:
            yield {"id": id, "type": 2}
        on_event({"type": "page", "pn": 1, "got": len(self.page_ids)})


class DiagnosticTests(unittest.TestCase):
    def test_remote_has_one_more_item_than_pagination(self):
        session = FakeSession(range(1, 316), [{"id": i, "type": 2} for i in range(1, 317)])
        report = diagnose_folder(session, "10", 316)
        self.assertTrue(report["read_only"])
        self.assertEqual(315, report["paged_count"])
        self.assertEqual(316, report["ids_count"])
        self.assertEqual(1, len(report["ids_not_in_pages"]))
        self.assertFalse(report["pages_not_in_ids"])
        self.assertEqual(1, len(session.requests))
        self.assertEqual("GET", session.requests[0][0])
        self.assertNotIn("cookie", str(report).lower())
        self.assertNotIn("316:", str(report))

    def test_complete_and_duplicate_detection(self):
        session = FakeSession([1, 2, 2], [{"id": 1, "type": 2}, {"id": 2, "type": 2}])
        report = diagnose_folder(session, "10", 3)
        self.assertEqual(3, report["paged_count"])
        self.assertEqual(2, report["paged_unique_count"])
        self.assertEqual([], report["ids_not_in_pages"])

    def test_rejects_large_folder_without_request(self):
        session = FakeSession([], [])
        with self.assertRaises(ValueError):
            diagnose_folder(session, "10", 1001)
        self.assertEqual([], session.requests)


if __name__ == "__main__":
    unittest.main()
