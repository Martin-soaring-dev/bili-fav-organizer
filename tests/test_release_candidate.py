from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.release_candidate import (
    CandidateError,
    create_manifest,
    release_asset_names,
    verify_manifest,
)


SOURCE_SHA = "a" * 40
SOURCE_TREE = "b" * 40


class ReleaseCandidateTests(unittest.TestCase):
    def _make_candidate(self, directory: Path, channel: str = "beta", tag: str = "v0.210-beta.1") -> None:
        names = release_asset_names(channel, tag)
        for index in (0, 2, 4):
            data = f"binary-{index}".encode("ascii")
            (directory / names[index]).write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            (directory / names[index + 1]).write_text(
                f"{digest}  {names[index]}\n", encoding="ascii"
            )

    def _create(self, directory: Path, channel: str = "beta", tag: str = "v0.210-beta.1") -> None:
        with patch("tools.release_candidate._git_tree", return_value=SOURCE_TREE):
            create_manifest(
                directory=directory,
                channel=channel,
                tag=tag,
                repository="owner/project",
                run_id="12345",
                run_attempt="2",
                source_sha=SOURCE_SHA,
                workflow_file=(
                    ".github/workflows/release-beta.yml"
                    if channel == "beta"
                    else ".github/workflows/release-stable.yml"
                ),
                source_ref="refs/heads/main",
            )

    def test_beta_and_stable_asset_names_are_bound_to_tags(self):
        beta = release_asset_names("beta", "v0.210-beta.3")
        self.assertEqual(beta[0], "BiliFavOrganizer-Windows-x64-v0.210-beta.3.zip")
        self.assertEqual(beta[4], "BiliFavOrganizer-neo-Setup-0.210.0-beta.3.exe")
        stable = release_asset_names("stable", "v1.2")
        self.assertEqual(stable[2], "BiliFavOrganizer-Setup-v1.2.exe")
        self.assertEqual(stable[4], "BiliFavOrganizer-neo-Setup-1.2.0.exe")

    def test_invalid_tag_channel_and_source_ref_are_rejected(self):
        with self.assertRaises(CandidateError):
            release_asset_names("beta", "v1.2")
        with self.assertRaises(CandidateError):
            release_asset_names("stable", "v1.2-beta.1")
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            self._make_candidate(directory)
            with self.assertRaises(CandidateError):
                create_manifest(
                    directory=directory,
                    channel="beta",
                    tag="v0.210-beta.1",
                    repository="owner/project",
                    run_id="12345",
                    run_attempt="2",
                    source_sha=SOURCE_SHA,
                    workflow_file=".github/workflows/release-beta.yml",
                    source_ref="refs/heads/feature",
                )

    def test_manifest_verifies_trusted_identity_and_all_six_hashes(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            self._make_candidate(directory)
            self._create(directory)
            manifest = verify_manifest(
                directory=directory,
                channel="beta",
                tag="v0.210-beta.1",
                repository="owner/project",
                run_id="12345",
                run_attempt="2",
                source_sha=SOURCE_SHA,
                source_tree=SOURCE_TREE,
                workflow_file=".github/workflows/release-beta.yml",
            )
            self.assertEqual(len(manifest["assets"]), 6)

    def test_tampered_asset_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            self._make_candidate(directory)
            self._create(directory)
            (directory / release_asset_names("beta", "v0.210-beta.1")[0]).write_bytes(b"tampered")
            with self.assertRaises(CandidateError):
                verify_manifest(
                    directory=directory,
                    channel="beta",
                    tag="v0.210-beta.1",
                    repository="owner/project",
                    run_id="12345",
                    run_attempt="2",
                    source_sha=SOURCE_SHA,
                    source_tree=SOURCE_TREE,
                    workflow_file=".github/workflows/release-beta.yml",
                )

    def test_manifest_rejects_source_run_tag_or_sidecar_mismatch(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            self._make_candidate(directory)
            self._create(directory)
            for kwargs in (
                {"run_id": "999"},
                {"run_attempt": "3"},
                {"source_sha": "c" * 40},
                {"source_tree": "d" * 40},
                {"tag": "v0.210-beta.2"},
            ):
                with self.subTest(kwargs=kwargs), self.assertRaises(CandidateError):
                    verify_manifest(
                        directory=directory,
                        channel="beta",
                        tag=kwargs.get("tag", "v0.210-beta.1"),
                        repository="owner/project",
                        run_id=kwargs.get("run_id", "12345"),
                        run_attempt=kwargs.get("run_attempt", "2"),
                        source_sha=kwargs.get("source_sha", SOURCE_SHA),
                        source_tree=kwargs.get("source_tree", SOURCE_TREE),
                        workflow_file=".github/workflows/release-beta.yml",
                    )

            checksum = release_asset_names("beta", "v0.210-beta.1")[1]
            (directory / checksum).write_text("0" * 64 + "  wrong.zip\n", encoding="ascii")
            with self.assertRaises(CandidateError):
                verify_manifest(
                    directory=directory,
                    channel="beta",
                    tag="v0.210-beta.1",
                    repository="owner/project",
                    run_id="12345",
                    run_attempt="2",
                    source_sha=SOURCE_SHA,
                    source_tree=SOURCE_TREE,
                    workflow_file=".github/workflows/release-beta.yml",
                )


if __name__ == "__main__":
    unittest.main()
