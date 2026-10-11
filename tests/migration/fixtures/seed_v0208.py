"""Seed a synthetic database through the real store.py from a historical Git ref."""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

# Store helpers timestamp synthetic rows with time.strftime(). Freeze only this
# fixture subprocess so logical hashes and test evidence stay stable across runs.
_FIXED_FIXTURE_TIME = time.strptime("2024-01-15 09:30:00", "%Y-%m-%d %H:%M:%S")
_ORIGINAL_STRFTIME = time.strftime
time.strftime = lambda fmt, *_args, **_kwargs: _ORIGINAL_STRFTIME(
    fmt, _FIXED_FIXTURE_TIME)

import store  # Loaded from the v0.208 source tree supplied in PYTHONPATH.


def seed(case: str) -> dict:
    if case not in {"legacy-empty", "legacy-normal", "legacy-edge"}:
        raise ValueError(f"unsupported fixture case: {case}")

    folders = []
    videos = {}
    if case != "legacy-empty":
        folder_count = 12 if case == "legacy-edge" else 10
        video_count = 500 if case == "legacy-normal" else 48
        folders = [
            {"media_id": f"SYN-F-{i:02d}", "title": f"Synthetic folder {i:02d}",
             "media_count": 0 if case == "legacy-edge" and i >= 10 else video_count // 5,
             "attr": 0, "legacy_note": "synthetic-only"}
            for i in range(folder_count)
        ]

        for i in range(video_count):
            bvid = f"BVTEST{i:08d}"
            folder_ids = [f"SYN-F-{i % 10:02d}", f"SYN-F-{(i + 1) % 10:02d}"]
            record = {
                "id": 900000 + i,
                "aid": 900000 + i,
                "bvid": bvid,
                "type": 2,
                "title": f"Synthetic video {i:04d}",
                "description": "SYNTHETIC TEST DATA; NOT A REAL BILIBILI RECORD",
                "pubdate": 1700000000 + i,
                "fav_time": 1701000000 + i,
                "folder_ids": folder_ids,
                "source_folder_id": folder_ids[0],
                "invalid_marker": bool(case == "legacy-edge" and i == 3),
                "legacy_unknown_field": "preserve-me" if i == 0 else None,
            }
            videos[bvid] = record

        if case == "legacy-edge":
            # Two old keys resolve to one resource key. The schema's primary key and
            # membership key should preserve one video with cross-folder references.
            duplicate = dict(videos["BVTEST00000000"])
            duplicate["legacy_alias"] = "duplicate-reference"
            videos["synthetic-duplicate-alias"] = duplicate

        store.save_folders(folders)
        store.save_videos(videos)

        profiles = min(folder_count, 10)
        for i in range(profiles):
            store.save_folder_profile(f"SYN-F-{i:02d}", {
                "summary": f"Synthetic profile {i:02d}",
                "topics": ["synthetic", f"topic-{i}"],
                "typical_content": [f"video-{i}"],
                "out_of_scope": [],
            })

        resource_keys = [f"BVTEST{i:08d}" for i in range(min(video_count, 6))]
        analysis = {
            key: {"target_folder_id": f"SYN-F-{i % 10:02d}", "reason": "synthetic"}
            for i, key in enumerate(resource_keys[:4])
        }
        analysis_meta = {
            resource_keys[0]: {"status": "current", "dependent_ids": ["SYN-F-00"]},
            resource_keys[1]: {"status": "stale", "dependent_ids": ["SYN-F-01"]},
        }
        store.save_analysis(analysis, meta=analysis_meta)
        store.save_plan({
            resource_keys[0]: {"resource_key": resource_keys[0], "status": "pending",
                               "target_folder_id": "SYN-F-00"},
            resource_keys[1]: {"resource_key": resource_keys[1], "status": "applied",
                               "target_folder_id": "SYN-F-01"},
            resource_keys[2]: {"resource_key": resource_keys[2], "status": "skipped"},
        })

        for i in range(min(10, folder_count)):
            mid = f"SYN-F-{i:02d}"
            if case == "legacy-edge" and i == 0:
                store.begin_folder_scan("synthetic-interrupted-run", mid, 12, "resume")
                store.save_scan_stage("synthetic-interrupted-run", mid, {
                    "BVTEST00000000": videos["BVTEST00000000"],
                })
                store.update_folder_scan_progress(mid, 4, cursor_page=2,
                                                  last_error="synthetic interrupted scan")
            else:
                store.mark_folder_scan(mid, "complete")

        store.save_scan_done([f"SYN-F-{i:02d}" for i in range(1, min(10, folder_count))])
        store.save_scan_selection(["SYN-F-00", "SYN-F-03"])
        store.save_analysis_scope_key("synthetic-scope-v0.208")
        store.save_apply_state({"run_id": "synthetic-apply", "status": "paused",
                                "completed": 1, "pending": 2})
        store.save_folder_merge_plan([{"source_ids": ["SYN-F-01", "SYN-F-02"],
                                       "target_id": "SYN-F-01"}])
        store.save_folder_merge_draft([{"source_ids": ["SYN-F-03", "SYN-F-04"],
                                        "target_id": "SYN-F-03"}])
        store.save_folder_merge_state({"status": "review", "synthetic": True})

        provider = store.create_provider(
            "Synthetic provider", "https://invalid.example/v1",
            "SYNTHETIC_INVALID_API_KEY_DO_NOT_USE")
        store.create_model(provider["id"], "synthetic-model",
                           context_tokens=4096, max_output_tokens=512,
                           context_source="manual", output_source="manual",
                           capabilities={"json_object": True})

    return {
        "case": case,
        "db_file": str(store.DB_FILE),
        "schema_version": store._SCHEMA_VERSION,
        "folder_count": len(store.load_folders(include_archived=True)),
        "video_count": len(store.load_videos_raw()),
        "provider_count": len(store.list_providers()),
        "model_count": len(store.list_models()),
    }


def capture_wal(target_dir: Path) -> None:
    """Copy a committed, uncheckpointed WAL snapshot while its writer is open."""
    target_dir.mkdir(parents=True, exist_ok=True)
    db_file = Path(store.DB_FILE)
    connection = sqlite3.connect(db_file)
    try:
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RuntimeError(f"WAL mode unavailable: {mode}")
        connection.execute(
            "INSERT OR REPLACE INTO app_state(name,value_json) VALUES(?,?)",
            ("synthetic_wal_marker", json.dumps({"value": "committed-in-wal"})),
        )
        connection.commit()
        wal_file = Path(f"{db_file}-wal")
        if not wal_file.is_file() or wal_file.stat().st_size <= 32:
            raise RuntimeError("expected a non-empty SQLite WAL sidecar")
        shutil.copy2(db_file, target_dir / "library.sqlite3")
        shutil.copy2(wal_file, target_dir / "library.sqlite3-wal")
        shm_file = Path(f"{db_file}-shm")
        if shm_file.is_file():
            shutil.copy2(shm_file, target_dir / "library.sqlite3-shm")
    finally:
        connection.close()


if __name__ == "__main__":
    case_name = sys.argv[1]
    manifest = seed(case_name)
    if len(sys.argv) > 2:
        capture_wal(Path(sys.argv[2]))
        print("WAL_READY", flush=True)
        sys.stdin.readline()
    print(json.dumps(manifest, sort_keys=True))
