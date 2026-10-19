"""SQLite file lifecycle, connection gating, and verified candidate replacement.

This module deliberately keeps the first implementation small and local. It does
not own schema migrations or vault encryption; it gives those layers a safe
connection primitive and a candidate validation/switch operation.
"""
from __future__ import annotations

import enum
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from core.database.profile_lock import ProfileFileLock, ProfileLockLease


class DatabaseState(str, enum.Enum):
    UNINITIALIZED = "UNINITIALIZED"
    OPENING = "OPENING"
    READY = "READY"
    DRAINING = "DRAINING"
    SEALING = "SEALING"
    LOCKED = "LOCKED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    FATAL = "FATAL"


class DatabaseLifecycleError(RuntimeError):
    """Base error for a denied or failed database lifecycle operation."""


class DatabaseNotInitializedError(DatabaseLifecycleError):
    """No database exists and an explicit first-time initialization is needed."""


class DatabaseRecoveryRequiredError(DatabaseLifecycleError):
    """Existing identity/data evidence exists, but the active database is absent."""


class DatabaseAlreadyInitializedError(DatabaseLifecycleError):
    """An explicit new-database request would risk replacing existing data."""


class ActiveConnectionsError(DatabaseLifecycleError):
    """A maintenance replacement was requested while connections remain open."""


class IntegrityValidationError(DatabaseLifecycleError):
    """A candidate is not a readable, structurally valid SQLite database."""


@dataclass(frozen=True)
class DatabaseValidation:
    path: str
    integrity_check: tuple[str, ...]
    foreign_key_violations: tuple[tuple, ...]
    schema_version: int | None


@dataclass(frozen=True)
class CandidateInstallResult:
    active_path: str
    backup_path: str | None
    validation: DatabaseValidation


_LONG_PATH_PREFIX = "\\\\?\\"
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _remove_sqlite_sidecars(path: Path) -> None:
    """Remove transient SQLite files associated with an already-closed DB."""
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        Path(str(path) + suffix).unlink(missing_ok=True)


def sqlite_uri(path: Path, mode: str) -> str:
    r"""Build a correctly escaped SQLite URI without relying on cwd semantics.

    ``Path.as_uri()`` renders an extended-length path (Windows ``\\?\`` prefix)
    as ``file://%3F/...``, which SQLite rejects with
    ``invalid uri authority: %3F``. ``resolve()`` can hand back that prefix for a
    long path and, on Windows, for a path that disappears while
    ``ntpath.realpath`` is walking it. Using ``abspath`` avoids the filesystem
    lookup entirely, and the prefix is stripped defensively.
    """
    resolved = os.path.abspath(os.fspath(path))
    if resolved.startswith(_LONG_PATH_PREFIX):
        rest = resolved[len(_LONG_PATH_PREFIX):]
        # \\?\UNC\server\share -> \\server\share, \\?\C:\dir -> C:\dir
        resolved = ("\\\\" + rest[4:]) if rest.upper().startswith("UNC\\") else rest
    return f"{Path(resolved).as_uri()}?mode={mode}"


def _readonly_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(sqlite_uri(path, "ro"), uri=True, timeout=30)


def validate_database(path: str | Path) -> DatabaseValidation:
    """Run full SQLite and FK checks without creating or modifying ``path``."""
    db_path = Path(path)
    if not db_path.is_file():
        raise IntegrityValidationError(f"候选数据库不存在：{db_path}")
    connection = None
    try:
        connection = _readonly_connection(db_path)
        integrity = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check"))
        if integrity != ("ok",):
            raise IntegrityValidationError(
                f"SQLite integrity_check 未通过：{'; '.join(integrity) or '无结果'}")
        foreign = tuple(tuple(row) for row in connection.execute("PRAGMA foreign_key_check"))
        if foreign:
            raise IntegrityValidationError(
                f"SQLite foreign_key_check 发现 {len(foreign)} 项违反")

        schema_version = None
        tables = {str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "schema_migrations" in tables:
            versions = [row[0] for row in connection.execute(
                "SELECT version FROM schema_migrations")]
            if any(type(version) is not int for version in versions):
                raise IntegrityValidationError(
                    "schema_migrations 存在非整数版本值，拒绝验证候选数据库")
            if versions:
                schema_version = max(versions)
        return DatabaseValidation(str(db_path), integrity, foreign, schema_version)
    except IntegrityValidationError:
        raise
    except sqlite3.DatabaseError as exc:
        raise IntegrityValidationError(f"候选数据库无法验证：{exc}") from exc
    finally:
        if connection is not None:
            connection.close()


class _TrackedConnection(sqlite3.Connection):
    """Connection subclass that releases its lifecycle lease exactly once."""

    def __init__(self, *args, lifecycle=None, lease_id=None, **kwargs):
        # sqlite3.connect passes its ``factory`` argument back to the factory
        # instance constructor; sqlite3.Connection.__init__ does not accept it.
        kwargs.pop("factory", None)
        super().__init__(*args, **kwargs)
        self._lifecycle = lifecycle
        self._lease_id = lease_id
        self._lease_released = False

    def close(self):
        try:
            return super().close()
        finally:
            self._release_lease()

    def execute(self, *args, **kwargs):
        cursor = super().execute(*args, **kwargs)
        self._refresh_transaction()
        return cursor

    def executemany(self, *args, **kwargs):
        cursor = super().executemany(*args, **kwargs)
        self._refresh_transaction()
        return cursor

    def executescript(self, *args, **kwargs):
        cursor = super().executescript(*args, **kwargs)
        self._refresh_transaction()
        return cursor

    def commit(self):
        try:
            return super().commit()
        finally:
            self._refresh_transaction()

    def rollback(self):
        try:
            return super().rollback()
        finally:
            self._refresh_transaction()

    def _refresh_transaction(self):
        if self._lifecycle is not None:
            self._lifecycle._mark_transaction(self._lease_id, self.in_transaction)

    def _release_lease(self):
        if not self._lease_released and self._lifecycle is not None:
            self._lease_released = True
            self._lifecycle._release(self._lease_id)

    def __del__(self):  # pragma: no cover - fallback for callers that forget close()
        try:
            self.close()
        except Exception:
            try:
                self._release_lease()
            except Exception:
                pass


class DatabaseLifecycle:
    """Per-database access gate and guarded candidate installer.

    Normal connections are available only in READY and use SQLite ``mode=ro`` or
    ``mode=rw``. The explicit creation path is intentionally separate and uses
    ``mode=rwc`` only for a caller that has already passed first-install checks.
    """

    def __init__(self, path: str | Path, *, evidence_paths: Iterable[str | Path] = (),
                 profile_lock_path: str | Path | None = None):
        self.path = Path(path)
        self.evidence_paths = tuple(Path(item) for item in evidence_paths)
        self.profile_lock_path = (Path(profile_lock_path) if profile_lock_path is not None
                                  else self.path.parent / ".bfo-profile.lock")
        self._profile_lock = ProfileFileLock(self.profile_lock_path)
        self._condition = threading.Condition(threading.RLock())
        self._state = DatabaseState.UNINITIALIZED
        self._leases: dict[str, dict] = {}

    @property
    def state(self) -> DatabaseState:
        with self._condition:
            return self._state

    @property
    def active_connections(self) -> tuple[dict, ...]:
        with self._condition:
            return tuple(dict(value) for value in self._leases.values())

    @property
    def active_connection_count(self) -> int:
        with self._condition:
            return len(self._leases)

    @property
    def active_read_transactions(self) -> int:
        with self._condition:
            return sum(value["mode"] == "ro" and value["in_transaction"]
                       for value in self._leases.values())

    @property
    def active_write_transactions(self) -> int:
        with self._condition:
            return sum(value["mode"] == "rw" and value["in_transaction"]
                       for value in self._leases.values())

    def set_state(self, state: DatabaseState | str) -> None:
        with self._condition:
            self._state = DatabaseState(state)
            self._condition.notify_all()

    def has_initialization_evidence(self) -> bool:
        if self.path.exists():
            return True
        return any(path.exists() for path in self.evidence_paths)

    def classify_missing_database(self) -> DatabaseState:
        state = (DatabaseState.RECOVERY_REQUIRED if self.has_initialization_evidence()
                 else DatabaseState.UNINITIALIZED)
        self.set_state(state)
        return state

    def _missing_error(self) -> DatabaseLifecycleError:
        if self._state == DatabaseState.LOCKED:
            return DatabaseLifecycleError(
                f"数据库当前处于锁定状态，必须通过保险库验证并恢复：{self.path}")
        if self._state in (DatabaseState.OPENING, DatabaseState.DRAINING,
                           DatabaseState.SEALING):
            return DatabaseLifecycleError(
                f"数据库当前处于生命周期维护状态（{self._state.value}）：{self.path}")
        if self._state == DatabaseState.READY:
            self.set_state(DatabaseState.RECOVERY_REQUIRED)
            return DatabaseRecoveryRequiredError(
                f"已开放的活动数据库意外缺失：{self.path}；请进入恢复流程")
        if self._state in (DatabaseState.RECOVERY_REQUIRED, DatabaseState.FATAL):
            return DatabaseRecoveryRequiredError(
                f"数据库处于 {self._state.value} 状态，普通访问已关闭：{self.path}")
        if self.has_initialization_evidence():
            self.set_state(DatabaseState.RECOVERY_REQUIRED)
            return DatabaseRecoveryRequiredError(
                f"已检测到既有数据或保险库证据，但活动数据库缺失：{self.path}；请进入恢复流程")
        self.set_state(DatabaseState.UNINITIALIZED)
        return DatabaseNotInitializedError(
            f"数据库尚未初始化：{self.path}；需要显式执行首次初始化")

    def connect(self, *, mode: str = "rw", owner: str = "store",
                profile_lock_timeout: float | None = None) -> sqlite3.Connection:
        """Open a normal connection that is incapable of creating a database."""
        if mode not in ("ro", "rw"):
            raise ValueError("普通数据库连接 mode 只能是 'ro' 或 'rw'")
        profile_lease = self._profile_lock.acquire_shared(timeout=profile_lock_timeout)
        lease_id = uuid.uuid4().hex
        try:
            with self._condition:
                if self._state != DatabaseState.READY:
                    if not self.path.is_file():
                        raise self._missing_error()
                    raise DatabaseLifecycleError(
                        f"数据库当前不允许普通访问（状态：{self._state.value}）")
                if not self.path.is_file():
                    raise self._missing_error()
                self._leases[lease_id] = {
                    "owner": str(owner), "mode": mode, "opened_at": time.time(),
                    "in_transaction": False, "profile_lease": profile_lease,
                }
                try:
                    return sqlite3.connect(
                        sqlite_uri(self.path, mode), uri=True, timeout=30,
                        factory=lambda *args, **kwargs: _TrackedConnection(
                            *args, lifecycle=self, lease_id=lease_id, **kwargs))
                except Exception:
                    self._leases.pop(lease_id, None)
                    self._condition.notify_all()
                    raise
        except Exception:
            profile_lease.close()
            raise

    @contextmanager
    def maintenance(self, timeout: float | None = None):
        """Hold this profile's reentrant cross-process maintenance lock.

        Callers must use the same ``profile_lock_path`` for schema snapshots,
        vault lock/unlock, backup/restore and other operations that need one
        stable profile view. Normal connections in every cooperating process
        hold a shared lease until close.
        """
        with self._profile_lock.maintenance(timeout=timeout):
            yield

    def acquire_profile_read_lease(self, timeout: float | None = None):
        """Acquire the shared profile lease without opening a SQLite connection.

        Store-level mutexes use this before taking their in-process lock so a
        maintenance writer never waits on a mutex owner that is blocked on this
        same cross-process gate.
        """
        return self._profile_lock.acquire_shared(timeout=timeout)

    def begin_drain(self, timeout: float | None = None) -> None:
        """Close normal access, then wait for already-open connections to leave.

        The state change and connection count are guarded by the same condition
        lock used by :meth:`connect`, so no new lease can slip in after the
        drain begins. On timeout, restore READY and report the outstanding
        owners; the caller must not proceed with a database snapshot.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            if self._state != DatabaseState.READY:
                raise DatabaseLifecycleError(
                    f"数据库当前不允许开始排空（状态：{self._state.value}）")
            self._state = DatabaseState.DRAINING
            self._condition.notify_all()
            while self._leases:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    owners = ", ".join(
                        str(item.get("owner", "unknown"))
                        for item in list(self._leases.values())[:5]
                    )
                    count = len(self._leases)
                    self._state = DatabaseState.READY
                    self._condition.notify_all()
                    raise ActiveConnectionsError(
                        f"等待数据库连接排空超时：仍有 {count} 个连接未释放"
                        + (f"（{owners}）" if owners else ""))
                self._condition.wait(remaining)

    def connect_for_drain(self, *, owner: str = "maintenance") -> sqlite3.Connection:
        """Open the sole tracked maintenance connection after normal access drains."""
        # Keep the process-wide maintenance lock for the full lifetime of this
        # connection. A caller that already owns maintenance gets a reentrant
        # lease; direct callers still receive cross-process protection.
        profile_lease = self._profile_lock.acquire_exclusive()
        with self._condition:
            try:
                if self._state != DatabaseState.DRAINING:
                    raise DatabaseLifecycleError(
                        f"数据库不处于排空状态（{self._state.value}），不能打开维护连接")
                if self._leases:
                    raise ActiveConnectionsError(
                        f"排空后仍有 {len(self._leases)} 个数据库连接，不能打开维护连接")
                if not self.path.is_file():
                    raise self._missing_error()
                lease_id = uuid.uuid4().hex
                self._leases[lease_id] = {
                    "owner": str(owner), "mode": "rw", "opened_at": time.time(),
                    "in_transaction": False, "profile_lease": profile_lease,
                }
                try:
                    return sqlite3.connect(
                        sqlite_uri(self.path, "rw"), uri=True, timeout=30,
                        factory=lambda *args, **kwargs: _TrackedConnection(
                            *args, lifecycle=self, lease_id=lease_id, **kwargs))
                except Exception:
                    self._leases.pop(lease_id, None)
                    self._condition.notify_all()
                    raise
            except Exception:
                profile_lease.close()
                raise

    def connect_for_initialization(self, path: str | Path, *, create: bool) -> sqlite3.Connection:
        """Internal lifecycle connection used only while OPENING a known candidate."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "rwc" if create else "rw"
        return sqlite3.connect(sqlite_uri(target, mode), uri=True, timeout=30)

    def initialize_new_database(self, initializer, *,
                                allow_existing_evidence: bool = False) -> DatabaseValidation:
        """Create a new DB on a sibling candidate and install it after validation."""
        with self._condition:
            if self._leases:
                raise ActiveConnectionsError("数据库仍有活跃连接，不能开始首次初始化")
        with self.maintenance():
            return self._initialize_new_database_under_maintenance(
                initializer, allow_existing_evidence=allow_existing_evidence)

    def _initialize_new_database_under_maintenance(
            self, initializer, *, allow_existing_evidence: bool) -> DatabaseValidation:
        with self._condition:
            if self.path.exists():
                self.set_state(DatabaseState.READY)
                raise DatabaseAlreadyInitializedError(f"数据库已存在：{self.path}")
            if self.has_initialization_evidence() and not allow_existing_evidence:
                self.set_state(DatabaseState.RECOVERY_REQUIRED)
                raise DatabaseRecoveryRequiredError(
                    f"发现既有数据或保险库证据，拒绝创建空数据库：{self.path}")
            if self._leases:
                raise ActiveConnectionsError("数据库仍有活跃连接，不能开始首次初始化")
            self._state = DatabaseState.OPENING

        self.path.parent.mkdir(parents=True, exist_ok=True)
        candidate = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.initializing")
        try:
            connection = self.connect_for_initialization(candidate, create=True)
            try:
                initializer(connection)
                connection.commit()
                # The initializer may enable WAL for compatibility. Flush all
                # committed pages into the candidate main file before it moves.
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint and int(checkpoint[0]) != 0:
                    raise ActiveConnectionsError("首次初始化候选库的 WAL checkpoint 未完成")
            finally:
                connection.close()
            validation = validate_database(candidate)
            # Recheck for a racing external writer or a second initial-install flow.
            with self._condition:
                if self.path.exists() or (not allow_existing_evidence and
                                          any(p.exists() for p in self.evidence_paths)):
                    raise DatabaseAlreadyInitializedError(
                        "初始化期间出现新的数据库/身份文件，候选库未切换")
                candidate_wal = Path(str(candidate) + "-wal")
                candidate_shm = Path(str(candidate) + "-shm")
                candidate_journal = Path(str(candidate) + "-journal")
                # Checkpoint should have removed live WAL frames. Preserve an
                # unexpected sidecar rather than installing a mismatched DB.
                if any(path.exists() and path.stat().st_size > 0
                       for path in (candidate_wal, candidate_journal)):
                    raise IntegrityValidationError("候选数据库仍有未封存的 SQLite sidecar")
                for path in (candidate_wal, candidate_shm, candidate_journal):
                    path.unlink(missing_ok=True)
                os.replace(candidate, self.path)
                self._state = DatabaseState.READY
                self._condition.notify_all()
            return DatabaseValidation(str(self.path), validation.integrity_check,
                                      validation.foreign_key_violations,
                                      validation.schema_version)
        except Exception:
            with self._condition:
                if self.path.exists():
                    self._state = DatabaseState.RECOVERY_REQUIRED
                else:
                    self._state = (DatabaseState.RECOVERY_REQUIRED
                                   if self.has_initialization_evidence()
                                   else DatabaseState.UNINITIALIZED)
                self._condition.notify_all()
            raise
        finally:
            candidate.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm", "-journal"):
                Path(str(candidate) + suffix).unlink(missing_ok=True)

    def install_candidate(self, candidate_path: str | Path, *,
                          validator: Callable[[str | Path], object] | None = None,
                          maintenance_timeout: float | None = None
                          ) -> CandidateInstallResult:
        """Verify a SQLite candidate, preserve the old DB, then atomically switch.

        All profile readers and writers must use the same profile lock path. The
        gate rejects replacement when this lifecycle still owns open connections;
        the cross-process exclusive lease drains other cooperating processes.
        The prior DB is kept as a verified sibling recovery copy. ``validator``
        lets the schema owner enforce its application-level contract on the
        candidate, staged copy, recovery copy and installed database. SQLite
        integrity checks always run regardless of the callback.
        """
        candidate = Path(candidate_path)
        if candidate.resolve() == self.path.resolve():
            raise ValueError("候选数据库必须与活动数据库路径不同")
        def validate(path: Path) -> DatabaseValidation:
            result = validate_database(path)
            if validator is not None:
                validator(path)
            return result

        with self._condition:
            if self._leases:
                raise ActiveConnectionsError(
                    f"仍有 {len(self._leases)} 个数据库连接，不能切换候选库")
            previous_state = self._state
            self._state = DatabaseState.DRAINING

        try:
            with self.maintenance(timeout=maintenance_timeout):
                return self._install_candidate_under_maintenance(candidate, validate)
        except Exception:
            # Lock acquisition can time out before the replacement body changes
            # state. Restore the exact prior gate state; no replacement happened.
            with self._condition:
                if self._state == DatabaseState.DRAINING:
                    self._state = previous_state
                    self._condition.notify_all()
            raise

    def _install_candidate_under_maintenance(
            self, candidate: Path,
            validate: Callable[[Path], DatabaseValidation]) -> CandidateInstallResult:
        """Install and roll back while the exclusive profile lease is held."""
        validate(candidate)
        staged = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.candidate")
        backup = None
        backup_verified = False
        had_active_database = self.path.is_file()
        candidate_installed = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Copy through SQLite so a candidate WAL, if present, is included.
            src = _readonly_connection(candidate)
            dst = sqlite3.connect(sqlite_uri(staged, "rwc"), uri=True, timeout=30)
            try:
                src.backup(dst)
                dst.commit()
            finally:
                dst.close()
                src.close()
            staged_validation = validate(staged)

            if self.path.is_file():
                backup = self.path.with_name(
                    f"{self.path.name}.recovery-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}")
                old = sqlite3.connect(sqlite_uri(self.path, "rw"), uri=True, timeout=30)
                saved = sqlite3.connect(sqlite_uri(backup, "rwc"), uri=True, timeout=30)
                try:
                    old.backup(saved)
                    saved.commit()
                finally:
                    saved.close()
                    old.close()
                validate(backup)
                # The validated recovery copy may itself retain WAL mode.
                # Checkpoint its committed contents before removing sidecars,
                # otherwise deleting a non-empty WAL would discard recovery
                # data even though the main file passed validation.
                saved = sqlite3.connect(sqlite_uri(backup, "rw"), uri=True, timeout=30)
                try:
                    row = saved.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
                    if row and int(row[0]) != 0:
                        raise ActiveConnectionsError(
                            "WAL checkpoint 未能完成，拒绝保留不完整的恢复副本")
                finally:
                    saved.close()
                _remove_sqlite_sidecars(backup)
                backup_verified = True
                # The backup includes committed WAL contents. Checkpoint before
                # removing sidecars so the still-active source remains recoverable.
                old = sqlite3.connect(sqlite_uri(self.path, "rw"), uri=True, timeout=30)
                try:
                    row = old.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
                    if row and int(row[0]) != 0:
                        raise ActiveConnectionsError("WAL checkpoint 未能完成，拒绝替换数据库")
                finally:
                    old.close()
                for suffix in _SQLITE_SIDECAR_SUFFIXES:
                    sidecar = Path(str(self.path) + suffix)
                    if sidecar.exists():
                        sidecar.unlink()

            os.replace(staged, self.path)
            candidate_installed = True
            installed_validation = validate(self.path)
            if installed_validation.schema_version != staged_validation.schema_version:
                raise IntegrityValidationError("候选库切换后 Schema 版本与验证结果不符")
            with self._condition:
                self._state = DatabaseState.READY
                self._condition.notify_all()
            return CandidateInstallResult(str(self.path), str(backup) if backup else None,
                                          installed_validation)
        except Exception as original_error:
            # A failed candidate must not take the last known good DB with it.
            rollback_ok = False
            if backup_verified and backup and backup.is_file():
                failed = None
                restore = None
                try:
                    active_sidecars = [Path(str(self.path) + suffix)
                                       for suffix in _SQLITE_SIDECAR_SUFFIXES]
                    if self.path.exists() or any(path.exists() for path in active_sidecars):
                        failed = self.path.with_name(
                            f"{self.path.name}.failed-candidate-{uuid.uuid4().hex[:8]}")
                        if self.path.exists():
                            os.replace(self.path, failed)
                        # SQLite sidecars belong to the failed active image and
                        # must not be left beside the restored known-good DB.
                        for sidecar in active_sidecars:
                            if sidecar.exists():
                                os.replace(sidecar, Path(str(failed) +
                                                          sidecar.name[len(self.path.name):]))
                    restore = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.restore")
                    shutil.copy2(backup, restore)
                    validate(restore)
                    _remove_sqlite_sidecars(restore)
                    os.replace(restore, self.path)
                    if failed is not None:
                        _remove_sqlite_sidecars(failed)
                        failed.unlink(missing_ok=True)
                    rollback_ok = True
                except Exception as rollback_error:
                    if hasattr(original_error, "add_note"):
                        original_error.add_note(f"活动库回滚也失败：{rollback_error}")
                finally:
                    if restore is not None:
                        try:
                            restore.unlink(missing_ok=True)
                            _remove_sqlite_sidecars(restore)
                        except Exception as cleanup_error:
                            rollback_ok = False
                            if hasattr(original_error, "add_note"):
                                original_error.add_note(
                                    f"回滚候选清理也失败：{cleanup_error}")
            with self._condition:
                if rollback_ok or (had_active_database and not candidate_installed and
                                   self.path.is_file()):
                    self._state = DatabaseState.READY
                else:
                    self._state = DatabaseState.RECOVERY_REQUIRED
                self._condition.notify_all()
            raise
        finally:
            staged.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm", "-journal"):
                Path(str(staged) + suffix).unlink(missing_ok=True)

    def _release(self, lease_id: str | None) -> None:
        if lease_id is None:
            return
        with self._condition:
            lease = self._leases.pop(lease_id, None)
            self._condition.notify_all()
        if lease is not None:
            profile_lease = lease.get("profile_lease")
            if isinstance(profile_lease, ProfileLockLease):
                profile_lease.close()

    def _mark_transaction(self, lease_id: str | None, active: bool) -> None:
        if lease_id is None:
            return
        with self._condition:
            lease = self._leases.get(lease_id)
            if lease is not None:
                lease["in_transaction"] = bool(active)
