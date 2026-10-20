"""Cross-process shared/exclusive locks for a database profile.

The lock file is only a stable rendezvous point for kernel locks. Its presence
does not indicate ownership, so process crashes cannot leave a stale lock.
"""
from __future__ import annotations

import errno
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class ProfileLockError(RuntimeError):
    """The profile lock could not be acquired or released safely."""


class ProfileLockTimeoutError(TimeoutError, ProfileLockError):
    """A shared or exclusive profile lock could not be acquired in time."""


class ProfileLockUpgradeError(ProfileLockError):
    """A thread attempted to upgrade a shared profile lock to exclusive."""


class _LocalRWState:
    """Coordinate threads before they enter the cross-process kernel lock."""

    def __init__(self) -> None:
        self.condition = threading.Condition(threading.RLock())
        self.readers = 0
        self.readers_by_thread: dict[int, int] = {}
        self.waiting_writers = 0
        self.writer: int | None = None
        self.writer_depth = 0
        self.writer_os_lock: object | None = None

    @staticmethod
    def _remaining(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        return deadline - time.monotonic()

    def acquire_shared(self, deadline: float | None) -> bool:
        """Return True when this is a borrowed read under this thread's writer."""
        thread_id = threading.get_ident()
        with self.condition:
            if self.writer == thread_id:
                return True
            while self.writer is not None or (
                    self.waiting_writers and
                    not self.readers_by_thread.get(thread_id, 0)):
                remaining = self._remaining(deadline)
                if remaining is not None and remaining <= 0:
                    raise ProfileLockTimeoutError("等待 profile 共享锁超时")
                self.condition.wait(remaining)
            self.readers += 1
            self.readers_by_thread[thread_id] = (
                self.readers_by_thread.get(thread_id, 0) + 1)
            return False

    def release_shared(self, thread_id: int) -> None:
        with self.condition:
            count = self.readers_by_thread.get(thread_id, 0)
            if count <= 0:
                raise ProfileLockError("profile 共享锁释放次数不匹配")
            if count == 1:
                del self.readers_by_thread[thread_id]
            else:
                self.readers_by_thread[thread_id] = count - 1
            self.readers -= 1
            if self.readers == 0:
                self.condition.notify_all()

    def acquire_exclusive(self, deadline: float | None) -> bool:
        """Acquire local writer state; return True for the outermost acquisition."""
        thread_id = threading.get_ident()
        with self.condition:
            if self.writer == thread_id:
                self.writer_depth += 1
                return False
            if self.readers_by_thread.get(thread_id, 0):
                raise ProfileLockUpgradeError(
                    "持有 profile 共享锁的线程不能直接升级为维护排他锁")

            self.waiting_writers += 1
            try:
                while self.writer is not None or self.readers:
                    remaining = self._remaining(deadline)
                    if remaining is not None and remaining <= 0:
                        raise ProfileLockTimeoutError("等待本进程 profile 租约排空超时")
                    self.condition.wait(remaining)
                self.writer = thread_id
                self.writer_depth = 1
                return True
            finally:
                self.waiting_writers -= 1
                self.condition.notify_all()

    def set_writer_os_lock(self, thread_id: int, os_lock: object) -> None:
        with self.condition:
            if self.writer != thread_id or self.writer_depth < 1:
                raise ProfileLockError("profile 排他锁本地状态已丢失")
            self.writer_os_lock = os_lock

    def abort_exclusive(self, thread_id: int) -> None:
        with self.condition:
            if self.writer == thread_id:
                self.writer = None
                self.writer_depth = 0
                self.writer_os_lock = None
                self.condition.notify_all()

    def release_exclusive(self, thread_id: int) -> None:
        with self.condition:
            if self.writer != thread_id or self.writer_depth <= 0:
                raise ProfileLockError("profile 排他锁释放次数不匹配")
            self.writer_depth -= 1
            if self.writer_depth:
                return
            os_lock = self.writer_os_lock
            self.writer_os_lock = None
            try:
                _release_os_lock(os_lock)
            finally:
                self.writer = None
                self.condition.notify_all()


_REGISTRY_LOCK = threading.Lock()
_LOCAL_STATES: dict[str, _LocalRWState] = {}


def _local_state(path: Path) -> _LocalRWState:
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _REGISTRY_LOCK:
        return _LOCAL_STATES.setdefault(key, _LocalRWState())


class _PosixOSLock:
    def __init__(self, fd: int) -> None:
        self.fd = fd


class _WindowsOSLock:
    def __init__(self, handle, overlapped) -> None:
        self.handle = handle
        self.overlapped = overlapped


def _open_os_lock_file(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        return _windows_open(path)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return os.open(path, flags, 0o600)


def _acquire_os_lock(path: Path, *, exclusive: bool,
                     deadline: float | None):
    if os.name == "nt":
        return _windows_acquire(path, exclusive=exclusive, deadline=deadline)

    import fcntl

    fd = _open_os_lock_file(path)
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        if deadline is None:
            fcntl.flock(fd, operation)
        else:
            while True:
                try:
                    fcntl.flock(fd, operation | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN,
                                         getattr(errno, "EWOULDBLOCK", errno.EAGAIN)):
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ProfileLockTimeoutError(
                            "等待 profile 排他锁超时" if exclusive
                            else "等待 profile 共享锁超时") from exc
                    time.sleep(min(0.01, remaining))
        return _PosixOSLock(fd)
    except Exception:
        os.close(fd)
        raise


def _windows_open(path: Path):
    import ctypes
    from ctypes import wintypes

    # The Windows acquisition path calls this function directly, bypassing
    # _open_os_lock_file. CreateFileW creates a file, not its parent directories.
    path.parent.mkdir(parents=True, exist_ok=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                            wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = create_file(os.fspath(path), 0x80000000 | 0x40000000,
                         0x00000001 | 0x00000002 | 0x00000004,
                         None, 4, 0x00000080, None)
    invalid_handle = wintypes.HANDLE(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        raise ctypes.WinError(error, f"无法打开 profile 锁文件：{path}")
    return kernel32, handle


def _windows_acquire(path: Path, *, exclusive: bool,
                     deadline: float | None):
    import ctypes
    from ctypes import wintypes

    kernel32, handle = _windows_open(path)

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p),
                    ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wintypes.DWORD),
                    ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    lock_file = kernel32.LockFileEx
    lock_file.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                          wintypes.DWORD, wintypes.DWORD,
                          ctypes.POINTER(OVERLAPPED)]
    lock_file.restype = wintypes.BOOL
    overlapped = OVERLAPPED()
    flags = 0x00000002 if exclusive else 0
    if deadline is not None:
        flags |= 0x00000001  # LOCKFILE_FAIL_IMMEDIATELY
    try:
        while True:
            if lock_file(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
                return _WindowsOSLock(handle, overlapped)
            error = ctypes.get_last_error()
            if deadline is None or error != 33:  # ERROR_LOCK_VIOLATION
                raise ctypes.WinError(error, "LockFileEx 无法获取 profile 锁")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProfileLockTimeoutError(
                    "等待 profile 排他锁超时" if exclusive
                    else "等待 profile 共享锁超时")
            time.sleep(min(0.01, remaining))
    except Exception:
        kernel32.CloseHandle(handle)
        raise


def _release_os_lock(os_lock: object | None) -> None:
    if os_lock is None:
        return
    if isinstance(os_lock, _PosixOSLock):
        import fcntl

        try:
            fcntl.flock(os_lock.fd, fcntl.LOCK_UN)
        finally:
            os.close(os_lock.fd)
        return
    if isinstance(os_lock, _WindowsOSLock):
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        unlock = kernel32.UnlockFileEx
        unlock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                           wintypes.DWORD, ctypes.c_void_p]
        unlock.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        try:
            if not unlock(os_lock.handle, 0, 1, 0,
                          ctypes.byref(os_lock.overlapped)):
                error = ctypes.get_last_error()
                raise ctypes.WinError(error, "UnlockFileEx 无法释放 profile 锁")
        finally:
            close_handle(os_lock.handle)


class ProfileLockLease:
    """An idempotent shared or exclusive profile lock lease."""

    def __init__(self, state: _LocalRWState, *, kind: str, owner_thread: int,
                 os_lock: object | None = None, borrowed: bool = False) -> None:
        self._state = state
        self._kind = kind
        self._owner_thread = owner_thread
        self._os_lock = os_lock
        self._borrowed = borrowed
        self._closed = False
        self._close_lock = threading.Lock()

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        if self._kind == "shared":
            if self._borrowed:
                return
            if self._os_lock is not None:
                try:
                    _release_os_lock(self._os_lock)
                finally:
                    self._state.release_shared(self._owner_thread)
        elif self._kind == "exclusive":
            self._state.release_exclusive(self._owner_thread)

    def __enter__(self) -> "ProfileLockLease":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class ProfileFileLock:
    """Shared connection leases and reentrant maintenance exclusivity.

    Use one lock path for every component that mutates or snapshots a profile.
    A normal database connection holds ``acquire_shared()`` until it closes.
    Maintenance code uses ``maintenance()`` around its full operation.
    """

    def __init__(self, lock_path: str | Path) -> None:
        self.path = Path(os.path.abspath(os.fspath(lock_path)))
        self._state = _local_state(self.path)

    def acquire_shared(self, timeout: float | None = None) -> ProfileLockLease:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        thread_id = threading.get_ident()
        borrowed = self._state.acquire_shared(deadline)
        if borrowed:
            return ProfileLockLease(self._state, kind="shared",
                                    owner_thread=thread_id, borrowed=True)
        try:
            os_lock = _acquire_os_lock(self.path, exclusive=False, deadline=deadline)
            return ProfileLockLease(self._state, kind="shared",
                                    owner_thread=thread_id, os_lock=os_lock)
        except Exception:
            self._state.release_shared(thread_id)
            raise

    def acquire_exclusive(self, timeout: float | None = None) -> ProfileLockLease:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        thread_id = threading.get_ident()
        outermost = self._state.acquire_exclusive(deadline)
        if not outermost:
            return ProfileLockLease(self._state, kind="exclusive",
                                    owner_thread=thread_id)
        os_lock = None
        try:
            os_lock = _acquire_os_lock(self.path, exclusive=True, deadline=deadline)
            self._state.set_writer_os_lock(thread_id, os_lock)
            return ProfileLockLease(self._state, kind="exclusive",
                                    owner_thread=thread_id)
        except Exception:
            try:
                if os_lock is not None:
                    _release_os_lock(os_lock)
            finally:
                self._state.abort_exclusive(thread_id)
            raise

    @contextmanager
    def maintenance(self, timeout: float | None = None) -> Iterator[None]:
        """Hold a reentrant exclusive profile lock for a maintenance operation."""
        lease = self.acquire_exclusive(timeout=timeout)
        try:
            yield
        finally:
            lease.close()
