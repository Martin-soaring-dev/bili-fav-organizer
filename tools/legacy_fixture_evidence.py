"""Print logical SQLite evidence for the v0.208 fixture and current importer."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from migration.support import (  # noqa: E402
    core_sha256,
    database_snapshot,
    fixture,
    probe,
    runtime,
)


def main() -> None:
    evidence = {}
    with tempfile.TemporaryDirectory(prefix=".bfo-migration-evidence-", dir=ROOT) as temp:
        root = Path(temp)
        tempfile.tempdir = str(root)
        for case in ("legacy-empty", "legacy-normal", "legacy-edge"):
            old_manifest = fixture(case)
            old_snapshot = database_snapshot(Path(old_manifest["db_file"]))
            current_runtime = runtime(case, root / case)
            current_snapshot = probe(current_runtime, "snapshot")["database"]
            evidence[case] = {
                "historical_source": old_manifest["source"],
                "schema_version": old_manifest["schema_version"],
                "legacy": {
                    "counts": old_snapshot["counts"],
                    "integrity_check": old_snapshot["integrity_check"],
                    "foreign_key_violations": old_snapshot["foreign_key_violations"],
                    "logical_sha256": old_snapshot["sha256"],
                    "core_sha256": core_sha256(old_snapshot),
                },
                "current": {
                    "counts": current_snapshot["counts"],
                    "integrity_check": current_snapshot["integrity_check"],
                    "foreign_key_violations": current_snapshot["foreign_key_violations"],
                    "logical_sha256": current_snapshot["sha256"],
                    "core_sha256": core_sha256(current_snapshot),
                },
                "logical_data_equal": old_snapshot["sha256"] == current_snapshot["sha256"],
                "core_data_equal": core_sha256(old_snapshot) == core_sha256(current_snapshot),
                "wal_capture_bytes": (
                    (Path(old_manifest["wal_capture_dir"]) / "library.sqlite3-wal").stat().st_size
                    if old_manifest["wal_capture_dir"] else 0
                ),
            }
    print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
