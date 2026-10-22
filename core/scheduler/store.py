"""Persistence adapters for scheduler-owned task metadata.

The production SQLite adapter uses an injected lifecycle connection factory.
Schema creation belongs to the owning database migration; the test adapter
connects with ``mode=rw`` and also refuses to create a missing database file.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable
import json
import logging
import sqlite3
import threading
from contextlib import nullcontext
from urllib.parse import quote

from .models import TaskRecord, TaskStatus, utc_now

logger = logging.getLogger(__name__)


class TaskStoreError(RuntimeError):
    """Base class for durable task storage failures."""


class DatabaseNotFoundError(TaskStoreError):
    """The configured SQLite database does not already exist."""


class TaskNotFoundError(TaskStoreError):
    """The requested task ID is not present."""


class ConcurrentTaskUpdateError(TaskStoreError):
    """A caller attempted to update a task revision that is no longer current."""


class CorruptTaskRecordError(TaskStoreError):
    """A persisted scheduler row cannot be decoded safely."""


@runtime_checkable
class TaskStore(Protocol):
    """Injectable persistence contract used by :class:`TaskScheduler`."""

    def ensure_schema(self) -> None: ...

    def claim_owner_epoch(self, owner_id: str) -> int: ...

    def create(self, record: TaskRecord) -> tuple[TaskRecord, bool]: ...

    def get(self, task_id: str) -> TaskRecord: ...

    def list(self, statuses: set[TaskStatus] | None = None) -> list[TaskRecord]: ...

    def update(self, record: TaskRecord, expected_revision: int) -> TaskRecord: ...


class InMemoryTaskStore:
    """Thread-safe test adapter with the same revision semantics as SQLite."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, TaskRecord] = {}
        self._idempotency: dict[str, str] = {}
        self._owner_epoch = 0

    def ensure_schema(self) -> None:
        return None

    def claim_owner_epoch(self, owner_id: str) -> int:
        if not owner_id:
            raise ValueError("owner_id 不能为空")
        with self._lock:
            self._owner_epoch += 1
            return self._owner_epoch

    def create(self, record: TaskRecord) -> tuple[TaskRecord, bool]:
        with self._lock:
            if record.idempotency_key:
                existing_id = self._idempotency.get(record.idempotency_key)
                if existing_id:
                    return self._records[existing_id], False
            if record.task_id in self._records:
                raise TaskStoreError(f"任务 ID 已存在：{record.task_id}")
            created = replace(record, revision=1)
            self._records[created.task_id] = created
            if created.idempotency_key:
                self._idempotency[created.idempotency_key] = created.task_id
            return created, True

    def get(self, task_id: str) -> TaskRecord:
        with self._lock:
            try:
                return self._records[task_id]
            except KeyError as exc:
                raise TaskNotFoundError(task_id) from exc

    def list(self, statuses: set[TaskStatus] | None = None) -> list[TaskRecord]:
        with self._lock:
            records = list(self._records.values())
        if statuses is not None:
            records = [record for record in records if record.status in statuses]
        return sorted(records, key=lambda row: (row.created_at, row.task_id))

    def update(self, record: TaskRecord, expected_revision: int) -> TaskRecord:
        with self._lock:
            current = self._records.get(record.task_id)
            if current is None:
                raise TaskNotFoundError(record.task_id)
            if current.revision != expected_revision:
                raise ConcurrentTaskUpdateError(
                    f"任务 {record.task_id} revision 已从 {expected_revision} 变为 {current.revision}"
                )
            if record.idempotency_key and record.idempotency_key != current.idempotency_key:
                other_id = self._idempotency.get(record.idempotency_key)
                if other_id and other_id != record.task_id:
                    raise TaskStoreError("idempotency_key 已被其它任务使用")
            updated = replace(record, revision=expected_revision + 1)
            self._records[record.task_id] = updated
            if current.idempotency_key and current.idempotency_key != updated.idempotency_key:
                self._idempotency.pop(current.idempotency_key, None)
            if updated.idempotency_key:
                self._idempotency[updated.idempotency_key] = updated.task_id
            return updated


class SQLiteTaskStore:
    """Persist scheduler records inside an already-created SQLite database."""

    _TASKS_TABLE = "bfo_scheduler_tasks"
    _META_TABLE = "bfo_scheduler_meta"

    def __init__(
        self,
        *,
        connection_factory: Callable[[], sqlite3.Connection],
        database_exists: Callable[[], bool],
        owner_lock_path: str | Path | None = None,
    ) -> None:
        """Build from the lifecycle manager's authorized short-lived connector.

        ``connection_factory`` must return a fresh connection that is already
        authorized by the database lifecycle layer and cannot create a file.
        The store closes each connection after one short metadata transaction.
        ``database_exists`` is a fail-closed preflight; it does not replace the
        lifecycle connection gate, which must still handle races.
        """
        if not callable(connection_factory) or not callable(database_exists):
            raise TypeError("connection_factory 和 database_exists 必须可调用")
        self._connection_factory = connection_factory
        self._database_exists = database_exists
        self.database_path: Path | None = None
        self.owner_lock_path = Path(owner_lock_path).expanduser().resolve() if owner_lock_path else None

    @classmethod
    def for_existing_database_for_tests(
        cls, database_path: str | Path, *, timeout: float = 10.0
    ) -> "SQLiteTaskStore":
        """Testing adapter that opens only a pre-existing file with SQLite mode=rw."""
        path = Path(database_path).expanduser().resolve()

        def connect_existing() -> sqlite3.Connection:
            if not path.is_file():
                raise DatabaseNotFoundError(f"拒绝创建缺失数据库：{path}")
            uri_path = quote(path.as_posix(), safe="/:\\")
            try:
                return sqlite3.connect(
                    f"file:{uri_path}?mode=rw",
                    uri=True,
                    timeout=timeout,
                    isolation_level=None,
                )
            except sqlite3.OperationalError as exc:
                if not path.exists():
                    raise DatabaseNotFoundError(f"拒绝创建缺失数据库：{path}") from exc
                raise

        return cls(
            connection_factory=connect_existing,
            database_exists=path.is_file,
            owner_lock_path=path.with_name(path.name + ".scheduler.lock"),
        )

    @classmethod
    def from_lifecycle(
        cls,
        lifecycle,
        *,
        owner: str = "task-scheduler",
        owner_lock_path: str | Path | None = None,
    ) -> "SQLiteTaskStore":
        """Bind storage to an authorized lifecycle connection gate.

        The lifecycle remains authoritative for READY/DRAINING/LOCKED access;
        each task-store operation requests one short write-capable connection
        and closes it before returning.
        """
        database_path = Path(lifecycle.path).expanduser().resolve()
        store = cls(
            connection_factory=lambda: lifecycle.connect(mode="rw", owner=owner),
            database_exists=database_path.is_file,
            owner_lock_path=(owner_lock_path or
                             database_path.with_name(database_path.name + ".scheduler.lock")),
        )
        # Kept for the guarded write opener below: task writes open a fresh
        # connection per operation, and the open must not race a sibling close.
        store._lifecycle = lifecycle
        return store

    def _open_write_transaction(self) -> sqlite3.Connection:
        """Open a connection and begin its write transaction as one step.

        Opening and immediately writing is the only window where a sibling
        connection closing (which deletes the WAL shared-memory file) can make a
        brand-new connection read-only. Holding the lifecycle gate across
        open+first statement removes that window; if SQLite still reports the
        connection as read-only, nothing has executed yet, so retrying exactly
        once on a new connection is safe. The first failure is always logged.
        """
        lifecycle = getattr(self, "_lifecycle", None)
        gate = lifecycle.connection_gate() if lifecycle is not None else nullcontext()
        with gate:
            conn = self._connect_ready()
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                conn.close()
                if "readonly database" not in str(exc).lower():
                    raise
                logger.warning(
                    "任务写入建立事务时数据库被判定为只读（并发关闭删除了 WAL 共享内存），"
                    "重开连接重试一次：%s", exc)
                conn = self._connect_ready()
                try:
                    conn.execute("BEGIN IMMEDIATE")
                except BaseException:
                    # The second attempt can fail too. Release its lifecycle lease
                    # explicitly before preserving the original exception.
                    try:
                        conn.close()
                    except Exception:
                        logger.exception("关闭重试失败的调度器 SQLite 连接时出错")
                    raise
        return conn

    def _connect(self) -> sqlite3.Connection:
        if not self._database_exists():
            raise DatabaseNotFoundError("生命周期层报告数据库不存在，调度器拒绝连接")
        conn = self._connection_factory()
        if not isinstance(conn, sqlite3.Connection):
            try:
                conn.close()
            except Exception:
                pass
            raise TypeError("connection_factory 必须返回独占的 sqlite3.Connection")
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            return conn
        except Exception:
            # A lifecycle connection owns a lease as soon as the factory
            # returns.  If connection setup fails (for example the PRAGMA is
            # rejected), release that lease before propagating the error.
            try:
                conn.close()
            except Exception:
                pass
            raise

    def _connect_ready(self) -> sqlite3.Connection:
        conn = self._connect()
        try:
            self._require_schema(conn)
            return conn
        except Exception:
            conn.close()
            raise

    @staticmethod
    def _json(value, *, label: str) -> str | None:
        if value is None:
            return None
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} 不能序列化为严格 JSON") from exc

    @staticmethod
    def _decode_json(raw, *, label: str, default=None):
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise CorruptTaskRecordError(f"任务记录字段 {label} 不是有效 JSON") from exc

    @classmethod
    def create_schema(cls, connection: sqlite3.Connection) -> None:
        """Add scheduler tables inside the caller's explicit DB migration.

        This method neither opens nor closes a connection and never commits;
        the owning storage/lifecycle migration controls its transaction.
        """
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("create_schema 需要生命周期层提供的 sqlite3.Connection")
        connection.execute(f"""
            CREATE TABLE IF NOT EXISTS {cls._META_TABLE} (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        connection.execute(f"""
            CREATE TABLE IF NOT EXISTS {cls._TASKS_TABLE} (
                task_id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL,
                task_schema_version INTEGER NOT NULL CHECK(task_schema_version > 0),
                checkpoint_schema_version INTEGER NOT NULL CHECK(checkpoint_schema_version > 0),
                status TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                checkpoint_json TEXT,
                progress_json TEXT NOT NULL,
                error_json TEXT,
                resume_policy TEXT NOT NULL,
                pause_reason TEXT,
                resume_intent TEXT NOT NULL,
                attempt INTEGER NOT NULL CHECK(attempt >= 0),
                owner_epoch INTEGER NOT NULL CHECK(owner_epoch >= 0),
                resource_keys_json TEXT NOT NULL,
                idempotency_key TEXT UNIQUE,
                reconciliation_required INTEGER NOT NULL DEFAULT 0,
                reconciliation_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK(revision > 0)
            )
        """)
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_bfo_scheduler_status_created "
            f"ON {cls._TASKS_TABLE}(status, created_at)"
        )
        connection.execute(
            f"INSERT OR IGNORE INTO {cls._META_TABLE}(key,value) "
            "VALUES('owner_epoch','0')"
        )

    @classmethod
    def _require_schema(cls, connection: sqlite3.Connection) -> None:
        tables = {
            str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required_tables = {cls._TASKS_TABLE, cls._META_TABLE}
        missing = required_tables - tables
        if missing:
            raise TaskStoreError(
                "调度器 Schema 尚未安装；必须先通过数据库版本化迁移创建："
                + ", ".join(sorted(missing))
            )
        required_columns = {
            "task_id", "task_type", "task_schema_version", "checkpoint_schema_version",
            "status", "parameters_json", "checkpoint_json", "progress_json", "error_json",
            "resume_policy", "pause_reason", "resume_intent", "attempt", "owner_epoch",
            "resource_keys_json", "idempotency_key", "reconciliation_required",
            "reconciliation_reason", "created_at", "updated_at", "revision",
        }
        actual_columns = {
            str(row[1]) for row in connection.execute(
                f"PRAGMA table_info({cls._TASKS_TABLE})"
            )
        }
        missing_columns = required_columns - actual_columns
        if missing_columns:
            raise TaskStoreError(
                "调度器 Schema 不兼容，缺少字段：" + ", ".join(sorted(missing_columns))
            )

    def ensure_schema(self) -> None:
        """Verify the migration-installed schema without modifying the database."""
        conn = self._connect()
        try:
            self._require_schema(conn)
        finally:
            conn.close()

    def claim_owner_epoch(self, owner_id: str) -> int:
        if not owner_id:
            raise ValueError("owner_id 不能为空")
        conn = self._open_write_transaction()
        try:
            row = conn.execute(
                f"SELECT value FROM {self._META_TABLE} WHERE key='owner_epoch'"
            ).fetchone()
            epoch = int(row[0] if row else 0) + 1
            conn.execute(
                f"INSERT INTO {self._META_TABLE}(key,value) VALUES('owner_epoch',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(epoch),),
            )
            conn.commit()
            return epoch
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create(self, record: TaskRecord) -> tuple[TaskRecord, bool]:
        conn = self._open_write_transaction()
        try:
            if record.idempotency_key:
                row = conn.execute(
                    f"SELECT * FROM {self._TASKS_TABLE} WHERE idempotency_key=?",
                    (record.idempotency_key,),
                ).fetchone()
                if row:
                    conn.commit()
                    return self._from_row(row), False
            columns, values = self._values(replace(record, revision=1))
            placeholders = ",".join("?" for _ in values)
            conn.execute(
                f"INSERT INTO {self._TASKS_TABLE}({','.join(columns)}) "
                f"VALUES({placeholders})",
                values,
            )
            conn.commit()
            return replace(record, revision=1), True
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            if record.idempotency_key:
                row = conn.execute(
                    f"SELECT * FROM {self._TASKS_TABLE} WHERE idempotency_key=?",
                    (record.idempotency_key,),
                ).fetchone()
                if row:
                    return self._from_row(row), False
            raise TaskStoreError(str(exc)) from exc
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, task_id: str) -> TaskRecord:
        conn = self._connect_ready()
        try:
            row = conn.execute(
                f"SELECT * FROM {self._TASKS_TABLE} WHERE task_id=?", (task_id,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise TaskNotFoundError(task_id)
        return self._from_row(row)

    def list(self, statuses: set[TaskStatus] | None = None) -> list[TaskRecord]:
        conn = self._connect_ready()
        try:
            if statuses:
                values = [status.value for status in sorted(statuses, key=lambda x: x.value)]
                marks = ",".join("?" for _ in values)
                rows = conn.execute(
                    f"SELECT * FROM {self._TASKS_TABLE} WHERE status IN ({marks}) "
                    "ORDER BY created_at, task_id",
                    values,
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM {self._TASKS_TABLE} ORDER BY created_at, task_id"
                ).fetchall()
        finally:
            conn.close()
        return [self._from_row(row) for row in rows]

    def update(self, record: TaskRecord, expected_revision: int) -> TaskRecord:
        # Schema verification happens on the same short-lived connection.
        updated = replace(record, revision=expected_revision + 1, updated_at=utc_now())
        columns, values = self._values(updated)
        mutable_columns = [column for column in columns if column != "task_id"]
        assignments = ",".join(f"{column}=?" for column in mutable_columns)
        parameters = [values[columns.index(column)] for column in mutable_columns]
        parameters.extend((record.task_id, expected_revision))
        conn = self._open_write_transaction()
        try:
            cursor = conn.execute(
                f"UPDATE {self._TASKS_TABLE} SET {assignments} "
                "WHERE task_id=? AND revision=?",
                parameters,
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    f"SELECT revision FROM {self._TASKS_TABLE} WHERE task_id=?",
                    (record.task_id,),
                ).fetchone()
                if row is None:
                    raise TaskNotFoundError(record.task_id)
                raise ConcurrentTaskUpdateError(
                    f"任务 {record.task_id} revision 已从 {expected_revision} 变为 {row[0]}"
                )
            conn.commit()
            return updated
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _values(self, record: TaskRecord) -> tuple[list[str], list[object]]:
        columns = [
            "task_id", "task_type", "task_schema_version", "checkpoint_schema_version",
            "status", "parameters_json", "checkpoint_json", "progress_json", "error_json",
            "resume_policy", "pause_reason", "resume_intent", "attempt", "owner_epoch",
            "resource_keys_json", "idempotency_key", "reconciliation_required",
            "reconciliation_reason", "created_at", "updated_at", "revision",
        ]
        values = [
            record.task_id,
            record.task_type,
            record.task_schema_version,
            record.checkpoint_schema_version,
            record.status.value,
            self._json(record.parameters, label="parameters"),
            self._json(record.checkpoint, label="checkpoint"),
            self._json(record.progress, label="progress"),
            self._json(record.error, label="error"),
            record.resume_policy.value,
            record.pause_reason.value if record.pause_reason else None,
            record.resume_intent.value,
            record.attempt,
            record.owner_epoch,
            self._json(list(record.resource_keys), label="resource_keys"),
            record.idempotency_key,
            int(record.reconciliation_required),
            record.reconciliation_reason,
            record.created_at,
            record.updated_at,
            record.revision,
        ]
        return columns, values

    def _from_row(self, row: sqlite3.Row) -> TaskRecord:
        try:
            return TaskRecord(
                task_id=row["task_id"],
                task_type=row["task_type"],
                task_schema_version=int(row["task_schema_version"]),
                checkpoint_schema_version=int(row["checkpoint_schema_version"]),
                status=TaskStatus(row["status"]),
                parameters=self._decode_json(row["parameters_json"], label="parameters", default={}),
                checkpoint=self._decode_json(row["checkpoint_json"], label="checkpoint"),
                progress=self._decode_json(row["progress_json"], label="progress", default={}),
                error=self._decode_json(row["error_json"], label="error"),
                resume_policy=row["resume_policy"],
                pause_reason=row["pause_reason"],
                resume_intent=row["resume_intent"],
                attempt=int(row["attempt"]),
                owner_epoch=int(row["owner_epoch"]),
                resource_keys=tuple(self._decode_json(
                    row["resource_keys_json"], label="resource_keys", default=[])),
                idempotency_key=row["idempotency_key"],
                reconciliation_required=bool(row["reconciliation_required"]),
                reconciliation_reason=row["reconciliation_reason"] or "",
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                revision=int(row["revision"]),
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise CorruptTaskRecordError(
                f"任务记录 {row['task_id']!r} 的字段无效：{exc}"
            ) from exc
