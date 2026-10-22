# -*- coding: utf-8 -*-
"""SQLite-backed local data store with legacy JSON import/export compatibility."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from core.database import (
    ActiveConnectionsError,
    DatabaseAlreadyInitializedError,
    DatabaseLifecycle,
    DatabaseRecoveryRequiredError,
    DatabaseState,
    IntegrityValidationError,
    open_readonly_session,
    sqlite_uri,
    validate_database,
)
from core.remote_operations import SqliteRemoteOperationRepository
from core.scheduler import SQLiteTaskStore

HERE = Path(__file__).resolve().parent
LEGACY_APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else HERE
LEGACY_DATA_DIR = LEGACY_APP_DIR / "data"
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

class _ProfileAwareRLock:
    """Acquire a profile shared lease before the process-local store mutex.

    This ordering is the mirror of maintenance code, which takes the profile
    exclusive lock before entering store operations. It prevents a thread from
    holding ``_lock`` while blocked behind an exclusive writer that is itself
    waiting to enter ``_lock``.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._local = threading.local()

    def __enter__(self):
        depth = int(getattr(self._local, "depth", 0))
        lease = None
        if depth == 0:
            lease = _lifecycle().acquire_profile_read_lease()
        try:
            self._lock.acquire()
        except BaseException:
            if lease is not None:
                lease.close()
            raise
        self._local.depth = depth + 1
        if depth == 0:
            self._local.profile_lease = lease
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        depth = int(getattr(self._local, "depth", 0))
        if depth <= 0:
            raise RuntimeError("store 锁的进入/退出次数不匹配")
        self._local.depth = depth - 1
        lease = self._local.profile_lease if depth == 1 else None
        self._lock.release()
        if depth == 1:
            del self._local.profile_lease
            if lease is not None:
                lease.close()


_lock = _ProfileAwareRLock()
_SCHEMA_VERSION = 8
# Oldest schema the app migrates forward. v0.200 shipped schema 5, so 5 is the
# floor; anything older has no verifiable historical source and stays refused.
_MIN_SUPPORTED_SCHEMA = 5
_DATASET_DEFAULTS = {
    "analysis": {}, "plan": {}, "apply_state": {}, "scan_done": [],
    "scan_selection": [], "folder_merge_plan": [], "folder_merge_draft": [],
    "folder_merge_state": {}, "analysis_scope_key": "",
    "scan_issues": {},
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

_lifecycle_lock = threading.RLock()
_lifecycle_instance: DatabaseLifecycle | None = None
_lifecycle_key: tuple | None = None

log = logging.getLogger(__name__)

# SQLite reports this when a freshly opened connection could not take a WAL read
# mark because a sibling connection closed (and dropped the -shm file) meanwhile.
_WRITE_READONLY_FRAGMENT = "readonly database"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _lifecycle_evidence_paths() -> tuple[Path, ...]:
    """Paths that prove this profile may have been initialized previously."""
    paths = [
        DATA_DIR / "library.sqlite3.enc",
        DATA_DIR / "lifecycle.json",
        DATA_DIR / "database-manifest.json",
        DATA_DIR / "library.sqlite3.plain-backup",
        DATA_DIR.parent / "vault.json",
        DATA_DIR / "backups",
        LEGACY_DB_FILE,
        *_LEGACY_FILES.values(),
    ]
    try:
        paths.extend(DATA_DIR.glob("*.migrating"))
        paths.extend(DATA_DIR.glob("*.candidate"))
        paths.extend(DATA_DIR.glob("*.initializing"))
    except OSError:
        pass
    return tuple(dict.fromkeys(Path(path) for path in paths))


# Candidate files that only exist while an initialization/migration/replacement is
# in flight. They are still passed to the gate as evidence, but they must not take
# part in the cache key: initialization itself creates and removes them, so a
# concurrent caller (the UI polls /api/status) would otherwise cache a second gate
# for an already-initialized profile - and that orphan gate stays in OPENING
# because nothing re-runs startup validation on it.
_TRANSIENT_DATABASE_SUFFIXES = (".initializing", ".migrating", ".candidate")


def _lifecycle() -> DatabaseLifecycle:
    """Get the lifecycle gate for the currently configured profile paths."""
    global _lifecycle_instance, _lifecycle_key
    evidence = _lifecycle_evidence_paths()
    key = (str(DB_FILE),
           tuple(str(path) for path in evidence
                 if not str(path).endswith(_TRANSIENT_DATABASE_SUFFIXES)))
    with _lifecycle_lock:
        if _lifecycle_instance is None or _lifecycle_key != key:
            _lifecycle_instance = DatabaseLifecycle(
                DB_FILE, evidence_paths=evidence,
                profile_lock_path=DATA_DIR / ".bfo-profile.lock")
            _lifecycle_key = key
            if DB_FILE.is_file():
                # Startup validation below is still required before business access.
                _lifecycle_instance.set_state(DatabaseState.OPENING)
            else:
                _lifecycle_instance.classify_missing_database()
        return _lifecycle_instance


def database_state() -> str:
    """Return the current database lifecycle state for diagnostics/UI adapters."""
    lifecycle = _lifecycle()
    if not DB_FILE.is_file() and lifecycle.state == DatabaseState.READY:
        lifecycle.classify_missing_database()
    return lifecycle.state.value


def database_connections() -> tuple[dict, ...]:
    """Return active connection owners for diagnostics and maintenance gates."""
    return _lifecycle().active_connections


def _connect(*, mode: str = "rw", owner: str = "store"):
    conn = _lifecycle().connect(mode=mode, owner=owner)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def _transaction():
    with _lock:
        lifecycle = _lifecycle()
        with lifecycle.connection_gate():
            conn = _connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                # A sibling connection closing as the last one deletes the WAL
                # shared-memory file; a connection that opened just before that
                # close has no read mark yet and SQLite reports it as read-only.
                # No statement has run at this point, so one freshly opened
                # attempt is safe. Anything else (or a second failure) is real.
                conn.close()
                if (_WRITE_READONLY_FRAGMENT not in str(exc).lower()
                        or database_state() != DatabaseState.READY.value):
                    raise
                log.warning(
                    "写事务建立时数据库被判定为只读（并发关闭删除了 WAL 共享内存），"
                    "重开连接重试一次：%s", exc)
                conn = _connect()
                conn.execute("BEGIN IMMEDIATE")
        # From here on the transaction holds a WAL read/write lock, so a sibling
        # close cannot delete the sidecars this connection depends on.
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()


def settle_wal_sidecars() -> bool:
    """Let SQLite drop orphaned WAL/SHM files once the profile is quiescent.

    Two connections closing at nearly the same time can leave an empty ``-wal``
    and a ``-shm`` behind: the first close cannot delete them while the other
    handle is still open, and SQLite suppresses that error. Re-opening and
    closing a writable handle gives SQLite another chance to delete them, and it
    still refuses while another process holds the database — this never unlinks
    a sidecar behind an active connection's back.
    """
    lifecycle = _lifecycle()
    if lifecycle.state is not DatabaseState.READY or not DB_FILE.is_file():
        return False
    if lifecycle.active_connection_count:
        return False
    connection = lifecycle.connect(mode="rw", owner="store:settle-wal")
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()
    finally:
        connection.close()
    return True


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


def _remove_sqlite_sidecars(path: str | Path) -> None:
    """Remove only SQLite sidecars belonging to a private candidate file."""
    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(path) + suffix).unlink(missing_ok=True)


def _migrate_database_location() -> None:
    """Copy the project-local SQLite database to the durable per-user data folder once."""
    lifecycle = _lifecycle()
    with lifecycle.maintenance(timeout=30.0):
        _migrate_database_location_under_maintenance()


def _migrate_database_location_under_maintenance() -> None:
    """Copy a legacy source while excluding every cooperating profile writer."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if DB_FILE.exists() or not LEGACY_DB_FILE.exists():
        return
    temporary = DATA_DIR / f".library-{uuid.uuid4().hex}.migrating"
    source = None
    target = None
    try:
        source = sqlite3.connect(LEGACY_DB_FILE, timeout=30)
        try:
            target = sqlite3.connect(temporary, timeout=30)
            try:
                # Refuse a corrupt/opaque source before SQLite's backup API can turn it
                # into a new empty destination database.
                integrity = [str(row[0]) for row in source.execute("PRAGMA integrity_check")]
                if integrity != ["ok"]:
                    raise IntegrityValidationError(
                        "项目内 SQLite 数据库完整性校验失败，拒绝迁移：" + "; ".join(integrity))
                source.backup(target)
                target.commit()
            finally:
                target.close()
        finally:
            source.close()
        # The copied file must be structurally sound and recognizable as a
        # supported BFO database before it can become the active file.
        validate_database_candidate(temporary)
        # A hard link is an atomic no-overwrite install. If another instance
        # won the race, retain that database and discard only our candidate.
        os.link(temporary, DB_FILE)
        print(f"[store] 已将 SQLite 数据库复制到持久目录：{DB_FILE}（项目内原库保留）")
    except FileExistsError:
        return
    finally:
        _remove_sqlite_sidecars(temporary)
        temporary.unlink(missing_ok=True)


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


def _initialize_schema(conn):
    """Create/upgrade the current schema inside a caller-owned candidate connection."""
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
            resource_key TEXT PRIMARY KEY, record_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'stale',
            dependent_ids TEXT NOT NULL DEFAULT '[]'
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
            capabilities_json TEXT NOT NULL DEFAULT '{}',
            thinking_effort TEXT NOT NULL DEFAULT '',
            test_status TEXT NOT NULL DEFAULT 'untested',
            test_message TEXT, test_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_models_provider ON models(provider_id);
    """)
    # Install the write-intent schema before this function opens its migration
    # transaction. The repository requires a validated connection with no active
    # transaction and never opens a database path itself.
    SqliteRemoteOperationRepository.create_schema(conn)
    # Scheduler DDL belongs to this versioned, lifecycle-owned migration.
    # The task store itself only verifies schema during ordinary operations.
    SQLiteTaskStore.create_schema(conn)
    model_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(models)")}
    if "context_source" not in model_columns:
        conn.execute("ALTER TABLE models ADD COLUMN context_source TEXT NOT NULL DEFAULT 'legacy_unknown'")
    if "output_source" not in model_columns:
        conn.execute("ALTER TABLE models ADD COLUMN output_source TEXT NOT NULL DEFAULT 'legacy_unknown'")
    if "capabilities_json" not in model_columns:
        conn.execute("ALTER TABLE models ADD COLUMN capabilities_json TEXT NOT NULL DEFAULT '{}'")
    folder_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(folders)")}
    if "status" not in folder_columns:
        conn.execute("ALTER TABLE folders ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
    if "archived_at" not in folder_columns:
        conn.execute("ALTER TABLE folders ADD COLUMN archived_at TEXT")
    if "directory_order" not in folder_columns:
        conn.execute("ALTER TABLE folders ADD COLUMN directory_order INTEGER NOT NULL DEFAULT 0")
    # v6：分析结果的可查询状态位。status='stale' 表示所依赖的画像已变化，需要重算。
    # dependent_ids 在分析落库时冻结，记录「这条结论当初参考了哪几个夹」，
    # 之后内容挪动不会追溯性地把它判为过期，避免执行完方案就触发全量重算。
    analysis_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(analyses)")}
    if "status" not in analysis_columns:
        conn.execute("ALTER TABLE analyses ADD COLUMN status TEXT NOT NULL DEFAULT 'stale'")
    if "dependent_ids" not in analysis_columns:
        conn.execute("ALTER TABLE analyses ADD COLUMN dependent_ids TEXT NOT NULL DEFAULT '[]'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_analyses_status ON analyses(status)")
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


def _has_locked_vault_evidence() -> bool:
    return any(path.exists() for path in (
        DATA_DIR / "library.sqlite3.enc",
        DATA_DIR.parent / "vault.json",
    ))


def _legacy_json_exists() -> bool:
    return any(path.is_file() for path in _LEGACY_FILES.values())


def _has_non_legacy_initialization_evidence() -> bool:
    """Identity/recovery evidence that forbids creating a replacement empty DB."""
    paths = (
        DATA_DIR / "library.sqlite3.enc",
        DATA_DIR / "lifecycle.json",
        DATA_DIR / "database-manifest.json",
        DATA_DIR / "library.sqlite3.plain-backup",
        DATA_DIR / "backups",
        DATA_DIR.parent / "vault.json",
        LEGACY_DB_FILE,
    )
    if any(path.exists() for path in paths):
        return True
    return any(any(DATA_DIR.glob(pattern)) for pattern in
               ("*.migrating", "*.candidate", "*.initializing"))


def _write_database_manifest() -> None:
    """Persist a small external initialized marker so later DB loss is detectable."""
    manifest = DATA_DIR / "database-manifest.json"
    temporary = manifest.with_name(f".{manifest.name}.{uuid.uuid4().hex}.tmp")
    payload = {
        "format_version": 1,
        "initialized": True,
        "database_file": DB_FILE.name,
        "schema_version": _SCHEMA_VERSION,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, manifest)
    finally:
        temporary.unlink(missing_ok=True)


_REQUIRED_DATABASE_TABLES = frozenset({
    "schema_migrations", "folders", "videos", "folder_items", "analyses",
    "plans", "app_state", "folder_scan_state", "scan_stage", "folder_profiles",
    "providers", "models",
})
_REQUIRED_DATABASE_COLUMN_CONTRACTS = {
    # Each entry is (declared SQLite type, PRAGMA table_info notnull flag).
    # Single-column PRIMARY KEY declarations report notnull=0 in SQLite, so
    # their uniqueness is checked separately by _REQUIRED_DATABASE_PRIMARY_KEYS.
    "schema_migrations": {
        "version": ("INTEGER", False), "applied_at": ("TEXT", True),
    },
    "folders": {
        "media_id": ("TEXT", False), "title": ("TEXT", True),
        "remote_count": ("INTEGER", True), "record_json": ("TEXT", True),
        "status": ("TEXT", True), "archived_at": ("TEXT", False),
        "directory_order": ("INTEGER", True),
    },
    "videos": {
        "resource_key": ("TEXT", False), "bvid": ("TEXT", False),
        "resource_id": ("TEXT", True), "resource_type": ("INTEGER", True),
        "record_json": ("TEXT", True), "updated_at": ("TEXT", True),
    },
    "folder_items": {
        "media_id": ("TEXT", True), "resource_key": ("TEXT", True),
        "fav_time": ("INTEGER", False), "scan_run_id": ("TEXT", False),
    },
    "analyses": {"resource_key": ("TEXT", False), "record_json": ("TEXT", True)},
    "plans": {"resource_key": ("TEXT", False), "record_json": ("TEXT", True)},
    "app_state": {"name": ("TEXT", False), "value_json": ("TEXT", True)},
    "folder_scan_state": {
        "media_id": ("TEXT", False), "status": ("TEXT", True),
        "strategy": ("TEXT", False), "expected_count": ("INTEGER", False),
        "fetched_count": ("INTEGER", True), "cursor_page": ("INTEGER", False),
        "snapshot_started_at": ("TEXT", False), "snapshot_completed_at": ("TEXT", False),
        "last_error": ("TEXT", False),
    },
    "scan_stage": {
        "scan_run_id": ("TEXT", True), "media_id": ("TEXT", True),
        "resource_key": ("TEXT", True), "record_json": ("TEXT", True),
    },
    "folder_profiles": {
        "media_id": ("TEXT", False), "profile_json": ("TEXT", True),
        "updated_at": ("TEXT", True),
    },
    "providers": {
        "id": ("TEXT", False), "name": ("TEXT", True), "base_url": ("TEXT", True),
        "api_key": ("TEXT", True), "created_at": ("TEXT", True),
        "updated_at": ("TEXT", True),
    },
    "models": {
        "id": ("TEXT", False), "provider_id": ("TEXT", True),
        "name": ("TEXT", True), "context_tokens": ("INTEGER", True),
        "max_output_tokens": ("INTEGER", True), "thinking_effort": ("TEXT", True),
        "test_status": ("TEXT", True), "test_message": ("TEXT", False),
        "test_at": ("TEXT", False), "created_at": ("TEXT", True),
        "updated_at": ("TEXT", True),
    },
}
_VERSIONED_EXTENSION_TABLES = {
    8: frozenset({
        "remote_operation_schema_version", "remote_operations", "remote_operation_events",
        "bfo_scheduler_tasks", "bfo_scheduler_meta",
    }),
}
_VERSIONED_EXTENSION_COLUMN_CONTRACTS = {
    6: {
        "analyses": {"status": ("TEXT", True), "dependent_ids": ("TEXT", True)},
    },
    7: {
        "models": {"capabilities_json": ("TEXT", True)},
    },
    8: {
        "models": {
            "context_source": ("TEXT", True), "output_source": ("TEXT", True),
        },
        "remote_operation_schema_version": {"version": ("INTEGER", True)},
        "remote_operations": {
            "operation_id": ("TEXT", False), "intent_json": ("TEXT", True),
            "task_id": ("TEXT", True), "account_id": ("TEXT", True),
            "operation_type": ("TEXT", True), "resource_keys_json": ("TEXT", True),
            "status": ("TEXT", True), "attempt": ("INTEGER", True),
            "version": ("INTEGER", True), "remote_response_json": ("TEXT", False),
            "local_result_json": ("TEXT", False), "reconciliation_json": ("TEXT", False),
            "error_code": ("TEXT", False), "created_at": ("TEXT", True),
            "updated_at": ("TEXT", True), "resolved_at": ("TEXT", False),
        },
        "remote_operation_events": {
            "event_id": ("INTEGER", False), "operation_id": ("TEXT", True),
            "from_status": ("TEXT", False), "to_status": ("TEXT", True),
            "event_type": ("TEXT", True), "details_json": ("TEXT", True),
            "created_at": ("TEXT", True),
        },
        "bfo_scheduler_tasks": {
            "task_id": ("TEXT", False), "task_type": ("TEXT", True),
            "task_schema_version": ("INTEGER", True),
            "checkpoint_schema_version": ("INTEGER", True), "status": ("TEXT", True),
            "parameters_json": ("TEXT", True), "checkpoint_json": ("TEXT", False),
            "progress_json": ("TEXT", True), "error_json": ("TEXT", False),
            "resume_policy": ("TEXT", True), "pause_reason": ("TEXT", False),
            "resume_intent": ("TEXT", True), "attempt": ("INTEGER", True),
            "owner_epoch": ("INTEGER", True), "resource_keys_json": ("TEXT", True),
            "idempotency_key": ("TEXT", False),
            "reconciliation_required": ("INTEGER", True),
            "reconciliation_reason": ("TEXT", True), "created_at": ("TEXT", True),
            "updated_at": ("TEXT", True), "revision": ("INTEGER", True),
        },
        "bfo_scheduler_meta": {
            "key": ("TEXT", False), "value": ("TEXT", True),
        },
    }
}

_VERSIONED_EXTENSION_CHECKS = {
    8: {
        "remote_operations": {
            "status": "CHECK(STATUSIN('PREPARED','DISPATCHING','CONFIRMED','REJECTED',"
                      "'NEEDS_RECONCILIATION','NOT_APPLIED','MANUAL_REVIEW'))",
            "attempt": "CHECK(ATTEMPT>=0)",
            "version": "CHECK(VERSION>=0)",
        },
        "bfo_scheduler_tasks": {
            "task_schema_version": "CHECK(TASK_SCHEMA_VERSION>0)",
            "checkpoint_schema_version": "CHECK(CHECKPOINT_SCHEMA_VERSION>0)",
            "attempt": "CHECK(ATTEMPT>=0)",
            "owner_epoch": "CHECK(OWNER_EPOCH>=0)",
            "revision": "CHECK(REVISION>0)",
        },
    }
}
_REQUIRED_DATABASE_PRIMARY_KEYS = {
    "schema_migrations": ("version",),
    "folders": ("media_id",),
    "videos": ("resource_key",),
    "folder_items": ("media_id", "resource_key"),
    "analyses": ("resource_key",),
    "plans": ("resource_key",),
    "app_state": ("name",),
    "folder_scan_state": ("media_id",),
    "scan_stage": ("scan_run_id", "media_id", "resource_key"),
    "folder_profiles": ("media_id",),
    "providers": ("id",),
    "models": ("id",),
}
_REQUIRED_DATABASE_FOREIGN_KEYS = {
    "models": frozenset({("providers", "provider_id", "id", "CASCADE")}),
}
_VERSIONED_EXTENSION_PRIMARY_KEYS = {
    8: {
        "remote_operations": ("operation_id",),
        "remote_operation_events": ("event_id",),
        "bfo_scheduler_tasks": ("task_id",),
        "bfo_scheduler_meta": ("key",),
    }
}
_VERSIONED_EXTENSION_FOREIGN_KEYS = {
    8: {
        "remote_operation_events": frozenset({
            ("remote_operations", "operation_id", "operation_id", "NO ACTION"),
        }),
    }
}
_VERSIONED_EXTENSION_UNIQUE_KEYS = {
    8: {
        "bfo_scheduler_tasks": frozenset({("idempotency_key",)}),
    }
}


def validate_database_candidate(path: str | Path) -> dict:
    """Validate SQLite integrity plus the supported BFO database schema contract."""
    try:
        validation = validate_database(path)
    except (TypeError, ValueError) as exc:
        raise IntegrityValidationError(f"候选数据库版本标记无效：{exc}") from exc
    if validation.schema_version is None:
        raise IntegrityValidationError(
            "候选文件缺少有效 schema_migrations 身份，拒绝作为 BFO 数据库")
    if validation.schema_version > _SCHEMA_VERSION:
        raise IntegrityValidationError(
            f"候选数据库 Schema {validation.schema_version} 高于当前支持版本 {_SCHEMA_VERSION}")
    if validation.schema_version < _MIN_SUPPORTED_SCHEMA:
        raise IntegrityValidationError(
            f"候选数据库 Schema {validation.schema_version} 低于已确认支持边界"
            f" {_MIN_SUPPORTED_SCHEMA}；拒绝猜测旧版结构")
    connection = open_readonly_session(path)
    try:
        tables = {str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        schema_version = int(validation.schema_version)
        required_tables = set(_REQUIRED_DATABASE_TABLES)
        required_column_contracts = {
            name: dict(columns)
            for name, columns in _REQUIRED_DATABASE_COLUMN_CONTRACTS.items()
        }
        required_columns = {
            name: set(columns) for name, columns in required_column_contracts.items()
        }
        required_primary_keys = dict(_REQUIRED_DATABASE_PRIMARY_KEYS)
        required_foreign_keys = {
            name: set(keys) for name, keys in _REQUIRED_DATABASE_FOREIGN_KEYS.items()
        }
        required_unique_keys = {}
        for version, extension_tables in _VERSIONED_EXTENSION_TABLES.items():
            if schema_version >= version:
                required_tables.update(extension_tables)
                for table, columns in _VERSIONED_EXTENSION_COLUMN_CONTRACTS.get(
                        version, {}).items():
                    required_column_contracts.setdefault(table, {}).update(columns)
                    required_columns.setdefault(table, set()).update(columns)
                required_primary_keys.update(
                    _VERSIONED_EXTENSION_PRIMARY_KEYS.get(version, {}))
                for table, keys in _VERSIONED_EXTENSION_FOREIGN_KEYS.get(version, {}).items():
                    required_foreign_keys.setdefault(table, set()).update(keys)
                for table, keys in _VERSIONED_EXTENSION_UNIQUE_KEYS.get(version, {}).items():
                    required_unique_keys.setdefault(table, set()).update(keys)
        missing_tables = sorted(required_tables - tables)
        if missing_tables:
            raise IntegrityValidationError(
                "候选数据库缺少必要业务表，拒绝自动补建：" + ", ".join(missing_tables))

        missing_columns = []
        primary_key_mismatches = []
        foreign_key_mismatches = []
        unique_key_mismatches = []
        column_shape_mismatches = []
        for table in sorted(required_tables):
            info = list(connection.execute(f'PRAGMA table_info("{table}")'))
            columns = {str(row[1]) for row in info}
            for column in sorted(required_columns.get(table, ()) - columns):
                missing_columns.append(f"{table}.{column}")
            for row in info:
                column_name = str(row[1])
                expected_shape = required_column_contracts.get(table, {}).get(column_name)
                if expected_shape is None:
                    continue
                expected_type, expected_not_null = expected_shape
                actual_type = str(row[2]).strip().upper()
                if actual_type != expected_type:
                    column_shape_mismatches.append(
                        f"{table}.{column_name} 类型 {actual_type or '未声明'} != {expected_type}")
                if bool(row[3]) != expected_not_null:
                    actual_requirement = "NOT NULL" if bool(row[3]) else "可空"
                    expected_requirement = "NOT NULL" if expected_not_null else "可空"
                    column_shape_mismatches.append(
                        f"{table}.{column_name} {actual_requirement} != {expected_requirement}")
            primary_key = tuple(
                str(row[1]) for row in sorted(
                    (row for row in info if int(row[5]) > 0), key=lambda row: int(row[5]))
            )
            expected_primary_key = required_primary_keys.get(table)
            if expected_primary_key and primary_key != expected_primary_key:
                primary_key_mismatches.append(
                    f"{table} ({', '.join(primary_key) or '无'} != "
                    f"{', '.join(expected_primary_key)})")
            declared_foreign_keys = {
                (str(row[2]), str(row[3]), str(row[4]), str(row[6]).upper())
                for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')
            }
            for foreign_key in sorted(required_foreign_keys.get(table, ())):
                if foreign_key not in declared_foreign_keys:
                    foreign_key_mismatches.append(
                        f"{table} {foreign_key[1]} -> {foreign_key[0]}.{foreign_key[2]}")

            unique_indexes = set()
            for index_row in connection.execute(f'PRAGMA index_list("{table}")'):
                if int(index_row[2]) != 1:
                    continue
                index_name = str(index_row[1]).replace('"', '""')
                index_columns = tuple(str(index[2]) for index in connection.execute(
                    f'PRAGMA index_info("{index_name}")'))
                unique_indexes.add(index_columns)
            for unique_key in sorted(required_unique_keys.get(table, ())):
                if unique_key not in unique_indexes:
                    unique_key_mismatches.append(f"{table} ({', '.join(unique_key)})")
        if missing_columns:
            raise IntegrityValidationError(
                "候选数据库缺少必要列，拒绝自动补建：" + ", ".join(missing_columns))
        if column_shape_mismatches:
            raise IntegrityValidationError(
                "候选数据库列类型或 NOT NULL 约束不匹配："
                + "; ".join(column_shape_mismatches))
        if primary_key_mismatches:
            raise IntegrityValidationError(
                "候选数据库主键结构不匹配，拒绝自动补建：" + "; ".join(primary_key_mismatches))
        if foreign_key_mismatches:
            raise IntegrityValidationError(
                "候选数据库外键结构不匹配，拒绝自动补建：" + "; ".join(foreign_key_mismatches))
        if unique_key_mismatches:
            raise IntegrityValidationError(
                "候选数据库唯一约束缺失，拒绝自动补建：" + "; ".join(unique_key_mismatches))

        check_mismatches = []
        for version, checks_by_table in _VERSIONED_EXTENSION_CHECKS.items():
            if schema_version < version:
                continue
            for table, checks_by_column in checks_by_table.items():
                row = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                table_sql = "" if row is None or row[0] is None else str(row[0])
                normalized_sql = "".join(table_sql.split())
                normalized_sql = (normalized_sql.replace('"', "").replace("`", "")
                                  .replace("[", "").replace("]", "").upper())
                for column, expected_check in checks_by_column.items():
                    if expected_check not in normalized_sql:
                        check_mismatches.append(f"{table}.{column}")
        if check_mismatches:
            raise IntegrityValidationError(
                "候选数据库缺少必要 CHECK 约束：" + ", ".join(check_mismatches))

        ledger_info = {str(row[1]): row for row in connection.execute(
            'PRAGMA table_info("schema_migrations")')}
        if not {"version", "applied_at"}.issubset(ledger_info):
            raise IntegrityValidationError(
                "schema_migrations 缺少必要列，拒绝推断迁移版本")
        if str(ledger_info["version"][2]).strip().upper() != "INTEGER":
            raise IntegrityValidationError(
                "schema_migrations.version 类型不是 INTEGER，拒绝推断迁移版本")
        ledger_rows = list(connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"))
        if any(type(row[0]) is not int for row in ledger_rows):
            raise IntegrityValidationError(
                "schema_migrations 存在非整数版本值，拒绝推断迁移链")
        ledger_versions = [row[0] for row in ledger_rows]
        expected_versions = list(range(1, schema_version + 1))
        if ledger_versions != expected_versions:
            raise IntegrityValidationError(
                f"schema_migrations 迁移链不连续：实际 {ledger_versions}，"
                f"期望 1..{schema_version}")
        if any(not isinstance(row[1], str) or not row[1].strip() for row in ledger_rows):
            raise IntegrityValidationError(
                "schema_migrations 存在空的 applied_at，拒绝推断迁移记录有效")
        if schema_version >= 8:
            remote_schema = connection.execute(
                "SELECT version FROM remote_operation_schema_version").fetchall()
            remote_schema_type = str(next(
                row[2] for row in connection.execute(
                    'PRAGMA table_info("remote_operation_schema_version")')
                if str(row[1]) == "version")).strip().upper()
            if (remote_schema_type != "INTEGER" or len(remote_schema) != 1 or
                    type(remote_schema[0][0]) is not int or remote_schema[0][0] != 1):
                raise IntegrityValidationError(
                    "远端操作日志 schema 版本标记缺失或不兼容")
    except IntegrityValidationError:
        raise
    except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
        raise IntegrityValidationError(f"候选数据库结构校验失败：{exc}") from exc
    finally:
        connection.close()
    return {
        "path": validation.path,
        "schema_version": validation.schema_version,
        "integrity_check": list(validation.integrity_check),
        "foreign_key_violations": list(validation.foreign_key_violations),
    }


def install_database_candidate(path: str | Path) -> dict:
    """Install a validated SQLite database candidate and preserve the old copy.

    The caller must first stop external writers and use an isolated decryption or
    import path. After installation, call ``_initialize()`` to apply compatible
    schema upgrades before exposing database-backed features.
    """
    lifecycle = _lifecycle()
    with lifecycle.maintenance(timeout=30.0):
        candidate_validation = validate_database_candidate(path)
        result = lifecycle.install_candidate(
            path, validator=validate_database_candidate, maintenance_timeout=30.0)
        _write_database_manifest()
    return {
        **candidate_validation,
        "active_path": result.active_path,
        "recovery_copy": result.backup_path,
    }


def _schema_candidate_path() -> Path:
    """Allocate a unique sibling candidate owned by this schema upgrade."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    candidate = DB_FILE.with_name(f".{DB_FILE.name}.{uuid.uuid4().hex}.migrating")
    # Exclusive creation makes the subsequent mode=rw open incapable of
    # replacing or adopting another process's candidate file.
    candidate.touch(exist_ok=False)
    return candidate


def _discard_schema_candidate(candidate: Path) -> None:
    """Remove only the candidate and SQLite sidecars created for this upgrade."""
    _remove_sqlite_sidecars(candidate)
    candidate.unlink(missing_ok=True)


def _copy_database_to_schema_candidate(lifecycle: DatabaseLifecycle,
                                       candidate: Path) -> None:
    """Snapshot source data without ever applying schema DDL to the active DB.

    SQLite may open or create its transient WAL/SHM coordination files while
    reading a WAL-mode source. Those sidecars are not schema edits; all DDL and
    migration writes are confined to the private candidate.
    """
    source = None
    target = None
    try:
        source = open_readonly_session(DB_FILE)
        target = lifecycle.connect_for_initialization(candidate, create=False)
        source.backup(target)
        target.commit()
        checkpoint = target.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint and int(checkpoint[0]) != 0:
            raise ActiveConnectionsError("Schema 候选副本的 WAL checkpoint 未完成")
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()


def _upgrade_schema_candidate(lifecycle: DatabaseLifecycle, candidate: Path) -> None:
    """Apply schema changes only to a private copy, then validate its full contract."""
    connection = None
    try:
        connection = lifecycle.connect_for_initialization(candidate, create=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        _initialize_schema(connection)
        connection.commit()
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint and int(checkpoint[0]) != 0:
            raise ActiveConnectionsError("Schema 升级候选库的 WAL checkpoint 未完成")
    finally:
        if connection is not None:
            connection.close()

    validation = validate_database_candidate(candidate)
    if int(validation["schema_version"]) != _SCHEMA_VERSION:
        raise IntegrityValidationError(
            f"候选数据库 Schema 升级未到目标版本："
            f"{validation['schema_version']} != {_SCHEMA_VERSION}")


def _initialize_existing_database() -> None:
    lifecycle = _lifecycle()
    try:
        with lifecycle.maintenance(timeout=30.0):
            _initialize_existing_database_under_maintenance(lifecycle)
    except Exception:
        lifecycle.set_state(DatabaseState.RECOVERY_REQUIRED)
        raise


def _initialize_existing_database_under_maintenance(lifecycle: DatabaseLifecycle) -> None:
    lifecycle.set_state(DatabaseState.OPENING)
    if lifecycle.active_connection_count:
        lifecycle.set_state(DatabaseState.RECOVERY_REQUIRED)
        raise RuntimeError("数据库仍有活跃连接，不能执行 Schema 初始化或验证")
    candidate = None
    try:
        validation = validate_database_candidate(DB_FILE)
        source_version = int(validation["schema_version"])
        if source_version < _SCHEMA_VERSION:
            # Never run schema DDL against the only copy of an existing user
            # database. Build and validate a same-directory SQLite backup,
            # migrate that copy, then let the lifecycle layer atomically install
            # it while preserving a verified recovery copy of the old database.
            candidate = _schema_candidate_path()
            _copy_database_to_schema_candidate(lifecycle, candidate)
            copied = validate_database_candidate(candidate)
            if int(copied["schema_version"]) != source_version:
                raise IntegrityValidationError(
                    "Schema 候选副本版本与活动源数据库不一致，拒绝迁移")
            _upgrade_schema_candidate(lifecycle, candidate)
            install_database_candidate(candidate)
            installed = validate_database_candidate(DB_FILE)
            if int(installed["schema_version"]) != _SCHEMA_VERSION:
                raise IntegrityValidationError(
                    "安装后的数据库 Schema 版本与目标不一致")
        lifecycle.set_state(DatabaseState.READY)
        _write_database_manifest()
    except Exception:
        lifecycle.set_state(DatabaseState.RECOVERY_REQUIRED)
        raise
    finally:
        if candidate is not None:
            _discard_schema_candidate(candidate)


def initialize_new_database() -> dict:
    """Explicit first-install operation; ordinary store connections never create files.

    The method refuses to run when the active DB or any legacy/vault/recovery
    evidence exists. Service code should call this only after the user confirms
    a genuinely new local database, before creating vault metadata.
    """
    lifecycle = _lifecycle()
    if DB_FILE.exists():
        raise DatabaseAlreadyInitializedError(f"数据库已存在，拒绝按新库初始化：{DB_FILE}")
    if _has_non_legacy_initialization_evidence() or _legacy_json_exists():
        lifecycle.classify_missing_database()
        raise DatabaseRecoveryRequiredError(
            "检测到既有数据库、保险库、旧数据或备份，拒绝创建空库；请走迁移或恢复流程")
    validation = lifecycle.initialize_new_database(_initialize_schema)
    _write_database_manifest()
    return {
        "database_file": validation.path,
        "schema_version": validation.schema_version,
        "integrity_check": list(validation.integrity_check),
    }


def _initialize_legacy_json_database() -> None:
    """Migrate an old JSON-only installation without treating it as a fresh profile."""
    lifecycle = _lifecycle()
    with lifecycle.maintenance(timeout=30.0):
        _initialize_legacy_json_database_under_maintenance(lifecycle)


def _initialize_legacy_json_database_under_maintenance(
        lifecycle: DatabaseLifecycle) -> None:
    if _has_locked_vault_evidence() or (DATA_DIR / "database-manifest.json").exists():
        lifecycle.classify_missing_database()
        raise DatabaseRecoveryRequiredError(
            "旧 JSON 与保险库/初始化身份同时存在；需要人工确认迁移来源")
    if _has_non_legacy_initialization_evidence():
        lifecycle.classify_missing_database()
        raise DatabaseRecoveryRequiredError("检测到数据库恢复证据，拒绝自动导入旧 JSON")
    validation = lifecycle.initialize_new_database(
        _initialize_schema, allow_existing_evidence=True)
    backup = _backup_legacy_files()
    _write_database_manifest()
    if backup:
        print(f"[store] 已备份旧数据：{backup}")
    print(f"[store] 已从旧版 JSON 导入并校验 SQLite 数据库：{validation.path}")


def _initialize() -> None:
    """Explicit schema/open operation retained for existing service callers.

    Existing files are validated and upgraded using the historical migration
    logic. A missing database is created only when there is no evidence of prior
    initialization; old JSON-only data follows its dedicated migration path.
    """
    if DB_FILE.is_file():
        _initialize_existing_database()
        return
    if _has_locked_vault_evidence() or any(path.exists() for path in (
            DATA_DIR / "database-manifest.json", DATA_DIR / "library.sqlite3.plain-backup",
            DATA_DIR / "backups")):
        _lifecycle().classify_missing_database()
        raise DatabaseRecoveryRequiredError(
            f"活动数据库缺失但存在保险库或恢复证据：{DB_FILE}")
    if LEGACY_DB_FILE.is_file():
        _migrate_database_location()
        if DB_FILE.is_file():
            _initialize_existing_database()
            return
    if _legacy_json_exists():
        _initialize_legacy_json_database()
        return
    initialize_new_database()


def _startup_initialize() -> None:
    """Open/migrate existing data on import without creating a blank first-run DB."""
    lifecycle = _lifecycle()
    try:
        # An encrypted snapshot is authoritative until vault unlock authenticates
        # and restores it. Never inspect or upgrade a leftover plaintext copy.
        if DB_FILE.is_file() and (DATA_DIR / "library.sqlite3.enc").exists():
            lifecycle.set_state(DatabaseState.LOCKED)
            return
        if DB_FILE.is_file():
            _initialize_existing_database()
            return
        if _has_locked_vault_evidence():
            lifecycle.set_state(DatabaseState.LOCKED)
            return
        if any(path.exists() for path in (
                DATA_DIR / "database-manifest.json", DATA_DIR / "library.sqlite3.plain-backup",
                DATA_DIR / "backups")):
            lifecycle.set_state(DatabaseState.RECOVERY_REQUIRED)
            return
        if LEGACY_DB_FILE.is_file():
            _migrate_database_location()
            if DB_FILE.is_file():
                _initialize_existing_database()
                return
        if _legacy_json_exists():
            _initialize_legacy_json_database()
            return
        lifecycle.classify_missing_database()
    except Exception as exc:
        lifecycle.set_state(DatabaseState.RECOVERY_REQUIRED)
        print(f"[store] 数据库启动校验/迁移未完成，普通访问已关闭：{exc}")
        # Keep the historical migration failure visible to callers. In
        # particular, do not continue startup after rejecting a legacy source.
        if LEGACY_DB_FILE.is_file() and not DB_FILE.is_file():
            raise


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
                        if record.get("metadata_status") == "unavailable":
                            continue  # 下次只补查未返回明细的 ID，不能把占位记录当作缓存命中。
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


def load_metadata_pending_folder_counts() -> dict[str, int]:
    """完整 ID 快照中仍待补齐明细的条数；供续扫及界面显示使用。"""
    with _lock:
        conn = _connect()
        try:
            return {str(row[0]): int(row[1]) for row in conn.execute(
                "SELECT fi.media_id,COUNT(*) FROM folder_items fi "
                "JOIN videos v USING(resource_key) "
                "WHERE json_extract(v.record_json,'$.metadata_status')='unavailable' "
                "GROUP BY fi.media_id")}
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
            return total, [by_key[key] for key in sample_keys if key in by_key
                           and by_key[key].get("metadata_status") != "unavailable"]
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


def delete_folder_profile(media_id: str) -> bool:
    with _transaction() as conn:
        cur = conn.execute("DELETE FROM folder_profiles WHERE media_id=?", (str(media_id),))
        return cur.rowcount > 0


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
        issues = dict(_load_dataset_conn(conn, "scan_issues", {}))
        if str(media_id) in issues:
            issues[str(media_id)].update(status="resolved", resolved_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            _save_dataset_conn(conn, "scan_issues", issues)


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


def load_scan_issues() -> dict:
    return _get_dataset("scan_issues") or {}


def update_scan_issue(media_id: str, **changes) -> dict:
    """Persist troubleshooting evidence independently of a retry's scan state."""
    with _transaction() as conn:
        issues = dict(_load_dataset_conn(conn, "scan_issues", {}))
        issue = issues.setdefault(str(media_id), {"media_id": str(media_id), "history": []})
        event = changes.pop("event", None)
        issue.update(changes)
        if event:
            issue["history"] = (issue.get("history", []) + [
                {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "text": event}])[-20:]
        _save_dataset_conn(conn, "scan_issues", issues)
        return issue


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


def save_analysis(items: dict, meta: dict | None = None):
    """写入 LLM 分析结果。

    meta: {resource_key: {"status": str, "dependent_ids": list[str]}}
    dependent_ids 记录这条结论当初参考了哪些收藏夹的画像，落库后冻结；
    之后内容挪动不会追溯性地把它判为过期，避免执行完方案就触发全量重算。
    """
    meta = meta or {}
    with _transaction() as conn:
        for key, rec in (items or {}).items():
            row_meta = meta.get(str(key)) or {}
            conn.execute(
                """INSERT INTO analyses(resource_key,record_json,status,dependent_ids)
                   VALUES(?,?,?,?)
                   ON CONFLICT(resource_key) DO UPDATE SET
                     record_json=excluded.record_json,
                     status=excluded.status,
                     dependent_ids=excluded.dependent_ids""",
                (str(key), _json(rec), str(row_meta.get("status") or "current"),
                 _json(sorted(row_meta.get("dependent_ids") or []))))


def load_analysis_rows() -> dict:
    """{resource_key: {"record": dict, "status": str, "dependent_ids": list}}"""
    with _lock:
        conn = _connect()
        try:
            return {str(r["resource_key"]): {
                "record": json.loads(r["record_json"]),
                "status": str(r["status"] or "stale"),
                "dependent_ids": json.loads(r["dependent_ids"] or "[]"),
            } for r in conn.execute(
                "SELECT resource_key,record_json,status,dependent_ids FROM analyses")}
        finally:
            conn.close()


def analysis_status_map() -> dict:
    """{resource_key: status}，用于判断哪些条目还需要重算。"""
    with _lock:
        conn = _connect()
        try:
            return {str(r["resource_key"]): str(r["status"] or "stale") for r in conn.execute(
                "SELECT resource_key,status FROM analyses")}
        finally:
            conn.close()


def set_analysis_statuses(updates: dict):
    """updates: {resource_key: (status, dependent_ids)}"""
    with _transaction() as conn:
        for key, (status, deps) in (updates or {}).items():
            conn.execute("UPDATE analyses SET status=?,dependent_ids=? WHERE resource_key=?",
                         (str(status), _json(sorted(deps or [])), str(key)))


def mark_all_analyses_stale():
    """候选收藏夹集合变了（新建/归档/删除）：所有结论的可选前提都变了，一律重算。"""
    with _transaction() as conn:
        conn.execute("UPDATE analyses SET status='stale' WHERE status<>'stale'")


def analysis_status_counts() -> dict:
    with _lock:
        conn = _connect()
        try:
            return {str(r["status"]): int(r["c"]) for r in conn.execute(
                "SELECT status,COUNT(*) c FROM analyses GROUP BY status")}
        finally:
            conn.close()


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
# 候选收藏夹集合指纹：变了说明「可选去向」变了，需要让全部分析结论作废重算
def save_analysis_scope_key(key: str): _set_dataset("analysis_scope_key", str(key or ""))
def load_analysis_scope_key() -> str: return str(_get_dataset("analysis_scope_key") or "")
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
    try:
        capabilities = json.loads(row["capabilities_json"] or "{}")
    except (KeyError, TypeError, ValueError):
        capabilities = {}
    return {"id": row["id"], "provider_id": row["provider_id"], "name": row["name"],
            "context_tokens": int(row["context_tokens"] or 0),
            "max_output_tokens": int(row["max_output_tokens"] or 0),
            "capabilities": capabilities if isinstance(capabilities, dict) else {},
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
                 capabilities: dict | None = None,
                 context_source: str | None = None, output_source: str | None = None,
                 test_status: str = "untested", test_message: str | None = None) -> dict:
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    mid = _new_id()
    with _transaction() as conn:
        conn.execute(
            """INSERT INTO models(id,provider_id,name,context_tokens,max_output_tokens,
               capabilities_json,thinking_effort,test_status,test_message,test_at,created_at,updated_at,
               context_source,output_source)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, str(provider_id), (name or "").strip(),
             max(0, int(context_tokens or 0)), max(0, int(max_output_tokens or 0)),
             _json(capabilities or {}), thinking_effort or "", test_status, test_message,
             now if test_status != "untested" else None, now, now,
             context_source or ("manual" if context_tokens else "unknown"),
             output_source or ("manual" if max_output_tokens else "unknown")))
    return get_model(mid)


def update_model(model_id: str, **fields) -> dict | None:
    existing = get_model(model_id)
    if not existing:
        return None
    allowed = {"name", "context_tokens", "max_output_tokens", "context_source", "output_source", "thinking_effort",
               "capabilities",
               "test_status", "test_message", "test_at", "provider_id"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return existing
    if "name" in updates:
        updates["name"] = (updates["name"] or "").strip() or existing["name"]
    if "capabilities" in updates:
        updates["capabilities_json"] = _json(updates.pop("capabilities") or {})
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
                if (item.get("context_tokens") is not None and
                        str(old["context_source"] or "") != "manual"):
                    updates["context_tokens"] = max(0, int(item["context_tokens"]))
                    updates["context_source"] = item.get("context_source") or "api"
                if (item.get("max_output_tokens") is not None and
                        str(old["output_source"] or "") != "manual"):
                    updates["max_output_tokens"] = max(0, int(item["max_output_tokens"]))
                    updates["output_source"] = item.get("output_source") or "api"
                if item.get("thinking_effort") is not None:
                    updates["thinking_effort"] = str(item["thinking_effort"] or "")
                if item.get("capabilities") is not None:
                    updates["capabilities_json"] = _json(item["capabilities"] or {})
                cols = ", ".join(f"{key}=?" for key in updates)
                conn.execute(f"UPDATE models SET {cols} WHERE id=?",
                             (*updates.values(), old["id"]))
            else:
                conn.execute(
                    """INSERT INTO models(id,provider_id,name,context_tokens,max_output_tokens,
                       capabilities_json,thinking_effort,test_status,test_message,test_at,created_at,updated_at,
                       context_source,output_source)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (_new_id(), str(provider_id), name,
                     max(0, int(item.get("context_tokens") or 0)),
                     max(0, int(item.get("max_output_tokens") or 0)),
                     _json(item.get("capabilities") or {}),
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
            for name, val in (("scan_done", []), ("scan_selection", []), ("scan_issues", {})):
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


_startup_initialize()
