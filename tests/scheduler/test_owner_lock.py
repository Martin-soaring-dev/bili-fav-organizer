"""Ownership contract regressions, including Windows mandatory byte locks."""
from __future__ import annotations

import errno
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from core.scheduler import ownership


class FileOwnerLockTests(unittest.TestCase):
    def _windows_backend(self, file_obj, backend):
        return (
            patch.object(ownership, "os", types.SimpleNamespace(name="nt")),
            patch.dict(sys.modules, {"msvcrt": backend}),
            patch.object(Path, "open", return_value=file_obj),
        )

    def test_windows_acquire_never_reads_or_writes_lock_bytes(self):
        file_obj = Mock()
        file_obj.read.side_effect = PermissionError(errno.EACCES, "locked byte")
        file_obj.write.side_effect = AssertionError("lock acquisition must not write")
        backend = types.SimpleNamespace(LK_NBLCK=1, LK_UNLCK=0, locking=Mock())
        with tempfile.TemporaryDirectory() as directory:
            lock = ownership.FileOwnerLock(Path(directory) / ".scheduler-owner.lock")
            platform, module, opened = self._windows_backend(file_obj, backend)
            with platform, module, opened:
                try:
                    lock.acquire()
                    backend.locking.assert_called_once_with(file_obj.fileno(), 1, 1)
                    self.assertIs(lock._file, file_obj)
                    file_obj.read.assert_not_called()
                    file_obj.write.assert_not_called()
                finally:
                    lock.release()
                self.assertEqual(backend.locking.call_args.args, (file_obj.fileno(), 0, 1))
                file_obj.close.assert_called_once()

    def test_windows_contender_reports_ownership_and_closes_handle_without_io(self):
        file_obj = Mock()
        file_obj.read.side_effect = PermissionError(errno.EACCES, "locked byte")
        denied = PermissionError(errno.EACCES, "lock already held")
        backend = types.SimpleNamespace(
            LK_NBLCK=1, LK_UNLCK=0, locking=Mock(side_effect=denied),
        )
        with tempfile.TemporaryDirectory() as directory:
            lock = ownership.FileOwnerLock(Path(directory) / ".scheduler-owner.lock")
            platform, module, opened = self._windows_backend(file_obj, backend)
            with platform, module, opened:
                with self.assertRaisesRegex(RuntimeError, "another scheduler process") as raised:
                    lock.acquire()
                self.assertIs(raised.exception.__cause__, denied)
                self.assertIsNone(lock._file)
                backend.locking.assert_called_once_with(file_obj.fileno(), 1, 1)
                file_obj.read.assert_not_called()
                file_obj.write.assert_not_called()
                file_obj.close.assert_called_once()

    def test_real_process_contention_and_reacquisition_for_empty_and_existing_files(self):
        script = """
import sys
from core.scheduler.ownership import FileOwnerLock
lock = FileOwnerLock(sys.argv[1])
try:
    lock.acquire()
except RuntimeError:
    sys.exit(23)
else:
    lock.release()
"""
        with tempfile.TemporaryDirectory() as directory:
            for initial in (b"", b"\0"):
                with self.subTest(initial=initial):
                    path = Path(directory) / ".scheduler-owner.lock"
                    path.write_bytes(initial)
                    lock = ownership.FileOwnerLock(path)
                    lock.acquire()
                    try:
                        result = subprocess.run(
                            [sys.executable, "-c", script, str(path)],
                            capture_output=True, text=True, timeout=10, check=False,
                        )
                        self.assertEqual(23, result.returncode, result.stdout + result.stderr)
                        self.assertEqual(len(initial), path.stat().st_size)
                    finally:
                        lock.release()
                    result = subprocess.run(
                        [sys.executable, "-c", script, str(path)],
                        capture_output=True, text=True, timeout=10, check=False,
                    )
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertEqual(initial, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
