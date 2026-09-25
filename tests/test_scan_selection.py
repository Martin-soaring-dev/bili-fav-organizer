import unittest
import tempfile
from unittest.mock import patch
from pathlib import Path

import store


class ScanSelectionTests(unittest.TestCase):
    def test_selection_round_trip_normalizes_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(store, "DATA_DIR", Path(tmp)), \
                 patch.object(store, "DB_FILE", Path(tmp) / "test.sqlite3"), \
                 patch.object(store, "_LEGACY_FILES", {}):
                store._initialize()
                store.save_scan_selection([123, "456"])
                self.assertEqual(["123", "456"], store.load_scan_selection())


if __name__ == "__main__":
    unittest.main()
