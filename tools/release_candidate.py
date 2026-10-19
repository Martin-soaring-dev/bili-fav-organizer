#!/usr/bin/env python3
"""Create and verify the integrity manifest for a Windows release candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
ASSET_COUNT = 6
BETA_TAG = re.compile(r"^v(?P<major>\d+)\.(?P<minor>\d+)-beta\.(?P<patch>\d+)$")
STABLE_TAG = re.compile(r"^v(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CandidateError(ValueError):
    """Raised when a release candidate cannot be trusted or verified."""


def release_asset_names(channel: str, tag: str) -> tuple[str, ...]:
    if channel == "beta":
        match = BETA_TAG.fullmatch(tag)
        if not match:
            raise CandidateError(f"Unsupported beta release tag: {tag}")
        numeric = f"{match['major']}.{match['minor']}.0-beta.{match['patch']}"
    elif channel == "stable":
        match = STABLE_TAG.fullmatch(tag)
        if not match:
            raise CandidateError(f"Unsupported stable release tag: {tag}")
        patch = match["patch"] or "0"
        numeric = f"{match['major']}.{match['minor']}.{patch}"
    else:
        raise CandidateError(f"Unsupported release channel: {channel}")

    zip_name = f"BiliFavOrganizer-Windows-x64-{tag}.zip"
    setup_name = f"BiliFavOrganizer-Setup-{tag}.exe"
    electron_name = f"BiliFavOrganizer-neo-Setup-{numeric}.exe"
    return (
        zip_name,
        f"{zip_name}.sha256",
        setup_name,
        f"{setup_name}.sha256",
        electron_name,
        f"{electron_name}.sha256",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_tree(source_sha: str) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", f"{source_sha}^{{tree}}"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CandidateError(f"Cannot resolve source tree for {source_sha}") from exc
    tree = result.stdout.strip().lower()
    if not SHA40.fullmatch(tree):
        raise CandidateError("Git returned an invalid source tree id")
    return tree


def create_manifest(
    *,
    directory: Path,
    channel: str,
    tag: str,
    repository: str,
    run_id: str,
    run_attempt: str,
    source_sha: str,
    workflow_file: str,
    source_ref: str,
) -> Path:
    names = release_asset_names(channel, tag)
    sha = source_sha.lower()
    if not SHA40.fullmatch(sha):
        raise CandidateError("source_sha must be a full 40-character commit SHA")
    if source_ref != "refs/heads/main":
        raise CandidateError("Release candidates may only be built from refs/heads/main")
    if not repository or "/" not in repository:
        raise CandidateError("repository must be owner/name")
    if not run_id.isdecimal():
        raise CandidateError("run_id must be a numeric GitHub Actions run id")
    if not run_attempt.isdecimal() or int(run_attempt) < 1:
        raise CandidateError("run_attempt must be a positive numeric GitHub Actions attempt")
    if workflow_file not in {
        ".github/workflows/release-beta.yml",
        ".github/workflows/release-stable.yml",
    }:
        raise CandidateError("Unexpected release build workflow path")

    directory.mkdir(parents=True, exist_ok=True)
    assets: list[dict[str, Any]] = []
    for name in names:
        path = directory / name
        if not path.is_file():
            raise CandidateError(f"Required release asset is missing: {name}")
        if path.stat().st_size <= 0:
            raise CandidateError(f"Release asset is empty: {name}")
        assets.append({"name": name, "size": path.stat().st_size, "sha256": _sha256(path)})

    # Each checksum sidecar must verify the corresponding binary/archive itself.
    for index in (0, 2, 4):
        asset = assets[index]
        sidecar = assets[index + 1]
        expected_line = f"{asset['sha256']}  {asset['name']}"
        try:
            actual_line = (directory / sidecar["name"]).read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise CandidateError(f"Invalid checksum sidecar: {sidecar['name']}") from exc
        if actual_line.lower() != expected_line.lower():
            raise CandidateError(f"Checksum sidecar does not match {asset['name']}")

    tree = _git_tree(sha)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "repository": repository,
        "source": {
            "run_id": int(run_id),
            "run_attempt": int(run_attempt),
            "workflow_file": workflow_file,
            "event": "workflow_dispatch",
            "ref": source_ref,
            "sha": sha,
            "tree": tree,
        },
        "release": {"channel": channel, "tag": tag},
        "assets": assets,
    }
    manifest_path = directory / "release-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def verify_manifest(
    *,
    directory: Path,
    channel: str,
    tag: str,
    repository: str,
    run_id: str,
    run_attempt: str,
    source_sha: str,
    source_tree: str,
    workflow_file: str,
) -> dict[str, Any]:
    manifest_path = directory / "release-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CandidateError("release-manifest.json is missing or invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise CandidateError("Unsupported release manifest schema")
    if manifest.get("repository") != repository:
        raise CandidateError("Manifest repository does not match this repository")
    if manifest.get("release") != {"channel": channel, "tag": tag}:
        raise CandidateError("Manifest release identity does not match requested release")

    source = manifest.get("source")
    expected_source = {
        "run_id": int(run_id),
        "run_attempt": int(run_attempt),
        "workflow_file": workflow_file,
        "event": "workflow_dispatch",
        "ref": "refs/heads/main",
        "sha": source_sha.lower(),
        "tree": source_tree.lower(),
    }
    if source != expected_source:
        raise CandidateError("Manifest source identity does not match the trusted build run")
    if not SHA40.fullmatch(expected_source["sha"]) or not SHA40.fullmatch(expected_source["tree"]):
        raise CandidateError("Trusted source SHA/tree is malformed")

    expected_names = release_asset_names(channel, tag)
    assets = manifest.get("assets")
    if not isinstance(assets, list) or len(assets) != ASSET_COUNT:
        raise CandidateError(f"Manifest must contain exactly {ASSET_COUNT} assets")
    if tuple(item.get("name") if isinstance(item, dict) else None for item in assets) != expected_names:
        raise CandidateError("Manifest asset names/order do not match the release tag")

    by_name: dict[str, dict[str, Any]] = {}
    for item in assets:
        if not isinstance(item, dict):
            raise CandidateError("Malformed asset entry")
        name = item["name"]
        path = directory / name
        if not path.is_file():
            raise CandidateError(f"Release asset is missing: {name}")
        actual_size = path.stat().st_size
        if actual_size <= 0:
            raise CandidateError(f"Release asset is empty: {name}")
        actual_hash = _sha256(path)
        if item.get("size") != actual_size:
            raise CandidateError(f"Asset size does not match manifest: {name}")
        digest = item.get("sha256")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest) or digest != actual_hash:
            raise CandidateError(f"Asset SHA-256 does not match manifest: {name}")
        by_name[name] = item

    for index in (0, 2, 4):
        asset = assets[index]
        sidecar = assets[index + 1]
        expected_line = f"{asset['sha256']}  {asset['name']}"
        actual_line = (directory / sidecar["name"]).read_text(encoding="ascii").strip()
        if actual_line.lower() != expected_line.lower():
            raise CandidateError(f"Checksum sidecar does not match {asset['name']}")
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create-manifest")
    create.add_argument("--directory", type=Path, required=True)
    create.add_argument("--channel", choices=("beta", "stable"), required=True)
    create.add_argument("--tag", required=True)
    create.add_argument("--repository", required=True)
    create.add_argument("--run-id", required=True)
    create.add_argument("--run-attempt", required=True)
    create.add_argument("--source-sha", required=True)
    create.add_argument("--source-ref", required=True)
    create.add_argument("--workflow-file", required=True)
    verify = subparsers.add_parser("verify-manifest")
    verify.add_argument("--directory", type=Path, required=True)
    verify.add_argument("--channel", choices=("beta", "stable"), required=True)
    verify.add_argument("--tag", required=True)
    verify.add_argument("--repository", required=True)
    verify.add_argument("--run-id", required=True)
    verify.add_argument("--run-attempt", required=True)
    verify.add_argument("--source-sha", required=True)
    verify.add_argument("--source-tree", required=True)
    verify.add_argument("--workflow-file", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create-manifest":
            path = create_manifest(
                directory=args.directory,
                channel=args.channel,
                tag=args.tag,
                repository=args.repository,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                source_sha=args.source_sha,
                workflow_file=args.workflow_file,
                source_ref=args.source_ref,
            )
            print(f"Created {path}")
        else:
            manifest = verify_manifest(
                directory=args.directory,
                channel=args.channel,
                tag=args.tag,
                repository=args.repository,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                source_sha=args.source_sha,
                source_tree=args.source_tree,
                workflow_file=args.workflow_file,
            )
            print(f"Verified {len(manifest['assets'])} assets for {args.tag}")
    except CandidateError as exc:
        print(f"release candidate error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
