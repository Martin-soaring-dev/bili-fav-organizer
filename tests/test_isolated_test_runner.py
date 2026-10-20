"""Safety contracts for the sanitized test checkout."""
from __future__ import annotations

import unittest

from tools.run_isolated_tests import ignore


class IsolatedTestRunnerCopyFilterTests(unittest.TestCase):
    def test_user_recovery_and_diagnostic_directories_are_not_copied(self):
        protected = {
            ".codex-recovery",
            ".bfo-diag-temp",
            ".candidate-test-tmp",
            "Test-BFO-Beta2",
        }

        self.assertEqual(protected, ignore("/repo", sorted(protected)))


if __name__ == "__main__":
    unittest.main()
