"""Durable coordination primitives for Bilibili write operations.

This module deliberately does not open a database path.  The application must
inject a connection owned by the database lifecycle manager after that manager
has verified the existing database is READY.  In particular, constructing a
repository can never create a missing or empty application database.

The repository persists ``DISPATCHING`` in its own committed transaction
before the coordinator invokes the network callback.  Network I/O is therefore
never performed while a SQLite transaction is held.  A process restart turns
stranded ``DISPATCHING`` rows into ``NEEDS_RECONCILIATION``; it never retries
them automatically.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Sequence


SCHEMA_VERSION = 1
_SECRET_KEY_PARTS = (
    "cookie", "csrf", "token", "secret", "password", "authorization",
    "sessdata", "bili_jct", "dedeuserid", "credential", "private_key",
)
_SAFE_CODE_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


class OperationStatus(str, Enum):
    PREPARED = "PREPARED"
    DISPATCHING = "DISPATCHING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    NOT_APPLIED = "NOT_APPLIED"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class RemoteOperationError(RuntimeError):
    """Base error for operation-log and state-machine violations."""


class OperationNotFound(RemoteOperationError):
    pass


class OperationIdentityConflict(RemoteOperationError):
    """An operation ID was reused for a different logical write."""


class InvalidOperationTransition(RemoteOperationError):
    pass


class ConflictingOperation(RemoteOperationError):
    """An unresolved write owns a conflicting account/resource scope."""


class UnsafeRetry(RemoteOperationError):
    """A retry lacks proof that the previous write was not applied."""


class RepositoryStateError(RemoteOperationError):
    """The injected SQLite connection is not in a usable transaction state."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def redact_sensitive(value: Any) -> Any:
    """Return JSON-safe data with common credential fields removed.

    Callers should pass summaries, not raw HTTP requests or full API objects.
    The redactor is a final guard, not a substitute for that contract.
    """
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.casefold().replace("-", "_")
            if any(part in lowered for part in _SECRET_KEY_PARTS):
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = redact_sensitive(item)
        return result
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # Don't serialize arbitrary objects (which might expose credentials).
    return f"<{type(value).__name__}>"


def _safe_code(value: str | None, fallback: str = "unspecified") -> str:
    text = _SAFE_CODE_RE.sub("_", str(value or fallback)).strip("_:")
    return (text or fallback)[:100]


def stable_operation_id(
    *,
    task_id: str,
    account_id: str,
    operation_type: str,
    target: Mapping[str, Any] | None = None,
    entity_ids: Sequence[str | int] = (),
    preconditions: Mapping[str, Any] | None = None,
    postconditions: Mapping[str, Any] | None = None,
    request_summary: Mapping[str, Any] | None = None,
) -> str:
    """Build a deterministic key for one logical write intent.

    The task ID intentionally participates in the key: restarting/resuming the
    same task reproduces the ID, while a newly requested task has a new intent.
    Entity IDs are treated as a set for batch writes, so reordering a batch does
    not accidentally create a second operation.  Secrets are redacted before
    hashing and must never be used as logical identity inputs.
    """
    if not str(task_id).strip() or not str(account_id).strip() or not str(operation_type).strip():
        raise ValueError("task_id、account_id 和 operation_type 必须非空")
    identity = {
        "version": 1,
        "task_id": str(task_id),
        "account_id": str(account_id),
        "operation_type": str(operation_type),
        "target": redact_sensitive(target or {}),
        "entity_ids": sorted({str(item) for item in entity_ids}),
        "preconditions": redact_sensitive(preconditions or {}),
        "postconditions": redact_sensitive(postconditions or {}),
        "request_summary": redact_sensitive(request_summary or {}),
    }
    digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    return f"bfo-op-v1-{digest}"


@dataclass(frozen=True)
class OperationIntent:
    """Immutable description of the intended remote side effect."""

    task_id: str
    account_id: str
    operation_type: str
    target: Mapping[str, Any] = field(default_factory=dict)
    entity_ids: Sequence[str | int] = field(default_factory=tuple)
    preconditions: Mapping[str, Any] = field(default_factory=dict)
    postconditions: Mapping[str, Any] = field(default_factory=dict)
    request_summary: Mapping[str, Any] = field(default_factory=dict)
    # Wildcard is the conservative default: unresolved writes serialize all
    # writes for this account. More granular keys can be introduced by callers
    # only when the operation-specific conflict rules are known.
    resource_keys: Sequence[str] = ("*",)
    operation_id: str = ""

    def __post_init__(self) -> None:
        if not str(self.task_id).strip() or not str(self.account_id).strip():
            raise ValueError("task_id 和 account_id 必须非空")
        if not str(self.operation_type).strip():
            raise ValueError("operation_type 必须非空")
        safe_target = redact_sensitive(self.target or {})
        safe_pre = redact_sensitive(self.preconditions or {})
        safe_post = redact_sensitive(self.postconditions or {})
        safe_request = redact_sensitive(self.request_summary or {})
        entities = tuple(sorted({str(item) for item in self.entity_ids}))
        resources = tuple(sorted({str(item).strip() for item in self.resource_keys if str(item).strip()}))
        if not resources:
            resources = ("*",)
        object.__setattr__(self, "target", safe_target)
        object.__setattr__(self, "entity_ids", entities)
        object.__setattr__(self, "preconditions", safe_pre)
        object.__setattr__(self, "postconditions", safe_post)
        object.__setattr__(self, "request_summary", safe_request)
        object.__setattr__(self, "resource_keys", resources)
        generated = stable_operation_id(
            task_id=self.task_id,
            account_id=self.account_id,
            operation_type=self.operation_type,
            target=safe_target,
            entity_ids=entities,
            preconditions=safe_pre,
            postconditions=safe_post,
            request_summary=safe_request,
        )
        if self.operation_id and self.operation_id != generated:
            raise ValueError("operation_id 与操作意图不匹配")
        object.__setattr__(self, "operation_id", generated)

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "task_id": self.task_id,
            "account_id": self.account_id,
            "operation_type": self.operation_type,
            "target": self.target,
            "entity_ids": list(self.entity_ids),
            "preconditions": self.preconditions,
            "postconditions": self.postconditions,
            "request_summary": self.request_summary,
            "resource_keys": list(self.resource_keys),
        }


@dataclass(frozen=True)
class RemoteOperation:
    operation_id: str
    task_id: str
    account_id: str
    operation_type: str
    target: Mapping[str, Any]
    entity_ids: tuple[str, ...]
    preconditions: Mapping[str, Any]
    postconditions: Mapping[str, Any]
    request_summary: Mapping[str, Any]
    resource_keys: tuple[str, ...]
    status: OperationStatus
    attempt: int
    version: int
    remote_response: Mapping[str, Any] | None
    local_result: Mapping[str, Any] | None
    reconciliation: Mapping[str, Any] | None
    error_code: str | None
    created_at: str
    updated_at: str
    resolved_at: str | None


@dataclass(frozen=True)
class OperationEvent:
    operation_id: str
    from_status: OperationStatus | None
    to_status: OperationStatus
    event_type: str
    details: Mapping[str, Any]
    created_at: str


class RemoteOperationRepository(Protocol):
    """Injected persistence contract required by the coordinator."""

    def prepare(self, intent: OperationIntent) -> RemoteOperation: ...
    def get(self, operation_id: str) -> RemoteOperation: ...
    def begin_dispatch(self, operation_id: str, *, retry: bool = False) -> RemoteOperation: ...
    def mark_confirmed(self, operation_id: str, *, remote_response: Any = None,
                       local_commit: Callable[[sqlite3.Connection, Any], Any] | None = None,
                       response: Any = None) -> RemoteOperation: ...
    def mark_rejected(self, operation_id: str, *, error_code: str) -> RemoteOperation: ...
    def mark_needs_reconciliation(self, operation_id: str, *, reason_code: str) -> RemoteOperation: ...
    def record_reconciliation(self, operation_id: str, *, decision: str,
                              preconditions_valid: bool | None = None,
                              evidence: Any = None) -> RemoteOperation: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS remote_operations (
    operation_id TEXT PRIMARY KEY,
    intent_json TEXT NOT NULL,
    task_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    operation_type TEXT NOT NULL,
    resource_keys_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'PREPARED','DISPATCHING','CONFIRMED','REJECTED',
        'NEEDS_RECONCILIATION','NOT_APPLIED','MANUAL_REVIEW'
    )),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    remote_response_json TEXT,
    local_result_json TEXT,
    reconciliation_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_remote_operations_account_status
    ON remote_operations(account_id, status);
CREATE TABLE IF NOT EXISTS remote_operation_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(operation_id) REFERENCES remote_operations(operation_id)
);
CREATE INDEX IF NOT EXISTS idx_remote_operation_events_operation
    ON remote_operation_events(operation_id, event_id);
"""


class SqliteRemoteOperationRepository:
    """Repository adapter over a lifecycle-manager-owned SQLite connection.

    No path is accepted and no connection is opened. ``create_schema`` is an
    explicit schema operation that must be called by the application migration
    path after the existing database has passed lifecycle validation. The
    repository does not close the injected connection.

    The connection must not already be inside a transaction when repository
    methods are called. Sharing it with other components requires the database
    manager to serialize access; this class serializes its own calls only.
    """

    _UNRESOLVED = {
        OperationStatus.DISPATCHING.value,
        OperationStatus.NEEDS_RECONCILIATION.value,
        OperationStatus.MANUAL_REVIEW.value,
        # The negative result may become stale if another write runs before
        # the caller retries. Keep the account/resource held until the caller
        # retries this same operation or escalates it for manual review.
        OperationStatus.NOT_APPLIED.value,
    }

    def __init__(self, connection: sqlite3.Connection, *, clock: Callable[[], str] = utc_now):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("必须注入已由数据库生命周期管理器打开的 sqlite3.Connection")
        self._connection = connection
        self._clock = clock
        self._lock = threading.RLock()

    @staticmethod
    def create_schema(connection: sqlite3.Connection) -> None:
        """Install this module's tables on an already-open validated database.

        This does not open or create a database file. It is kept explicit so a
        migration/lifecycle owner controls when schema changes are permitted.
        """
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection 必须是已打开的 sqlite3.Connection")
        if connection.in_transaction:
            raise RepositoryStateError("不能在活动事务中安装远端操作日志 schema")
        version_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='remote_operation_schema_version'"
        ).fetchone()
        if version_table:
            current = connection.execute(
                "SELECT version FROM remote_operation_schema_version LIMIT 1"
            ).fetchone()
            if current is not None and int(current[0]) != SCHEMA_VERSION:
                raise RepositoryStateError(
                    f"远端操作日志 schema 版本不兼容：{current[0]} != {SCHEMA_VERSION}"
                )
        connection.executescript(_SCHEMA)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS remote_operation_schema_version "
            "(version INTEGER NOT NULL)"
        )
        row = connection.execute(
            "SELECT version FROM remote_operation_schema_version LIMIT 1"
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO remote_operation_schema_version(version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
        elif int(row[0]) != SCHEMA_VERSION:  # Guard against concurrent migration.
            raise RepositoryStateError(f"schema 版本已变化：{row[0]}")
        connection.commit()

    def prepare(self, intent: OperationIntent) -> RemoteOperation:
        if not isinstance(intent, OperationIntent):
            raise TypeError("intent 必须是 OperationIntent")
        now = self._clock()
        intent_json = _canonical_json(intent.as_dict())
        with self._transaction() as conn:
            existing = self._select_one(conn, intent.operation_id)
            if existing is not None:
                if existing["intent_json"] != intent_json:
                    raise OperationIdentityConflict(
                        "相同 operation_id 对应不同操作意图，拒绝覆盖"
                    )
                return self._decode_row(existing)
            conn.execute(
                """INSERT INTO remote_operations (
                    operation_id,intent_json,task_id,account_id,operation_type,
                    resource_keys_json,status,attempt,version,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,0,0,?,?)""",
                (intent.operation_id, intent_json, intent.task_id, intent.account_id,
                 intent.operation_type, _canonical_json(list(intent.resource_keys)),
                 OperationStatus.PREPARED.value, now, now),
            )
            self._append_event(
                conn, intent.operation_id, None, OperationStatus.PREPARED,
                "PREPARED", {"schema_version": SCHEMA_VERSION}, now,
            )
            return self._decode_row(self._select_one(conn, intent.operation_id))

    def get(self, operation_id: str) -> RemoteOperation:
        with self._lock:
            row = self._select_one(self._connection, operation_id)
            if row is None:
                raise OperationNotFound(operation_id)
            return self._decode_row(row)

    def list_operations(
        self, *, task_id: str | None = None, account_id: str | None = None,
        statuses: Sequence[OperationStatus | str] | None = None,
    ) -> list[RemoteOperation]:
        clauses: list[str] = []
        values: list[Any] = []
        if task_id is not None:
            clauses.append("task_id=?")
            values.append(str(task_id))
        if account_id is not None:
            clauses.append("account_id=?")
            values.append(str(account_id))
        if statuses:
            status_values = [item.value if isinstance(item, OperationStatus) else str(item)
                             for item in statuses]
            clauses.append("status IN (" + ",".join("?" for _ in status_values) + ")")
            values.extend(status_values)
        sql = "SELECT * FROM remote_operations"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, operation_id"
        with self._lock:
            rows = self._connection.execute(sql, values).fetchall()
            return [self._decode_row(self._row_mapping(self._connection, row)) for row in rows]

    def events(self, operation_id: str) -> list[OperationEvent]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT operation_id,from_status,to_status,event_type,details_json,created_at
                   FROM remote_operation_events WHERE operation_id=? ORDER BY event_id""",
                (operation_id,),
            ).fetchall()
            if not rows and self._select_one(self._connection, operation_id) is None:
                raise OperationNotFound(operation_id)
            return [OperationEvent(
                operation_id=row[0],
                from_status=OperationStatus(row[1]) if row[1] else None,
                to_status=OperationStatus(row[2]),
                event_type=row[3],
                details=json.loads(row[4]),
                created_at=row[5],
            ) for row in rows]

    def begin_dispatch(self, operation_id: str, *, retry: bool = False) -> RemoteOperation:
        now = self._clock()
        with self._transaction() as conn:
            row = self._select_one(conn, operation_id)
            if row is None:
                raise OperationNotFound(operation_id)
            current = OperationStatus(row["status"])
            if retry:
                if current is not OperationStatus.NOT_APPLIED:
                    raise UnsafeRetry("只有已核对为 NOT_APPLIED 的原操作可以重试")
                reconciliation = json.loads(row["reconciliation_json"] or "{}")
                if reconciliation.get("preconditions_valid") is not True:
                    raise UnsafeRetry("未证明原操作前置状态仍有效，禁止重试")
            elif current is not OperationStatus.PREPARED:
                if current is OperationStatus.NOT_APPLIED:
                    raise UnsafeRetry("已核对未生效的操作必须通过显式 retry=True 重试")
                raise InvalidOperationTransition(
                    f"不能从 {current.value} 进入 DISPATCHING"
                )
            self._assert_no_conflict(conn, row)
            attempt = int(row["attempt"]) + 1
            changed = conn.execute(
                """UPDATE remote_operations
                   SET status=?, attempt=?, version=version+1, error_code=NULL, updated_at=?
                   WHERE operation_id=? AND status=? AND version=?""",
                (OperationStatus.DISPATCHING.value, attempt, now, operation_id,
                 current.value, int(row["version"])),
            ).rowcount
            if changed != 1:
                raise InvalidOperationTransition("操作状态已被并发更新，未发送请求")
            self._append_event(
                conn, operation_id, current, OperationStatus.DISPATCHING,
                "DISPATCHING", {"attempt": attempt, "retry": bool(retry)}, now,
            )
            return self._decode_row(self._select_one(conn, operation_id))

    def mark_confirmed(
        self, operation_id: str, *, remote_response: Any = None,
        local_commit: Callable[[sqlite3.Connection, Any], Any] | None = None,
        response: Any = None,
    ) -> RemoteOperation:
        now = self._clock()
        with self._transaction() as conn:
            row = self._select_one(conn, operation_id)
            if row is None:
                raise OperationNotFound(operation_id)
            current = OperationStatus(row["status"])
            if current is not OperationStatus.DISPATCHING:
                raise InvalidOperationTransition(
                    f"不能从 {current.value} 确认远端成功"
                )
            local_result = local_commit(conn, response) if local_commit else None
            response_json = self._json_or_none(remote_response)
            local_json = self._json_or_none(local_result)
            conn.execute(
                """UPDATE remote_operations SET status=?, remote_response_json=?,
                   local_result_json=?, error_code=NULL, version=version+1,
                   updated_at=?, resolved_at=? WHERE operation_id=?""",
                (OperationStatus.CONFIRMED.value, response_json, local_json,
                 now, now, operation_id),
            )
            self._append_event(
                conn, operation_id, current, OperationStatus.CONFIRMED,
                "CONFIRMED", {"local_commit": local_commit is not None}, now,
            )
            return self._decode_row(self._select_one(conn, operation_id))

    def mark_rejected(self, operation_id: str, *, error_code: str) -> RemoteOperation:
        return self._finish_dispatch(
            operation_id, OperationStatus.REJECTED, "REJECTED",
            error_code=_safe_code(error_code),
        )

    def mark_needs_reconciliation(
        self, operation_id: str, *, reason_code: str,
    ) -> RemoteOperation:
        return self._finish_dispatch(
            operation_id, OperationStatus.NEEDS_RECONCILIATION,
            "NEEDS_RECONCILIATION", error_code=_safe_code(reason_code),
        )

    def record_reconciliation(
        self, operation_id: str, *, decision: str,
        preconditions_valid: bool | None = None, evidence: Any = None,
    ) -> RemoteOperation:
        mapping = {
            "confirmed": OperationStatus.CONFIRMED,
            "not_applied": OperationStatus.NOT_APPLIED,
            "manual_review": OperationStatus.MANUAL_REVIEW,
        }
        if decision not in mapping:
            raise ValueError("decision 必须是 confirmed、not_applied 或 manual_review")
        if decision == "not_applied" and preconditions_valid is not True:
            raise UnsafeRetry(
                "只有证明前置状态仍有效，才能把操作核对为可重试的 NOT_APPLIED"
            )
        now = self._clock()
        destination = mapping[decision]
        evidence_safe = redact_sensitive(evidence) if evidence is not None else None
        reconciliation = {
            "decision": decision,
            "preconditions_valid": preconditions_valid,
            "evidence": evidence_safe,
            "checked_at": now,
        }
        with self._transaction() as conn:
            row = self._select_one(conn, operation_id)
            if row is None:
                raise OperationNotFound(operation_id)
            current = OperationStatus(row["status"])
            if current is not OperationStatus.NEEDS_RECONCILIATION:
                raise InvalidOperationTransition(
                    f"不能从 {current.value} 执行远端结果核对"
                )
            conn.execute(
                """UPDATE remote_operations SET status=?, reconciliation_json=?,
                   error_code=NULL, version=version+1, updated_at=?,
                   resolved_at=? WHERE operation_id=?""",
                (destination.value, _canonical_json(reconciliation), now,
                 None if destination is OperationStatus.NOT_APPLIED else now,
                 operation_id),
            )
            self._append_event(
                conn, operation_id, current, destination, "RECONCILED",
                reconciliation, now,
            )
            return self._decode_row(self._select_one(conn, operation_id))

    def recover_stranded_dispatches(self) -> int:
        """Fail-safe startup recovery for DISPATCHING rows.

        The caller must first establish single-process scheduler ownership so a
        second live process cannot reclassify another process's in-flight call.
        """
        now = self._clock()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT operation_id,version FROM remote_operations WHERE status=?",
                (OperationStatus.DISPATCHING.value,),
            ).fetchall()
            for operation_id, version in rows:
                conn.execute(
                    """UPDATE remote_operations SET status=?, error_code=?,
                       version=version+1, updated_at=? WHERE operation_id=?
                       AND status=? AND version=?""",
                    (OperationStatus.NEEDS_RECONCILIATION.value,
                     "process_restart", now, operation_id,
                     OperationStatus.DISPATCHING.value, int(version)),
                )
                self._append_event(
                    conn, operation_id, OperationStatus.DISPATCHING,
                    OperationStatus.NEEDS_RECONCILIATION,
                    "RECOVERED_AFTER_RESTART", {"reason_code": "process_restart"}, now,
                )
            return len(rows)

    def _finish_dispatch(
        self, operation_id: str, destination: OperationStatus, event_type: str,
        *, error_code: str,
    ) -> RemoteOperation:
        now = self._clock()
        with self._transaction() as conn:
            row = self._select_one(conn, operation_id)
            if row is None:
                raise OperationNotFound(operation_id)
            current = OperationStatus(row["status"])
            if current is not OperationStatus.DISPATCHING:
                raise InvalidOperationTransition(
                    f"不能从 {current.value} 结束正在发送的操作"
                )
            conn.execute(
                """UPDATE remote_operations SET status=?, error_code=?,
                   version=version+1, updated_at=?, resolved_at=?
                   WHERE operation_id=?""",
                (destination.value, error_code, now,
                 now if destination is OperationStatus.REJECTED else None,
                 operation_id),
            )
            self._append_event(
                conn, operation_id, current, destination, event_type,
                {"error_code": error_code}, now,
            )
            return self._decode_row(self._select_one(conn, operation_id))

    def _assert_no_conflict(self, conn: sqlite3.Connection, operation_row: Mapping[str, Any]) -> None:
        unresolved = sorted(self._UNRESOLVED)
        status_placeholders = ",".join("?" for _ in unresolved)
        rows = conn.execute(
            """SELECT operation_id,resource_keys_json,status FROM remote_operations
               WHERE account_id=? AND operation_id<>? AND status IN ("""
            + status_placeholders + ")",
            (operation_row["account_id"], operation_row["operation_id"],
             *unresolved),
        ).fetchall()
        resources = set(json.loads(operation_row["resource_keys_json"] or '["*"]')) or {"*"}
        for blocker_id, blocker_keys_json, blocker_status in rows:
            blocker_resources = set(json.loads(blocker_keys_json or '["*"]')) or {"*"}
            conflict = "*" in resources or "*" in blocker_resources or bool(resources & blocker_resources)
            if conflict:
                raise ConflictingOperation(
                    f"同账号存在未核对写操作 {blocker_id} ({blocker_status})，阻止发送"
                )

    def _transaction(self):
        return _Transaction(self._connection, self._lock)

    @staticmethod
    def _select_one(conn: sqlite3.Connection, operation_id: str) -> dict[str, Any] | None:
        cursor = conn.execute(
            "SELECT * FROM remote_operations WHERE operation_id=?", (operation_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {description[0]: value for description, value in zip(cursor.description, row)}

    @classmethod
    def _row_mapping(cls, conn: sqlite3.Connection, row: Sequence[Any]) -> dict[str, Any]:
        # Used only for rows returned by a direct SELECT with its own cursor.
        cursor = conn.execute("SELECT * FROM remote_operations LIMIT 0")
        return {description[0]: value for description, value in zip(cursor.description, row)}

    @classmethod
    def _decode_row(cls, row: Mapping[str, Any] | None) -> RemoteOperation:
        if row is None:
            raise OperationNotFound("operation row disappeared")
        intent = json.loads(row["intent_json"])
        return RemoteOperation(
            operation_id=row["operation_id"],
            task_id=row["task_id"],
            account_id=row["account_id"],
            operation_type=row["operation_type"],
            target=intent["target"],
            entity_ids=tuple(intent["entity_ids"]),
            preconditions=intent["preconditions"],
            postconditions=intent["postconditions"],
            request_summary=intent["request_summary"],
            resource_keys=tuple(json.loads(row["resource_keys_json"])),
            status=OperationStatus(row["status"]),
            attempt=int(row["attempt"]),
            version=int(row["version"]),
            remote_response=json.loads(row["remote_response_json"])
                if row["remote_response_json"] else None,
            local_result=json.loads(row["local_result_json"])
                if row["local_result_json"] else None,
            reconciliation=json.loads(row["reconciliation_json"])
                if row["reconciliation_json"] else None,
            error_code=row["error_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            resolved_at=row["resolved_at"],
        )

    @staticmethod
    def _json_or_none(value: Any) -> str | None:
        if value is None:
            return None
        safe = redact_sensitive(value)
        return _canonical_json(safe)

    @staticmethod
    def _append_event(
        conn: sqlite3.Connection, operation_id: str,
        from_status: OperationStatus | None, to_status: OperationStatus,
        event_type: str, details: Any, created_at: str,
    ) -> None:
        conn.execute(
            """INSERT INTO remote_operation_events
               (operation_id,from_status,to_status,event_type,details_json,created_at)
               VALUES (?,?,?,?,?,?)""",
            (operation_id, from_status.value if from_status else None,
             to_status.value, event_type,
             _canonical_json(redact_sensitive(details)), created_at),
        )


class _Transaction:
    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock):
        self._connection = connection
        self._lock = lock

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        if self._connection.in_transaction:
            self._lock.release()
            raise RepositoryStateError(
                "注入的 SQLite 连接已有活动事务；操作日志需要独立提交边界"
            )
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except BaseException:
            self._lock.release()
            raise
        return self._connection

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        except BaseException:
            try:
                self._connection.rollback()
            except BaseException:
                pass
            raise
        finally:
            self._lock.release()
        return False


class RemoteOperationCoordinator:
    """Enforce write-ahead dispatch and fail-closed outcome handling."""

    def __init__(self, repository: RemoteOperationRepository):
        self._repository = repository

    def prepare(self, intent: OperationIntent) -> RemoteOperation:
        return self._repository.prepare(intent)

    def execute(
        self,
        operation_id: str,
        sender: Callable[[RemoteOperation], Any],
        *,
        retry: bool = False,
        validate_retry: Callable[[RemoteOperation], bool] | None = None,
        is_definite_rejection: Callable[[Exception], bool] | None = None,
        summarize_response: Callable[[Any], Any] | None = None,
        local_commit: Callable[[sqlite3.Connection, Any], Any] | None = None,
    ) -> Any:
        """Send one write only after DISPATCHING has committed.

        All sender exceptions become a durable ``NEEDS_RECONCILIATION`` by
        default. A caller may classify an explicit, known remote rejection;
        transport uncertainty should never satisfy that classifier. If the
        remote call succeeds but local persistence fails, the row remains or is
        marked unresolved so a restart cannot blindly repeat the write.
        """
        if retry:
            if validate_retry is None:
                raise UnsafeRetry("重试前必须重新验证远端前置状态")
            current = self._repository.get(operation_id)
            if current.status is not OperationStatus.NOT_APPLIED:
                raise UnsafeRetry("只有已核对为 NOT_APPLIED 的原操作可以重试")
            try:
                valid_now = bool(validate_retry(current))
            except Exception as exc:
                raise UnsafeRetry(
                    f"重试前状态核验失败：{type(exc).__name__}"
                ) from exc
            if not valid_now:
                raise UnsafeRetry("重试时远端前置状态已变化，禁止发送")
        dispatching = self._repository.begin_dispatch(operation_id, retry=retry)
        try:
            response = sender(dispatching)
        except Exception as exc:
            definite = False
            # Keep the known transport-uncertain exception fail-closed even if
            # a broad caller classifier treats every BiliApiError as a reject.
            if (is_definite_rejection is not None
                    and type(exc).__name__ != "WriteUncertainError"):
                try:
                    definite = bool(is_definite_rejection(exc))
                except Exception:
                    definite = False
            if definite:
                self._repository.mark_rejected(
                    operation_id, error_code=f"remote_rejected:{type(exc).__name__}"
                )
            else:
                self._repository.mark_needs_reconciliation(
                    operation_id, reason_code=f"write_outcome_unknown:{type(exc).__name__}"
                )
            raise

        try:
            summary = summarize_response(response) if summarize_response else None
            self._repository.mark_confirmed(
                operation_id, remote_response=summary,
                local_commit=local_commit, response=response,
            )
        except Exception:
            # A successful remote response followed by a local commit/logging
            # error still has an unresolved remote side effect. If the primary
            # DB failure prevents this update too, DISPATCHING is itself safely
            # recovered to NEEDS_RECONCILIATION at next startup.
            try:
                self._repository.mark_needs_reconciliation(
                    operation_id, reason_code="confirmation_persist_failed"
                )
            except Exception:
                pass
            raise
        return response


__all__ = [
    "ConflictingOperation", "InvalidOperationTransition", "OperationEvent",
    "OperationIdentityConflict", "OperationIntent", "OperationNotFound",
    "OperationStatus", "RemoteOperation", "RemoteOperationCoordinator",
    "RemoteOperationError", "RemoteOperationRepository", "RepositoryStateError",
    "SCHEMA_VERSION", "SqliteRemoteOperationRepository", "UnsafeRetry",
    "redact_sensitive", "stable_operation_id",
]
