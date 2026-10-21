"""Exercise the Electron external-link allowlist with Node's built-in test runner."""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ElectronExternalLinkTests(unittest.TestCase):
    def test_project_release_urls_are_the_only_github_external_pages_allowed(self):
        node = shutil.which("node")
        if not node:
            self.fail("Node.js is required to exercise the Electron URL policy")
        result = subprocess.run(
            [node, "--test", str(REPOSITORY_ROOT / "tests" / "electron_external_links.test.js")],
            cwd=REPOSITORY_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout)


if __name__ == "__main__":
    unittest.main()
