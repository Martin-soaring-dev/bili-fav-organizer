from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ReleaseWorkflowContractTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")

    def test_release_build_workflows_are_tag_triggered_and_publish_after_tests(self):
        expected_tags = {
            "release-stable.yml": 'tags: ["v*"]',
            "release-beta.yml": 'tags: ["v*-beta.*"]',
        }
        for name, tag_filter in expected_tags.items():
            with self.subTest(workflow=name):
                text = self.read(name)
                self.assertIn("on:\n  push:", text)
                self.assertIn(tag_filter, text)
                self.assertNotIn("workflow_dispatch:", text)
                self.assertIn("contents: read", text)
                self.assertIn("contents: write", text)
                self.assertIn("RELEASE_VERSION: ${{ github.ref_name }}", text)
                self.assertIn("persist-credentials: false", text)
                self.assertIn("gh release create $tag @assets --verify-tag", text)
                self.assertNotIn("inputs.tag", text)
                self.assertNotIn("create-manifest", text)
                self.assertNotIn("actions/upload-artifact@", text)
                tests = text.index("- name: Run tests")
                python_build = text.index("- name: Build self-contained application")
                installer = text.index("- name: Build installer (Inno Setup)")
                electron = text.index("- name: Build Electron neo")
                publish = text.index("- name: Publish GitHub Release after all tests and packages pass")
                self.assertLess(tests, python_build)
                self.assertLess(python_build, installer)
                self.assertLess(installer, electron)
                self.assertLess(electron, publish)

    def test_publisher_is_environment_gated_and_has_scoped_write_permission(self):
        text = self.read("publish-release.yml")
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("environment: release-approval", text)
        self.assertIn("github.ref == 'refs/heads/main'", text)
        self.assertIn("contents: write", text)
        self.assertIn("admin", text)
        self.assertIn("maintain", text)
        self.assertIn("$env:GITHUB_ACTOR, $env:GITHUB_TRIGGERING_ACTOR", text)
        self.assertIn("foreach ($actor in $actors)", text)
        self.assertIn("for the original and triggering actors", text)
        self.assertIn("actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093", text)
        self.assertIn("verify-manifest", text)
        self.assertIn("--verify-tag", text)
        self.assertIn("gh release create", text)
        job_header = text.split("    steps:", 1)[0]
        self.assertNotIn("GH_TOKEN:", job_header)
        self.assertEqual(text.count("GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}"), 3)
        self.assertEqual(text.count("contents: write"), 1)
        publish_job = text.split("  publish:\n", 1)[1]
        self.assertEqual(publish_job.count("uses:"), 1)
        self.assertIn("uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683", publish_job)
        self.assertIn("persist-credentials: false", publish_job)

    def test_publisher_write_token_is_scoped_to_trusted_shell_steps(self):
        text = self.read("publish-release.yml")
        verification_job = text.split("  verify-candidate:\n", 1)[1].split("\n  publish:\n", 1)[0]
        publish_job = text.split("  publish:\n", 1)[1]
        top_level_permissions = text.split("jobs:", 1)[0]
        self.assertIn("permissions:\n  contents: read\n  actions: read", top_level_permissions)
        self.assertNotIn("contents: write", top_level_permissions)
        self.assertIn("permissions:\n      contents: read\n      actions: read", verification_job)
        self.assertIn("permissions:\n      contents: write\n      actions: read", publish_job)
        self.assertEqual(publish_job.count("uses:"), 1)
        self.assertIn("persist-credentials: false", publish_job)
        token_index = publish_job.index("GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}")
        self.assertEqual(publish_job.count("GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}"), 1)
        step_start = publish_job.rfind("      - name:", 0, token_index)
        step_end = publish_job.find("\n      - name:", token_index)
        token_step = publish_job[step_start:] if step_end < 0 else publish_job[step_start:step_end]
        self.assertIn("shell: pwsh", token_step)
        self.assertIn("run: |", token_step)
        self.assertIn("gh ", token_step)

    def test_pull_request_workflows_use_read_only_tokens_and_pinned_actions(self):
        expected = {
            "actions/checkout": "11bd71901bbe5b1630ceea73d27597364c9af683",
            "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
            "actions/setup-node": "49933ea5288caeca8642d1e84afbd3f7d6820020",
        }
        for name in ("ci.yml", "ci-beta-package.yml"):
            with self.subTest(workflow=name):
                text = self.read(name)
                top_level = text.split("jobs:", 1)[0]
                self.assertIn("permissions:\n  contents: read", top_level)
                self.assertNotIn("contents: write", top_level)
                action_lines = [line for line in text.splitlines()
                                if re.match(r"^\s*(?:-\s*)?uses:\s+actions/", line)]
                self.assertTrue(action_lines)
                for line in action_lines:
                    action_match = re.match(
                        r"^\s*(?:-\s*)?uses:\s+(actions/(?:checkout|setup-python|setup-node))@([0-9a-f]{40})",
                        line,
                    )
                    self.assertIsNotNone(action_match, line)
                    action, sha = action_match.groups()
                    self.assertEqual(expected[action], sha, line)

    def test_release_workflows_pin_actions_to_verified_commit_shas(self):
        expected = {
            "actions/checkout": "11bd71901bbe5b1630ceea73d27597364c9af683",
            "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
            "actions/setup-node": "49933ea5288caeca8642d1e84afbd3f7d6820020",
        }
        publisher_expected = {
            "actions/checkout": expected["actions/checkout"],
            "actions/setup-python": expected["actions/setup-python"],
            "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
        }
        for workflow in ("release-beta.yml", "release-stable.yml"):
            text = self.read(workflow)
            for action, sha in expected.items():
                with self.subTest(workflow=workflow, action=action):
                    self.assertIn(f"uses: {action}@{sha}", text)
        publisher = self.read("publish-release.yml")
        for action, sha in publisher_expected.items():
            with self.subTest(workflow="publish-release.yml", action=action):
                self.assertIn(f"uses: {action}@{sha}", publisher)
        for name in ("publish-release.yml", "release-beta.yml", "release-stable.yml"):
            with self.subTest(workflow=name, contract="all actions are pinned"):
                action_lines = [line for line in self.read(name).splitlines()
                                if re.match(r"^\s*uses:", line)]
                self.assertTrue(action_lines)
                for line in action_lines:
                    self.assertRegex(
                        line,
                        r"^\s*uses:\s+[^@\s]+@[0-9a-f]{40}(?:\s+#.*)?$",
                    )

    def test_publisher_rechecks_tag_immediately_before_release_creation(self):
        text = self.read("publish-release.yml")
        publish_start = text.index("gh release view $env:RELEASE_TAG")
        refetch = text.index('gh api "repos/$env:GITHUB_REPOSITORY/commits/tags/$env:RELEASE_TAG"', publish_start)
        recheck = text.index("$prepublishTagSha -ne $env:SOURCE_SHA", refetch)
        create_beta = text.index("gh release create $env:RELEASE_TAG", recheck)
        create_stable = text.index("gh release create $env:RELEASE_TAG", create_beta + 1)
        self.assertLess(publish_start, refetch)
        self.assertLess(refetch, recheck)
        self.assertLess(recheck, create_beta)
        self.assertLess(create_beta, create_stable)

    def test_write_job_revalidates_manifest_and_all_candidate_asset_hashes(self):
        text = self.read("publish-release.yml")
        publish = text.split("  publish:\n", 1)[1]
        for contract in (
            "gh run download $env:SOURCE_RUN_ID",
            "$manifest.assets.Count -ne 6",
            "$source.sha -ne $env:SOURCE_SHA",
            "$source.tree -ne $env:SOURCE_TREE",
            "Get-FileHash -Algorithm SHA256 $assetPath",
            "$actualHash -ne $asset.sha256",
            "$checksumText -ne \"$($binary.sha256)  $($binary.name)\"",
            "commits/tags/$env:RELEASE_TAG",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, publish)

    def test_publisher_binds_run_repository_workflow_event_branch_result_and_commit(self):
        text = self.read("publish-release.yml")
        for contract in (
            "run.repository.full_name",
            "run.head_repository.full_name",
            "run.workflow_id",
            "actions/workflows?per_page=100",
            "$_.path -eq $env:EXPECTED_WORKFLOW",
            'run.event -ne "workflow_dispatch"',
            'run.head_branch -ne "main"',
            'run.status -ne "completed"',
            'run.conclusion -ne "success"',
            "run.head_sha",
            "run.run_attempt",
            "source_attempt=$($run.run_attempt)",
            'refs/tags/$env:RELEASE_TAG',
            'FETCH_HEAD^{commit}',
            'tagSha -ne $run.head_sha',
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, text)

    def test_ci_beta_dispatch_is_restricted_to_main_and_tracks_release_contract_files(self):
        text = self.read("ci-beta-package.yml")
        self.assertIn("github.ref == 'refs/heads/main'", text)
        for path in (
            "publish-release.yml",
            "tools/release_candidate.py",
            "tests/test_release_candidate.py",
            "tests/test_release_workflow_contract.py",
            "store.py",
            "core/**",
            "vault.py",
            "static/**",
            "tests/**",
            "tools/**",
        ):
            self.assertIn(path, text)

    def test_all_python_test_workflows_require_the_pinned_playwright_browser(self):
        requirements = (ROOT / "requirements-test.txt").read_text(encoding="utf-8")
        self.assertIn("playwright==1.63.0", requirements)
        for name in ("ci.yml", "ci-beta-package.yml", "release-beta.yml", "release-stable.yml"):
            with self.subTest(workflow=name):
                text = self.read(name)
                self.assertIn('BFO_REQUIRE_PLAYWRIGHT: "1"', text)
                self.assertIn("python -m playwright install chromium", text)

        ui_tests = (ROOT / "tests" / "test_ui_regressions.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("BFO_REQUIRE_PLAYWRIGHT") == "1"', ui_tests)
        self.assertIn("but the Playwright Python package is unavailable", ui_tests)


if __name__ == "__main__":
    unittest.main()
