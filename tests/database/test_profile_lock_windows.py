"""Exercise the Windows backend boundary without replacing its file-open logic.

Kernel calls are mocked here; Windows lifecycle/multiprocess tests remain the
acceptance evidence for real LockFileEx behavior.
"""
from __future__ import annotations

import ctypes
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.database import profile_lock


class WindowsProfileLockTests(unittest.TestCase):
    def test_windows_backend_creates_missing_profile_parents_before_createfile(self):
        for exclusive in (False, True):
            with self.subTest(exclusive=exclusive), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "new-profile" / "data" / ".bfo-profile.lock"
                kernel = mock.MagicMock()

                def create_file(filename, access, sharing, security, creation, flags, template):
                    self.assertTrue(path.parent.is_dir(),
                                    "CreateFileW cannot create missing parent directories")
                    self.assertEqual(os.fspath(path), filename)
                    self.assertEqual(4, creation)  # OPEN_ALWAYS
                    path.touch()
                    return 123

                kernel.CreateFileW.side_effect = create_file
                kernel.LockFileEx.return_value = True
                kernel.UnlockFileEx.return_value = True
                with mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
                        mock.patch.object(profile_lock, "os", SimpleNamespace(
                            name="nt", fspath=os.fspath)):
                    lease = profile_lock._acquire_os_lock(
                        path, exclusive=exclusive, deadline=time.monotonic() + 1)
                    profile_lock._release_os_lock(lease)

                self.assertTrue(path.is_file())
                kernel.LockFileEx.assert_called_once()
                lock_flags = kernel.LockFileEx.call_args.args[1]
                self.assertEqual(3 if exclusive else 1, lock_flags)
                kernel.CloseHandle.assert_called_once_with(123)

    def test_windows_open_preserves_native_error_code(self):
        from ctypes import wintypes

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".bfo-profile.lock"
            kernel = mock.MagicMock()
            kernel.CreateFileW.return_value = wintypes.HANDLE(-1).value
            native_error = OSError("synthetic Windows access denied")
            native_error.winerror = 5
            with mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
                    mock.patch.object(ctypes, "get_last_error", return_value=5, create=True), \
                    mock.patch.object(ctypes, "WinError", return_value=native_error,
                                      create=True) as win_error:
                with self.assertRaises(OSError) as caught:
                    profile_lock._windows_open(path)
            self.assertIs(native_error, caught.exception)
            self.assertEqual(5, caught.exception.winerror)
            self.assertEqual(5, win_error.call_args.args[0])
            kernel.CloseHandle.assert_not_called()

    def test_windows_open_rejects_a_file_in_place_of_profile_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "not-a-directory"
            parent.write_bytes(b"preserve this file")
            kernel = mock.MagicMock()
            with mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True):
                with self.assertRaises(FileExistsError):
                    profile_lock._windows_open(parent / ".bfo-profile.lock")
            self.assertEqual(b"preserve this file", parent.read_bytes())
            kernel.CreateFileW.assert_not_called()

    def test_windows_acquire_failure_preserves_error_and_closes_handle(self):
        kernel = mock.MagicMock()
        kernel.CreateFileW.return_value = 123
        kernel.LockFileEx.return_value = False
        native_error = OSError("synthetic Windows lock failure")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
                mock.patch.object(ctypes, "get_last_error", return_value=5, create=True), \
                mock.patch.object(ctypes, "WinError", return_value=native_error, create=True):
            with self.assertRaises(OSError) as caught:
                profile_lock._windows_acquire(
                    Path(tmp) / "profile" / ".bfo-profile.lock", exclusive=True,
                    deadline=time.monotonic() + 1)
        self.assertIs(native_error, caught.exception)
        kernel.CloseHandle.assert_called_once_with(123)

    def test_windows_unlock_failure_preserves_error_and_closes_handle(self):
        kernel = mock.MagicMock()
        kernel.UnlockFileEx.return_value = False
        native_error = OSError("synthetic Windows unlock failure")
        lease = profile_lock._WindowsOSLock(123, ctypes.c_int())
        with mock.patch.object(ctypes, "WinDLL", return_value=kernel, create=True), \
                mock.patch.object(ctypes, "get_last_error", return_value=6, create=True), \
                mock.patch.object(ctypes, "WinError", return_value=native_error, create=True):
            with self.assertRaises(OSError) as caught:
                profile_lock._release_os_lock(lease)
        self.assertIs(native_error, caught.exception)
        kernel.CloseHandle.assert_called_once_with(123)


if __name__ == "__main__":
    unittest.main()
