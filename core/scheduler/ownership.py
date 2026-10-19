"""Local single-process scheduler ownership locks."""

from __future__ import annotations

from pathlib import Path
import os
import threading


class OwnerLock:
    def acquire(self) -> None:
        raise NotImplementedError

    def release(self) -> None:
        raise NotImplementedError


class FileOwnerLock(OwnerLock):
    """Cross-process advisory lock; the lock file contains no application data."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file = None

    def acquire(self) -> None:
        if self._file is not None:
            raise RuntimeError("owner lock is already held")
        if not self.path.parent.is_dir():
            raise FileNotFoundError(f"owner lock directory does not exist: {self.path.parent}")
        file_obj = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                # Windows byte locks may extend past EOF. Lock byte zero
                # directly, even for a new empty file: reading or initializing
                # it first can hit another owner's mandatory byte-range lock.
                file_obj.seek(0)
                try:
                    msvcrt.locking(file_obj.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise RuntimeError("another scheduler process owns this database") from exc
            else:
                import fcntl

                try:
                    fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise RuntimeError("another scheduler process owns this database") from exc
            self._file = file_obj
        except Exception:
            file_obj.close()
            raise

    def release(self) -> None:
        file_obj = self._file
        if file_obj is None:
            return
        self._file = None
        try:
            if os.name == "nt":
                import msvcrt

                file_obj.seek(0)
                msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)
        finally:
            file_obj.close()


class ProcessOwnerLock(OwnerLock):
    """In-process ownership guard for injected stores such as the test store."""

    _guard = threading.Lock()
    _held_keys: set[object] = set()

    def __init__(self, key: object) -> None:
        self.key = key
        self._held = False

    def acquire(self) -> None:
        with self._guard:
            if self.key in self._held_keys:
                raise RuntimeError("another scheduler instance owns this store")
            self._held_keys.add(self.key)
            self._held = True

    def release(self) -> None:
        if not self._held:
            return
        with self._guard:
            self._held_keys.discard(self.key)
            self._held = False
