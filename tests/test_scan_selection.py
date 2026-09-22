import unittest
from unittest.mock import patch

import store


class ScanSelectionTests(unittest.TestCase):
    def test_selection_round_trip_normalizes_ids(self):
        written = {}
        def write(path, value):
            written["value"] = value
        with patch.object(store, "_write_json", side_effect=write):
            store.save_scan_selection([123, "456"])
        self.assertEqual(["123", "456"], written["value"])


if __name__ == "__main__":
    unittest.main()
