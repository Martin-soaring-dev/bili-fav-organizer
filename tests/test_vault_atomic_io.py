"""Atomic ciphertext writes must fit the destination's path budget."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from vault import Vault


class VaultAtomicIOTests(unittest.TestCase):
    def test_atomic_write_creates_archive_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "new-profile" / "vault-archives" / "archive.enc"
            Vault._atomic_bytes(dest, b"complete ciphertext")
            self.assertEqual(b"complete ciphertext", dest.read_bytes())
            self.assertEqual([dest], list(dest.parent.iterdir()))

    def test_atomic_archive_temporary_fits_destination_path_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "vault-archives" / ("archive-" + "a" * 64 + ".enc")
            # Model a Windows path near MAX_PATH: the destination fits, but
            # copying its full filename and appending a UUID does not.
            path_budget = len(str(dest)) + 4
            real_open = Path.open

            def bounded_open(path, mode="r", *args, **kwargs):
                if "x" in mode and len(str(path)) > path_budget:
                    raise FileNotFoundError(2, "simulated Windows path limit", str(path))
                return real_open(path, mode, *args, **kwargs)

            with mock.patch.object(Path, "open", bounded_open):
                Vault._atomic_bytes(dest, b"verified archive")
            self.assertEqual(b"verified archive", dest.read_bytes())
            self.assertEqual([dest], list(dest.parent.iterdir()))

    def test_replace_denial_preserves_old_ciphertext_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "archive.enc"
            dest.write_bytes(b"old verified ciphertext")
            with mock.patch("vault.os.replace", side_effect=PermissionError(
                    13, "synthetic replace denial", str(dest))):
                with self.assertRaises(PermissionError):
                    Vault._atomic_bytes(dest, b"new candidate")
            self.assertEqual(b"old verified ciphertext", dest.read_bytes())
            self.assertEqual([dest], list(dest.parent.iterdir()))


if __name__ == "__main__":
    unittest.main()
