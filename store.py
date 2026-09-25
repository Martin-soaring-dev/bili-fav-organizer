# -*- coding: utf-8 -*-
"""SQLite-backed local data store with legacy JSON import/export compatibility."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEGACY_DATA_DIR = HERE / "data"
_configured_data_dir = os.environ.get("BILI_FAV_ORGANIZER_DATA_DIR", "").strip()
if _configured_data_dir:
    DATA_DIR = Path(_configured_data_dir).expanduser()
elif os.environ.get("LOCALAPPDATA"):
    DATA_DIR = Path(os.environ["LOCALAPPDATA"]) / "BiliFavOrganizer" / "data"
else:
    DATA_DIR = Path.home() / ".local" / "share" / "BiliFavOrganizer" / "data"
DB_FILE = DATA_DIR / "library.sqlite3"
LEGACY_DB_FILE = LEGACY_DATA_DIR / "library.sqlite3"

# Legacy JSON paths remain as import sources and for callers that use the names.
FOLDERS_FILE = LEGACY_DATA_DIR / "folders.json"
VIDEOS_FILE = LEGACY_DATA_DIR / "videos.json"
ANALYSIS_FILE = LEGACY_DATA_DIR / "analysis.json"
PLAN_FILE = LEGACY_DATA_DIR / "plan.json"
APPLY_STATE_FILE = LEGACY_DATA_DIR / "apply_state.json"
SCAN_DONE_FILE = LEGACY_DATA_DIR / "scan_done.json"
SCAN_SELECTION_FILE = LEGACY_DATA_DIR / "scan_selection.json"
FOLDER_MERGE_PLAN_FILE = LEGACY_DATA_DIR / "folder_merge_plan.json"
FOLDER_MERGE_STATE_FILE = LEGACY_DATA_DIR / "folder_merge_state.json"
FOLDER_MERGE_DRAFT_FILE = LEGACY_DATA_DIR / "folder_merge_draft.json"

_lock = threading.RLock()
_SCHEMA_VERSION = 5
_DATASET_DEFAULTS = {
    "analysis": {}, "plan": {}, "apply_state": {}, "scan_done": [],
    "scan_selection": [], "folder_merge_plan": [], "folder_merge_draft": [],
    "folder_merge_state": {},
}
_LEGACY_FILES = {
    "folders": FOLDERS_FILE, "videos": VIDEOS_FILE,
    **{k: v for k, v in zip(
        ("analysis", "plan", "apply_state", "scan_done", "scan_selection",
         "folder_merge_plan", "folder_merge_draft", "folder_merge_state"),
        (ANALYSIS_FILE, PLAN_FILE, APPLY_STATE_FILE, SCAN_DONE_FILE,
         SCAN_SELECTION_FILE, FOLDER_MERGE_PLAN_FILE, FOLDER_MERGE_DRAFT_FILE,
         FOLDER_MERGE_STATE_FILE))},
}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _connect():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def _transaction():
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _backup_legacy_files() -> str:
    if not LEGACY_DATA_DIR.exists():
        return ""
    files = [p for p in LEGACY_DATA_DIR.iterdir()
             if p.is_file() and p.suffix.lower() in (".json", ".jsonl")]
    if not files:
        return ""
    dst = DATA_DIR / "backups" / f"pre-sqlite-{time.strftime('%Y%m%d_%H%M%S')}-{time.time_ns() % 1000000:06d}"
    dst.mkdir(parents=True, exist_ok=False)
    for path in files:
        shutil.copy2(path, dst / path.name)
    return str(dst)


def _migrate_database_location() -> None:
    """Copy the project-local SQLite database to the durable per-user data folder once."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if DB_FILE.exists() or not LEGACY_DB_FILE.exists():
        return
    temporary = DATA_DIR / f".library-{uuid.uuid4().hex}.migrating"
    source = sqlite3.connect(LEGACY_DB_FILE, timeout=30)
    target = sqlite3.connect(temporary, timeout=30)
    try:
        source.backup(target)
        result = target.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise sqlite3.DatabaseError(f"数据库迁移校验失败：{result[0] if result else '无结果'}")
        target.commit()
    except Exception:
        target.close()
        source.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        target.close()
        source.close()
        temporary.replace(DB_FILE)
        print(f"[store] 已将 SQLite 数据库复制到持久目录：{DB_FILE}（项目内原库保留）")


def _resource_key(record: dict, fallback: str = "") -> str:
    bvid = str(record.get("bvid") or fallback or "").strip()
    if bvid and bvid.startswith("BV"):
        return bvid
    rid = record.get("id") or record.get("aid") or ""
    rtype = record.get("type") or 2
    if rid:
        return f"{rtype}:{rid}"
    return bvid or str(fallback)


def _insert_video(conn, key: str, record: dict):
    rec = dict(record)
    real_key = _resource_key(rec, key)
    rid = str(rec.get("id") or rec.get("aid") or "")
    try:
        rtype = int(rec.get("type", 2) or 2)
    except (TypeError, ValueError):
        rtype = 2
    bvid = str(rec.get("bvid") or (key if str(key).startswith("BV") else ""))
    conn.execute(
        """INSERT INTO videos(resource_key, bvid, resource_id, resource_type, record_json, updated_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(resource_key) DO UPDATE SET bvid=excluded.bvid,
             resource_id=excluded.resource_id, resource_type=excluded.resource_type,
             record_json=excluded.record_json, updated_at=excluded.updated_at""",
        (real_key, bvid, rid, rtype, _json(rec), time.strftime("%Y-%m-%d %H:%M:%S")))
    return real_key, rec


def _legacy_bundle():
    bundle = {}
    for name, path in _LEGACY_FILES.items():
        bundle[name] = _read_json(path, [] if name in
                                  ("folders", "scan_done", "scan_selection", "folder_merge_plan",
                                   "folder_merge_draft") else {})
    return bundle


def _initialize():
    _migrate_database_location()
    existed = DB_FILE.exists()
    if not existed:
        backup = _backup_legacy_files()
        if backup:
            print(f"[store] 已备份旧数据：{backup}")
    conn = _connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS folders (
                media_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
                remote_count INTEGER NOT NULL DEFAULT 0, record_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', archived_at TEXT,
                directory_order INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS videos (
                resource_key TEXT PRIMARY KEY, bvid TEXT, resource_id TEXT NOT NULL DEFAULT '',
                resource_type INTEGER NOT NULL DEFAULT 2, record_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_videos_bvid ON videos(bvid);
            CREATE TABLE IF NOT EXISTS folder_items (
                media_id TEXT NOT NULL, resource_key TEXT NOT NULL,
                fav_time INTEGER, scan_run_id TEXT,
                PRIMARY KEY(media_id, resource_key)
            );
            CREATE INDEX IF NOT EXISTS idx_folder_items_resource ON folder_items(resource_key);
            CREATE TABLE IF NOT EXISTS analyses (
                resource_key TEXT PRIMARY KEY, record_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS plans (
                resource_key TEXT PRIMARY KEY, record_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_state (
                name TEXT PRIMARY KEY, value_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS folder_scan_state (
                media_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'never',
                strategy TEXT, expected_count INTEGER, fetched_count INTEGER NOT NULL DEFAULT 0,
                cursor_page INTEGER, snapshot_started_at TEXT, snapshot_completed_at TEXT,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS scan_stage (
                scan_run_id TEXT NOT NULL, media_id TEXT NOT NULL,
                resource_key TEXT NOT NULL, record_json TEXT NOT NULL,
                PRIMARY KEY(scan_run_id, media_id, resource_key)
            );
            CREATE INDEX IF NOT EXISTS idx_scan_stage_folder ON scan_stage(media_id, scan_run_id);
            CREATE TABLE IF NOT EXISTS folder_profiles (
                media_id TEXT PRIMARY KEY, profile_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS providers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
                api_key TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS models (
                id TEXT PRIMARY KEY,
                provider_id TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
                name TEXT NOT NULL, context_tokens INTEGER NOT NULL DEFAULT 32768,
                max_output_tokens INTEGER NOT NULL DEFAULT 8192,
                thinking_effort TEXT NOT NULL DEFAULT '',
                test_status TEXT NOT NULL DEFAULT 'untested',
                test_message TEXT, test_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_models_provider ON models(provider_id);
        """)
        model_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(models)")}
        if "context_source" not in model_columns:
            conn.execute("ALTER TABLE models ADD COLUMN context_source TEXT NOT NULL DEFAULT 'legacy_unknown'")
        if "output_source" not in model_columns:
            conn.execute("ALTER TABLE models ADD COLUMN output_source TEXT NOT NULL DEFAULT 'legacy_unknown'")
        folder_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(folders)")}
        if "status" not in folder_columns:
            conn.execute("ALTER TABLE folders ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        if "archived_at" not in folder_columns:
            conn.execute("ALTER TABLE folders ADD COLUMN archived_at TEXT")
        if "directory_order" not in folder_columns:
            conn.execute("ALTER TABLE folders ADD COLUMN directory_order INTEGER NOT NULL DEFAULT 0")
        row = conn.execute("SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1").fetchone()
        if row is None:
            has_data = any(conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                           for table in ("folders", "videos", "analyses", "plans", "app_state"))
            if not has_data:
                _import_bundle(conn, _legacy_bundle(), legacy=True)
            row = (0,)
        current_version = int(row[0])
        if current_version < 2:
            conn.execute("UPDATE folders SET status='active' WHERE status IS NULL OR status=''")
        if current_version < 3:
            conn.execute("UPDATE folders SET directory_order=(SELECT COUNT(*) FROM folders AS earlier "
                         "WHERE earlier.rowid < folders.rowid)")
        if current_version < 4:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_models_provider ON models(provider_id)")
        for version in range(current_version + 1, _SCHEMA_VERSION + 1):
            conn.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?,?)",
                         (version, time.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()


def _load_dataset_conn(conn, name: str, default=None):
    row = conn.execute("SELECT value_json FROM app_state WHERE name=?", (name,)).fetchone()
    if row is None:
        return _DATASET_DEFAULTS.get(name, default)
    return json.loads(row[0])


def _save_dataset_conn(conn, name: str, value):
    conn.execute("""INSERT INTO app_state(name,value_json) VALUES(?,?)
                  ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json""",
                 (name, _json(value)))


def _upsert_folder_conn(conn, folder: dict, *, status: str = "active", archived_at=None,
                        directory_order: int | None = None):
    mid = str(folder.get("media_id", ""))
    if not mid:
        return
    try:
        count = int(folder.get("count", folder.get("media_count", 0)) or 0)
    except (TypeError, ValueError):
        count = 0
    try:
        order = int(directory_order if directory_order is not None else folder.get("directory_order", 0))
    except (TypeError, ValueError):
        order = 0
    conn.execute("""INSERT INTO folders(media_id,title,remote_count,record_json,status,archived_at,directory_order)
                  VALUES(?,?,?,?,?,?,?)
                  ON CONFLICT(media_id) DO UPDATE SET title=excluded.title,
                  remote_count=excluded.remote_count,record_json=excluded.record_json,
                  status=excluded.status,archived_at=excluded.archived_at,
                  directory_order=excluded.directory_order""",
                 (mid, str(folder.get("title", "")), count, _json(folder), status, archived_at, order))


def _import_folders_conn(conn, folders: list):
    """Restore folders from an import bundle, preserving archive lifecycle metadata."""
    conn.execute("DELETE FROM folders")
    for order, folder in enumerate(folders or []):
        mid = str(folder.get("media_id", ""))
        if not mid:
            continue
        status = "archived" if folder.get("status") == "archived" else "active"
        _upsert_folder_conn(conn, folder, status=status,
                            archived_at=folder.get("archived_at") if status == "archived" else None,
                            directory_order=order)


def _sync_folders_conn(conn, folders: list):
    """Upsert the live Bilibili directory and archive IDs that disappeared."""
    active_before = {str(row[0]) for row in conn.execute(
        "SELECT media_id FROM folders WHERE status='active'")}
    live = {}
    for order, folder in enumerate(folders or []):
        mid = str(folder.get("media_id", ""))
        if mid:
            live[mid] = (folder, order)
    for mid, (folder, order) in live.items():
        _upsert_folder_conn(conn, folder, status="active", archived_at=None,
                            directory_order=order)
    disappeared = active_before - set(live)
    archived_at = time.strftime("%Y-%m-%d %H:%M:%S")
    for mid in disappeared:
        conn.execute("UPDATE folders SET status='archived',archived_at=? WHERE media_id=?",
                     (archived_at, mid))
        # Memberships are a live index of current favorites; retain the video metadata itself.
        conn.execute("DELETE FROM folder_items WHERE media_id=?", (mid,))
        conn.execute("DELETE FROM scan_stage WHERE media_id=?", (mid,))
        conn.execute("UPDATE folder_scan_state SET status='stale',last_error=? WHERE media_id=?",
                     ("收藏夹已归档；若再次出现需重新扫描", mid))
    if disappeared:
        selected = {str(x) for x in _load_dataset_conn(conn, "scan_selection", [])}
        selected.difference_update(disappeared)
        _save_dataset_conn(conn, "scan_selection", sorted(selected))


def _save_videos_conn(conn, videos: dict, *, replace=False, sync_memberships=False):
    if replace:
        conn.execute("DELETE FROM videos")
        conn.execute("DELETE FROM folder_items")
    for key, rec0 in (videos or {}).items():
        if not isinstance(rec0, dict):
            continue
        rec = dict(rec0)
        resource_key, rec = _insert_video(conn, str(key), rec)
        mids = {str(x) for x in (rec.get("folder_ids") or []) if str(x)}
        source = str(rec.get("source_folder_id") or "")
        if source:
            mids.add(source)
        if sync_memberships:
            conn.execute("DELETE FROM folder_items WHERE resource_key=?", (resource_key,))
        for mid in mids:
            conn.execute("""INSERT INTO folder_items(media_id,resource_key,fav_time)
                          VALUES(?,?,?) ON CONFLICT(media_id,resource_key) DO UPDATE
                          SET fav_time=excluded.fav_time""",
                         (mid, resource_key, rec.get("fav_time")))


def _import_bundle(conn, bundle: dict, legacy=False):
    folders = bundle.get("folders") or []
    videos = bundle.get("videos") or {}
    _import_folders_conn(conn, folders)
    _save_videos_conn(conn, videos, replace=True)
    for name in ("analysis", "plan"):
        items = bundle.get(name) or {}
        table = "analyses" if name == "analysis" else "plans"
        conn.execute(f"DELETE FROM {table}")
        for key, rec in items.items():
            if isinstance(rec, dict):
                conn.execute(f"INSERT INTO {table}(resource_key,record_json) VALUES(?,?)",
                             (str(key), _json(rec)))
    for name in _DATASET_DEFAULTS:
        if name not in ("analysis", "plan"):
            _save_dataset_conn(conn, name, bundle.get(name, _DATASET_DEFAULTS[name]))
    conn.execute("DELETE FROM folder_profiles")
    profiles = bundle.get("folder_profiles") or {}
    if isinstance(profiles, dict):
        profile_rows = profiles.items()
    else:
        profile_rows = ((x.get("media_id"), x) for x in profiles if isinstance(x, dict))
    for mid, profile in profile_rows:
        if not mid or not isinstance(profile, dict):
            continue
        profile = dict(profile)
        updated_at = str(profile.pop("updated_at", "") or time.strftime("%Y-%m-%d %H:%M:%S"))
        conn.execute("INSERT OR REPLACE INTO folder_profiles(media_id,profile_json,updated_at) VALUES(?,?,?)",
                     (str(mid), _json(profile), updated_at))
    done = {str(x) for x in (bundle.get("scan_done") or [])}
    for mid in done:
        conn.execute("""INSERT INTO folder_scan_state(media_id,status) VALUES(?,'complete')
                      ON CONFLICT(media_id) DO UPDATE SET status='complete'""", (mid,))
    if legacy or not bundle.get("folder_scan_state"):
        # Old scan_done only recorded a folder id. Its coverage and freshness cannot
        # be proven, so keep it as legacy-complete but visibly mark it stale.
        for mid in done:
            conn.execute("UPDATE folder_scan_state SET status='stale',last_error=? WHERE media_id=?",
                         ("从旧 scan_done.json 迁移；建议校验或重扫", mid))


def _read_folders_conn(conn, *, include_archived=True):
    where = "" if include_archived else "WHERE status='active'"
    result = []
    for row in conn.execute(f"SELECT record_json,status,archived_at FROM folders {where} "
                            "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,directory_order,rowid"):
        folder = json.loads(row[0])
        folder["status"] = str(row[1] or "active")
        if row[2]:
            folder["archived_at"] = str(row[2])
        else:
            folder.pop("archived_at", None)
        result.append(folder)
    return result


def _read_videos_raw_conn(conn, include_stage=True):
    rows = conn.execute("SELECT resource_key,record_json FROM videos").fetchall()
    result = {str(row[0]): json.loads(row[1]) for row in rows}
    memberships = {}
    for row in conn.execute("SELECT resource_key,media_id FROM folder_items"):
        memberships.setdefault(str(row[0]), set()).add(str(row[1]))
    for key, rec in result.items():
        actual = memberships.get(key, set())
        rec["folder_ids"] = sorted(actual)
        if rec.get("source_folder_id") not in actual:
            rec["source_folder_id"] = sorted(actual)[0] if actual else ""
    if include_stage:
        stages = conn.execute("SELECT resource_key,record_json FROM scan_stage").fetchall()
        for row in stages:
            key, rec = str(row[0]), json.loads(row[1])
            if key not in result:
                result[key] = rec
            else:
                old = result[key]
                memberships = set(str(x) for x in old.get("folder_ids", []))
                memberships.update(str(x) for x in rec.get("folder_ids", []))
                if old.get("source_folder_id"):
                    memberships.add(str(old["source_folder_id"]))
                if rec.get("source_folder_id"):
                    memberships.add(str(rec["source_folder_id"]))
                merged = {**old, **rec}
                merged["folder_ids"] = sorted(memberships)
                result[key] = merged
    return result


def _read_analysis_conn(conn):
    return {str(r[0]): json.loads(r[1]) for r in conn.execute(
        "SELECT resource_key,record_json FROM analyses")}


def _read_plan_conn(conn):
    return {str(r[0]): json.loads(r[1]) for r in conn.execute(
        "SELECT resource_key,record_json FROM plans")}


def save_folders(folders: list):
    with _transaction() as conn:
        _sync_folders_conn(conn, folders)


def load_folders(*, include_archived: bool = False) -> list:
    with _lock:
        conn = _connect()
        try: return _read_folders_conn(conn, include_archived=include_archived)
        finally: conn.close()


def save_videos(videos: dict):
    with _transaction() as conn:
        _save_videos_conn(conn, videos, sync_memberships=True)


def load_videos_raw() -> dict:
    with _lock:
        conn = _connect()
        try: return _read_videos_raw_conn(conn)
        finally: conn.close()


def load_videos() -> list:
    return list(load_videos_raw().values())


def load_video_index_for_resources(resources: list[dict]) -> dict[str, dict]:
    """Look up requested type/id pairs in the durable index without loading every video."""
    wanted: dict[int, set[str]] = {}
    for resource in resources or []:
        try:
            rtype = int(resource.get("type", 2) or 2)
        except (AttributeError, TypeError, ValueError):
            continue
        rid = str(resource.get("id", "") or "")
        if rid:
            wanted.setdefault(rtype, set()).add(rid)
    with _lock:
        conn = _connect()
        try:
            found: dict[str, dict] = {}
            keys: list[str] = []
            for rtype, ids in wanted.items():
                values = sorted(ids)
                for start in range(0, len(values), 800):
                    part = values[start:start + 800]
                    marks = ",".join("?" for _ in part)
                    rows = conn.execute(
                        f"SELECT resource_key,resource_id,record_json FROM videos "
                        f"WHERE resource_type=? AND resource_id IN ({marks})",
                        [rtype, *part]).fetchall()
                    for row in rows:
                        key = f"{rtype}:{row[1]}"
                        record = json.loads(row[2])
                        record["resource_key"] = str(row[0])
                        found[key] = record
                        keys.append(str(row[0]))
            memberships: dict[str, list[str]] = {}
            for start in range(0, len(keys), 800):
                part = keys[start:start + 800]
                marks = ",".join("?" for _ in part)
                for row in conn.execute(
                        f"SELECT resource_key,media_id FROM folder_items WHERE resource_key IN ({marks})",
                        part):
                    memberships.setdefault(str(row[0]), []).append(str(row[1]))
            for record in found.values():
                key = str(record.get("resource_key", ""))
                record["folder_ids"] = sorted(set(memberships.get(key, [])))
                if record.get("source_folder_id") not in record["folder_ids"]:
                    record["source_folder_id"] = record["folder_ids"][0] if record["folder_ids"] else ""
            return found
        finally:
            conn.close()


def load_folder_profile_samples(media_id: str, *, limit: int | None = None) -> tuple[int, list[dict]]:
    """Load local folder records in favorite-time order; default returns every indexed item."""
    with _lock:
        conn = _connect()
        try:
            keys = [str(row[0]) for row in conn.execute(
                "SELECT resource_key FROM folder_items WHERE media_id=? "
                "ORDER BY COALESCE(fav_time,0),resource_key", (str(media_id),))]
            total = len(keys)
            if not total:
                return 0, []
            if limit is None or total <= max(1, min(1000000, int(limit))):
                sample_keys = keys
            else:
                safe_limit = max(1, min(1000000, int(limit)))
                sample_keys = [keys[round(i * (total - 1) / (safe_limit - 1))]
                               for i in range(safe_limit)] if safe_limit > 1 else [keys[total // 2]]
            by_key = {}
            for start in range(0, len(sample_keys), 800):
                part = sample_keys[start:start + 800]
                marks = ",".join("?" for _ in part)
                rows = conn.execute(
                    f"SELECT resource_key,record_json FROM videos WHERE resource_key IN ({marks})",
                    part).fetchall()
                by_key.update({str(row[0]): json.loads(row[1]) for row in rows})
            return total, [by_key[key] for key in sample_keys if key in by_key]
        finally:
            conn.close()


def load_folder_profiles() -> dict[str, dict]:
    with _lock:
        conn = _connect()
        try:
            return {str(row[0]): {**json.loads(row[1]), "updated_at": str(row[2])}
                    for row in conn.execute("SELECT media_id,profile_json,updated_at FROM folder_profiles")}
        finally:
            conn.close()


def save_folder_profile(media_id: str, profile: dict) -> None:
    with _transaction() as conn:
        conn.execute("""INSERT INTO folder_profiles(media_id,profile_json,updated_at) VALUES(?,?,?)
                      ON CONFLICT(media_id) DO UPDATE SET profile_json=excluded.profile_json,
                      updated_at=excluded.updated_at""",
                     (str(media_id), _json(profile), time.strftime("%Y-%m-%d %H:%M:%S")))


def clear_videos():
    with _transaction() as conn:
        conn.execute("DELETE FROM videos")
        conn.execute("DELETE FROM folder_items")
        conn.execute("DELETE FROM scan_stage")


def replace_videos(videos: dict):
    with _transaction() as conn:
        _save_videos_conn(conn, videos, replace=True)


def save_scan_stage(scan_run_id: str, media_id: str, videos: dict):
    with _transaction() as conn:
        for key, rec0 in (videos or {}).items():
            if not isinstance(rec0, dict): continue
            rec = dict(rec0)
            rkey = _resource_key(rec, str(key))
            conn.execute("""INSERT INTO scan_stage(scan_run_id,media_id,resource_key,record_json)
                          VALUES(?,?,?,?) ON CONFLICT(scan_run_id,media_id,resource_key)
                          DO UPDATE SET record_json=excluded.record_json""",
                         (str(scan_run_id), str(media_id), rkey, _json(rec)))


def begin_folder_scan(scan_run_id: str, media_id: str, expected: int, strategy: str):
    with _transaction() as conn:
        conn.execute("DELETE FROM scan_stage WHERE media_id=?", (str(media_id),))
        conn.execute("""INSERT INTO folder_scan_state(media_id,status,strategy,expected_count,
                      fetched_count,cursor_page,snapshot_started_at,snapshot_completed_at,last_error)
                      VALUES(?,'partial',?,?,0,1,?,NULL,NULL)
                      ON CONFLICT(media_id) DO UPDATE SET status='partial',strategy=excluded.strategy,
                      expected_count=excluded.expected_count,fetched_count=0,cursor_page=1,
                      snapshot_started_at=excluded.snapshot_started_at,snapshot_completed_at=NULL,
                      last_error=NULL""",
                     (str(media_id), strategy, int(expected), time.strftime("%Y-%m-%d %H:%M:%S")))


def update_folder_scan_progress(media_id: str, fetched: int, cursor_page: int | None = None,
                                last_error: str | None = None):
    with _transaction() as conn:
        conn.execute("""UPDATE folder_scan_state SET fetched_count=?,cursor_page=?,last_error=?
                      WHERE media_id=?""", (int(fetched), cursor_page, last_error, str(media_id)))


def finish_folder_scan(scan_run_id: str, media_id: str, expected: int, fetched: int):
    with _transaction() as conn:
        staged = conn.execute("""SELECT resource_key,record_json FROM scan_stage
                               WHERE scan_run_id=? AND media_id=?""",
                              (str(scan_run_id), str(media_id))).fetchall()
        conn.execute("DELETE FROM folder_items WHERE media_id=?", (str(media_id),))
        for row in staged:
            rec = json.loads(row[1])
            memberships = {str(x) for x in rec.get("folder_ids", [])}
            memberships.add(str(media_id))
            rec["folder_ids"] = sorted(memberships)
            rec["source_folder_id"] = rec.get("source_folder_id") or str(media_id)
            _insert_video(conn, str(row[0]), rec)
            conn.execute("INSERT INTO folder_items(media_id,resource_key,fav_time,scan_run_id) VALUES(?,?,?,?)",
                         (str(media_id), str(row[0]), rec.get("fav_time"), str(scan_run_id)))
            conn.execute("UPDATE videos SET record_json=? WHERE resource_key=?", (_json(rec), str(row[0])))
        conn.execute("DELETE FROM scan_stage WHERE scan_run_id=? AND media_id=?",
                     (str(scan_run_id), str(media_id)))
        conn.execute("""UPDATE folder_scan_state SET status='complete',expected_count=?,
                      fetched_count=?,cursor_page=NULL,snapshot_completed_at=?,last_error=NULL
                      WHERE media_id=?""",
                     (int(expected), int(fetched), time.strftime("%Y-%m-%d %H:%M:%S"), str(media_id)))
        # Keep legacy scan_done visible for compatibility/export.
        done = {str(x) for x in _load_dataset_conn(conn, "scan_done", [])}
        done.add(str(media_id))
        _save_dataset_conn(conn, "scan_done", sorted(done))


def mark_folder_scan(media_id: str, status: str, error: str = ""):
    with _transaction() as conn:
        conn.execute("""INSERT INTO folder_scan_state(media_id,status,last_error) VALUES(?,?,?)
                      ON CONFLICT(media_id) DO UPDATE SET status=excluded.status,
                      last_error=excluded.last_error""", (str(media_id), status, error or None))


def load_folder_scan_states() -> dict:
    with _lock:
        conn = _connect()
        try:
            return {str(r[0]): dict(r) for r in conn.execute("SELECT * FROM folder_scan_state")}
        finally: conn.close()


def load_folder_items(media_id: str, *, query: str = "", offset: int = 0,
                      limit: int = 100) -> tuple[list[dict], int]:
    """Fetch a local folder page and total count without loading the whole library."""
    media_id = str(media_id)
    offset = max(0, int(offset))
    limit = max(1, min(100, int(limit)))
    query = (query or "").strip()
    params: list = [media_id]
    where = "fi.media_id=?"
    if query:
        term = "%" + query.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
        where += " AND (v.bvid LIKE ? ESCAPE '!' OR json_extract(v.record_json,'$.title') LIKE ? ESCAPE '!' OR json_extract(v.record_json,'$.upper_name') LIKE ? ESCAPE '!' OR json_extract(v.record_json,'$.desc') LIKE ? ESCAPE '!')"
        params.extend([term] * 4)
    with _lock:
        conn = _connect()
        try:
            total = conn.execute(
                f"SELECT COUNT(*) FROM folder_items fi JOIN videos v USING(resource_key) WHERE {where}",
                params).fetchone()[0]
            rows = conn.execute(
                f"""SELECT v.resource_key,v.record_json,fi.fav_time,a.record_json
                    FROM folder_items fi JOIN videos v USING(resource_key)
                    LEFT JOIN analyses a ON a.resource_key=v.bvid
                    WHERE {where}
                    ORDER BY COALESCE(fi.fav_time, json_extract(v.record_json,'$.fav_time'),0) DESC,
                             v.resource_key LIMIT ? OFFSET ?""",
                [*params, limit, offset]).fetchall()
            items = []
            for row in rows:
                record = json.loads(row[1])
                record["resource_key"] = str(row[0])
                if row[2] is not None:
                    record["fav_time"] = row[2]
                if row[3]:
                    record["analysis"] = json.loads(row[3])
                record["folder_ids"] = [media_id]
                record["source_folder_id"] = record.get("source_folder_id") or media_id
                items.append(record)
            return items, int(total)
        finally:
            conn.close()


def load_folder_item_counts() -> dict[str, int]:
    with _lock:
        conn = _connect()
        try:
            return {str(row[0]): int(row[1]) for row in conn.execute(
                "SELECT media_id,COUNT(*) FROM folder_items GROUP BY media_id")}
        finally:
            conn.close()


def save_analysis(items: dict):
    with _transaction() as conn:
        for key, rec in (items or {}).items():
            conn.execute("""INSERT INTO analyses(resource_key,record_json) VALUES(?,?)
                          ON CONFLICT(resource_key) DO UPDATE SET record_json=excluded.record_json""",
                         (str(key), _json(rec)))


def load_analysis_raw() -> dict:
    with _lock:
        conn = _connect()
        try: return _read_analysis_conn(conn)
        finally: conn.close()


def load_analysis() -> list:
    return list(load_analysis_raw().values())


def clear_analysis():
    with _transaction() as conn:
        conn.execute("DELETE FROM analyses")


def save_plan(items: dict):
    with _transaction() as conn:
        for key, rec in (items or {}).items():
            conn.execute("""INSERT INTO plans(resource_key,record_json) VALUES(?,?)
                          ON CONFLICT(resource_key) DO UPDATE SET record_json=excluded.record_json""",
                         (str(key), _json(rec)))


def load_plan_raw() -> dict:
    with _lock:
        conn = _connect()
        try: return _read_plan_conn(conn)
        finally: conn.close()


def load_plan() -> list:
    return list(load_plan_raw().values())


def clear_plan():
    with _transaction() as conn:
        conn.execute("DELETE FROM plans")


def replace_plan(items: dict):
    with _transaction() as conn:
        conn.execute("DELETE FROM plans")
        for key, rec in (items or {}).items():
            conn.execute("INSERT INTO plans(resource_key,record_json) VALUES(?,?)",
                         (str(key), _json(rec)))


def _get_dataset(name: str):
    with _lock:
        conn = _connect()
        try: return _load_dataset_conn(conn, name)
        finally: conn.close()


def _set_dataset(name: str, value):
    with _transaction() as conn: _save_dataset_conn(conn, name, value)


def save_apply_state(state: dict): _set_dataset("apply_state", state or {})
def load_apply_state() -> dict: return _get_dataset("apply_state")
def clear_apply_state(): _set_dataset("apply_state", {})
def save_scan_done(done_ids: list): _set_dataset("scan_done", [str(x) for x in done_ids])
def load_scan_done() -> list: return _get_dataset("scan_done")
def clear_scan_done(): _set_dataset("scan_done", [])
def save_scan_selection(folder_ids: list): _set_dataset("scan_selection", [str(x) for x in folder_ids])
def load_scan_selection() -> list: return _get_dataset("scan_selection")
def save_folder_merge_plan(groups: list): _set_dataset("folder_merge_plan", groups or [])
def load_folder_merge_plan() -> list: return _get_dataset("folder_merge_plan")
def save_folder_merge_draft(groups: list): _set_dataset("folder_merge_draft", groups or [])
def load_folder_merge_draft() -> list: return _get_dataset("folder_merge_draft")
def save_folder_merge_state(state: dict): _set_dataset("folder_merge_state", state or {})
def load_folder_merge_state() -> dict: return _get_dataset("folder_merge_state")


# ---------- 供应商 / 模型索引 ----------
def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def _provider_row(row) -> dict:
    return {"id": row["id"], "name": row["name"], "base_url": row["base_url"],
            "api_key": row["api_key"],
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def _model_row(row) -> dict:
    return {"id": row["id"], "provider_id": row["provider_id"], "name": row["name"],
            "context_tokens": int(row["context_tokens"] or 0),
            "max_output_tokens": int(row["max_output_tokens"] or 0),
            "context_source": row["context_source"] or "legacy_unknown",
            "output_source": row["output_source"] or "legacy_unknown",
            "thinking_effort": row["thinking_effort"] or "",
            "test_status": row["test_status"] or "untested",
            "test_message": row["test_message"], "test_at": row["test_at"],
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def list_providers() -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            return [_provider_row(r) for r in conn.execute(
                "SELECT * FROM providers ORDER BY created_at, name")]
        finally:
            conn.close()


def get_provider(provider_id: str) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM providers WHERE id=?", (str(provider_id),)).fetchone()
            return _provider_row(row) if row else None
        finally:
            conn.close()


def create_provider(name: str, base_url: str, api_key: str = "") -> dict:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    pid = _new_id()
    with _transaction() as conn:
        conn.execute(
            "INSERT INTO providers(id,name,base_url,api_key,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (pid, (name or "").strip() or "未命名供应商", (base_url or "").strip().rstrip("/"),
             api_key or "", now, now))
    return get_provider(pid)


def update_provider(provider_id: str, *, name=None, base_url=None, api_key=None) -> dict | None:
    existing = get_provider(provider_id)
    if not existing:
        return None
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _transaction() as conn:
        conn.execute(
            "UPDATE providers SET name=?, base_url=?, api_key=?, updated_at=? WHERE id=?",
            ((name if name is not None else existing["name"]).strip() or existing["name"],
             (base_url if base_url is not None else existing["base_url"]).strip().rstrip("/"),
             (api_key if api_key is not None else existing["api_key"]),
             now, str(provider_id)))
    return get_provider(provider_id)


def delete_provider(provider_id: str) -> bool:
    with _transaction() as conn:
        cur = conn.execute("DELETE FROM providers WHERE id=?", (str(provider_id),))
        return cur.rowcount > 0


def list_models(provider_id: str | None = None) -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            if provider_id:
                rows = conn.execute(
                    "SELECT * FROM models WHERE provider_id=? ORDER BY name",
                    (str(provider_id),)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM models ORDER BY provider_id, name").fetchall()
            return [_model_row(r) for r in rows]
        finally:
            conn.close()


def get_model(model_id: str) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM models WHERE id=?", (str(model_id),)).fetchone()
            return _model_row(row) if row else None
        finally:
            conn.close()


def create_model(provider_id: str, name: str, *, context_tokens: int | None = None,
                 max_output_tokens: int | None = None, thinking_effort: str = "",
                 context_source: str | None = None, output_source: str | None = None,
                 test_status: str = "untested", test_message: str | None = None) -> dict:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    mid = _new_id()
    with _transaction() as conn:
        conn.execute(
            """INSERT INTO models(id,provider_id,name,context_tokens,max_output_tokens,
               thinking_effort,test_status,test_message,test_at,created_at,updated_at,
               context_source,output_source)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, str(provider_id), (name or "").strip(),
             max(0, int(context_tokens or 0)), max(0, int(max_output_tokens or 0)),
             thinking_effort or "", test_status, test_message,
             now if test_status != "untested" else None, now, now,
             context_source or ("manual" if context_tokens else "unknown"),
             output_source or ("manual" if max_output_tokens else "unknown")))
    return get_model(mid)


def update_model(model_id: str, **fields) -> dict | None:
    existing = get_model(model_id)
    if not existing:
        return None
    allowed = {"name", "context_tokens", "max_output_tokens", "context_source", "output_source", "thinking_effort",
               "test_status", "test_message", "test_at", "provider_id"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return existing
    if "name" in updates:
        updates["name"] = (updates["name"] or "").strip() or existing["name"]
    if "context_tokens" in updates:
        updates["context_tokens"] = max(0, int(updates["context_tokens"]))
        updates["context_source"] = "manual" if updates["context_tokens"] else "unknown"
    if "max_output_tokens" in updates:
        updates["max_output_tokens"] = max(0, int(updates["max_output_tokens"]))
        updates["output_source"] = "manual" if updates["max_output_tokens"] else "unknown"
    if "context_source" in fields:
        updates["context_source"] = str(fields["context_source"] or "unknown")
    if "output_source" in fields:
        updates["output_source"] = str(fields["output_source"] or "unknown")
    updates["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    cols = ", ".join(f"{k}=?" for k in updates)
    with _transaction() as conn:
        conn.execute(f"UPDATE models SET {cols} WHERE id=?",
                     (*updates.values(), str(model_id)))
    return get_model(model_id)


def delete_model(model_id: str) -> bool:
    with _transaction() as conn:
        cur = conn.execute("DELETE FROM models WHERE id=?", (str(model_id),))
        return cur.rowcount > 0


def delete_provider_models(provider_id: str) -> list[str]:
    """Delete all local models for a provider, keeping the provider and its key."""
    with _transaction() as conn:
        model_ids = [str(row[0]) for row in conn.execute(
            "SELECT id FROM models WHERE provider_id=?", (str(provider_id),))]
        conn.execute("DELETE FROM models WHERE provider_id=?", (str(provider_id),))
    return model_ids


def find_model_by_name(provider_id: str, name: str) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM models WHERE provider_id=? AND name=?",
                (str(provider_id), str(name))).fetchone()
            return _model_row(row) if row else None
        finally:
            conn.close()


def replace_provider_models(provider_id: str, models: list[dict]) -> list[dict]:
    """Sync a provider's model list while preserving IDs and local metadata for matches."""
    with _transaction() as conn:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        existing = {str(row["name"]): row for row in conn.execute(
            "SELECT * FROM models WHERE provider_id=?", (str(provider_id),))}
        keep_names = set()
        for item in models:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            keep_names.add(name)
            old = existing.get(name)
            if old:
                # Remote model-list APIs often expose only an ID. Keep user-maintained
                # limits and test state unless the endpoint actually supplied metadata.
                updates = {"updated_at": now}
                if item.get("context_tokens") is not None:
                    updates["context_tokens"] = max(0, int(item["context_tokens"]))
                    updates["context_source"] = item.get("context_source") or "api"
                if item.get("max_output_tokens") is not None:
                    updates["max_output_tokens"] = max(0, int(item["max_output_tokens"]))
                    updates["output_source"] = item.get("output_source") or "api"
                if item.get("thinking_effort") is not None:
                    updates["thinking_effort"] = str(item["thinking_effort"] or "")
                cols = ", ".join(f"{key}=?" for key in updates)
                conn.execute(f"UPDATE models SET {cols} WHERE id=?",
                             (*updates.values(), old["id"]))
            else:
                conn.execute(
                    """INSERT INTO models(id,provider_id,name,context_tokens,max_output_tokens,
                       thinking_effort,test_status,test_message,test_at,created_at,updated_at,
                       context_source,output_source)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (_new_id(), str(provider_id), name,
                     max(0, int(item.get("context_tokens") or 0)),
                     max(0, int(item.get("max_output_tokens") or 0)),
                     str(item.get("thinking_effort") or ""),
                     "untested", None, None, now, now,
                     item.get("context_source") or "unknown",
                     item.get("output_source") or "unknown"))
        stale_names = set(existing) - keep_names
        if stale_names:
            placeholders = ",".join("?" for _ in stale_names)
            conn.execute(f"DELETE FROM models WHERE provider_id=? AND name IN ({placeholders})",
                         (str(provider_id), *sorted(stale_names)))
    return list_models(provider_id)


def export_all() -> dict:
    """Return the historical JSON bundle shape, excluding credentials."""
    with _lock:
        conn = _connect()
        try:
            return {
                "version": 3, "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "folders": _read_folders_conn(conn), "videos": _read_videos_raw_conn(conn, include_stage=False),
                "folder_profiles": {str(r[0]): {**json.loads(r[1]), "updated_at": str(r[2])}
                                    for r in conn.execute("SELECT media_id,profile_json,updated_at FROM folder_profiles")},
                "analysis": _read_analysis_conn(conn), "plan": _read_plan_conn(conn),
                **{name: _load_dataset_conn(conn, name) for name in _DATASET_DEFAULTS
                   if name not in ("analysis", "plan")},
                "folder_scan_state": {str(r[0]): dict(r) for r in conn.execute(
                    "SELECT * FROM folder_scan_state")},
                # 供应商密钥不导出；仅导出名称与地址
                "providers": [{k: r[k] for k in ("id", "name", "base_url", "created_at", "updated_at")}
                              for r in conn.execute("SELECT * FROM providers ORDER BY created_at")],
                "models": list_models(),
            }
        finally: conn.close()


def import_all(bundle: dict) -> dict:
    with _transaction() as conn:
        conn.execute("DELETE FROM videos")
        conn.execute("DELETE FROM folder_items")
        conn.execute("DELETE FROM folders")
        conn.execute("DELETE FROM analyses")
        conn.execute("DELETE FROM plans")
        conn.execute("DELETE FROM app_state")
        conn.execute("DELETE FROM folder_scan_state")
        conn.execute("DELETE FROM scan_stage")
        conn.execute("DELETE FROM folder_profiles")
        # providers/models 不随项目数据导入覆盖，避免误删本地密钥与模型索引
        _import_bundle(conn, bundle)
        for mid, state in (bundle.get("folder_scan_state") or {}).items():
            allowed = {k: state.get(k) for k in ("status", "strategy", "expected_count", "fetched_count",
                       "cursor_page", "snapshot_started_at", "snapshot_completed_at", "last_error")}
            conn.execute("""INSERT OR REPLACE INTO folder_scan_state(media_id,status,strategy,
                          expected_count,fetched_count,cursor_page,snapshot_started_at,
                          snapshot_completed_at,last_error) VALUES(?,?,?,?,?,?,?,?,?)""",
                         (str(mid), allowed.get("status", "never"), allowed.get("strategy"),
                          allowed.get("expected_count"), allowed.get("fetched_count", 0) or 0,
                          allowed.get("cursor_page"), allowed.get("snapshot_started_at"),
                          allowed.get("snapshot_completed_at"), allowed.get("last_error")))
    return stats()


def clear_scope(scope: str) -> list:
    cleared = []
    with _transaction() as conn:
        if scope in ("scan", "all"):
            conn.execute("DELETE FROM folders")
            conn.execute("DELETE FROM videos")
            conn.execute("DELETE FROM folder_items")
            conn.execute("DELETE FROM scan_stage")
            conn.execute("DELETE FROM folder_scan_state")
            conn.execute("DELETE FROM folder_profiles")
            for name, val in (("scan_done", []), ("scan_selection", [])):
                _save_dataset_conn(conn, name, val)
            cleared += ["folders", "videos", "scan_done", "scan_selection"]
        if scope == "folder_profile":
            conn.execute("DELETE FROM folder_profiles")
            cleared.append("folder_profiles")
        if scope in ("analysis", "all"):
            conn.execute("DELETE FROM analyses")
            cleared.append("analysis")
        if scope in ("plan", "all"):
            conn.execute("DELETE FROM plans")
            _save_dataset_conn(conn, "apply_state", {})
            cleared += ["plan", "apply_state"]
        if scope in ("folder_merge", "all"):
            for name in ("folder_merge_plan", "folder_merge_draft", "folder_merge_state"):
                _save_dataset_conn(conn, name, _DATASET_DEFAULTS[name])
            cleared += ["folder_merge_plan", "folder_merge_draft", "folder_merge_state"]
    return cleared


def stats() -> dict:
    with _lock:
        conn = _connect()
        try:
            return {"folders": conn.execute("SELECT COUNT(*) FROM folders WHERE status='active'").fetchone()[0],
                    "videos": conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0],
                    "analyzed": conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0],
                    "planned": conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]}
        finally: conn.close()


def backup_database(destination: Path):
    """Create a transactionally consistent SQLite backup at destination."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        source = _connect()
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()


_initialize()
