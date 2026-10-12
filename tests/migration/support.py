"""Temporary source trees and data helpers for migration compatibility tests."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_REPOSITORY = Path(os.environ.get("BFO_SOURCE_REPOSITORY") or REPO_ROOT).resolve()
LEGACY_REF = os.environ.get("BFO_LEGACY_REF", "v0.208")
LEGACY_COMMIT = "b39d7af30a8a365e1e491d848a70474036ec3113"
LEGACY_STORE_BLOB = "f9380f5421769620dfdac162db712ca94fb9910b"
CORE_TABLES = (
    "folders", "videos", "folder_items", "folder_scan_state", "scan_stage",
    "folder_profiles", "analyses", "plans", "app_state",
)
ALL_TABLES = (
    "schema_migrations", *CORE_TABLES, "providers", "models",
)

_FIXTURE_TEMP: tempfile.TemporaryDirectory[str] | None = None
_FIXTURE_PATHS: dict[str, dict] = {}


@dataclass(frozen=True)
class Runtime:
    root: Path
    app_dir: Path
    local_app_data: Path
    data_dir: Path
    db_file: Path
    encrypted_file: Path
    env: dict[str, str]


def _git_text(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=SOURCE_REPOSITORY, text=True,
        encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"Git command failed ({' '.join(args)}): {result.stderr.strip()}"
        )
    return result.stdout


def historical_source_identity() -> dict[str, str]:
    commit = _git_text("rev-parse", f"{LEGACY_REF}^{{commit}}").strip()
    blob = _git_text("rev-parse", f"{LEGACY_REF}:store.py").strip()
    if LEGACY_REF == "v0.208":
        if commit != LEGACY_COMMIT:
            raise AssertionError(f"v0.208 resolved to unexpected commit: {commit}")
        if blob != LEGACY_STORE_BLOB:
            raise AssertionError(f"v0.208 store.py blob changed: {blob}")
    return {"ref": LEGACY_REF, "commit": commit, "store_blob": blob}


def _isolated_env(root: Path, *, data_override: Path | None = None,
                  source_repository: Path | None = None) -> dict[str, str]:
    profile = root / "profile"
    home = root / "home"
    temp = root / "temp"
    profile.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(profile)
    env["USERPROFILE"] = str(profile)
    env["HOME"] = str(home)
    env["TEMP"] = str(temp)
    env["TMP"] = str(temp)
    env["TMPDIR"] = str(temp)
    env.pop("BILI_FAV_ORGANIZER_DATA_DIR", None)
    if data_override is not None:
        env["BILI_FAV_ORGANIZER_DATA_DIR"] = str(data_override)
    if source_repository is not None:
        env["BFO_SOURCE_REPOSITORY"] = str(source_repository)
    env["BFO_MIGRATION_TEST"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # No test action may make a network call. This proxy points at a closed loopback port.
    env["HTTP_PROXY"] = "http://127.0.0.1:9"
    env["HTTPS_PROXY"] = "http://127.0.0.1:9"
    env["ALL_PROXY"] = "http://127.0.0.1:9"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    return env


def _generate_fixture(case: str, root: Path) -> dict:
    source = _git_text("show", f"{LEGACY_REF}:store.py")
    legacy_app = root / "legacy-app"
    legacy_app.mkdir(parents=True)
    (legacy_app / "store.py").write_text(source, encoding="utf-8")
    legacy_home = root / "legacy-profile"
    legacy_home.mkdir(parents=True)
    legacy_temp = root / "legacy-temp"
    legacy_temp.mkdir(parents=True)
    capture_dir = root / "wal-capture"
    seed_script = REPO_ROOT / "tests" / "migration" / "fixtures" / "seed_v0208.py"
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(legacy_home)
    env["USERPROFILE"] = str(legacy_home)
    env["HOME"] = str(legacy_home)
    env["TEMP"] = str(legacy_temp)
    env["TMP"] = str(legacy_temp)
    env["TMPDIR"] = str(legacy_temp)
    env["PYTHONPATH"] = str(legacy_app)
    env["BFO_MIGRATION_TEST"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("BILI_FAV_ORGANIZER_DATA_DIR", None)
    env.pop("BFO_SOURCE_REPOSITORY", None)
    env["HTTP_PROXY"] = "http://127.0.0.1:9"
    env["HTTPS_PROXY"] = "http://127.0.0.1:9"
    env["ALL_PROXY"] = "http://127.0.0.1:9"
    env["NO_PROXY"] = "127.0.0.1,localhost"

    command = [sys.executable, str(seed_script), case]
    if case == "legacy-edge":
        capture_dir.mkdir()
        command.append(str(capture_dir))
        process = subprocess.Popen(
            command, cwd=legacy_app, env=env, text=True,
            encoding="utf-8", errors="replace",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert process.stdout is not None and process.stdin is not None
        ready = process.stdout.readline().strip()
        if ready != "WAL_READY":
            stdout_tail, stderr = process.communicate(timeout=15)
            raise RuntimeError(
                f"v0.208 WAL fixture generation failed: {ready}\n{stdout_tail}\n{stderr}"
            )
        process.stdin.write("finish\n")
        process.stdin.close()
        stdout_tail = process.stdout.read()
        stderr = process.stderr.read() if process.stderr is not None else ""
        code = process.wait(timeout=15)
        process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        if code:
            raise RuntimeError(f"v0.208 WAL fixture generation failed: {stdout_tail}\n{stderr}")
        lines = [line.strip() for line in stdout_tail.splitlines() if line.strip()]
        manifest = json.loads(lines[-1])
    else:
        result = subprocess.run(
            command, cwd=legacy_app, env=env, text=True,
            encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"v0.208 fixture generation failed for {case}:\n"
                f"{result.stdout}\n{result.stderr}"
            )
        manifest = json.loads(result.stdout.splitlines()[-1])

    manifest["source"] = historical_source_identity()
    manifest["db_file"] = str(Path(manifest["db_file"]))
    manifest["wal_capture_dir"] = str(capture_dir) if case == "legacy-edge" else ""
    return manifest


def fixture(case: str) -> dict:
    global _FIXTURE_TEMP
    if case not in {"legacy-empty", "legacy-normal", "legacy-edge"}:
        raise ValueError(case)
    if case not in _FIXTURE_PATHS:
        if _FIXTURE_TEMP is None:
            _FIXTURE_TEMP = tempfile.TemporaryDirectory(prefix="bfo-v0208-fixtures-")
        base = Path(_FIXTURE_TEMP.name) / case
        base.mkdir(parents=True, exist_ok=True)
        _FIXTURE_PATHS[case] = _generate_fixture(case, base)
    return _FIXTURE_PATHS[case]


def _copy_current_app(app_dir: Path) -> None:
    app_dir.mkdir(parents=True, exist_ok=True)
    for name in ("server.py", "store.py", "vault.py", "bili_api.py", "llm_analyzer.py"):
        shutil.copy2(REPO_ROOT / name, app_dir / name)
    shutil.copytree(REPO_ROOT / "core", app_dir / "core",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (app_dir / "static").mkdir()
    shutil.copy2(REPO_ROOT / "tests" / "migration" / "runtime_probe.py",
                 app_dir / "migration_probe.py")


def runtime(case: str, root: Path, *, data_override: Path | None = None,
            local_app_data_root: Path | None = None, legacy_config: bool = True,
            copy_legacy_db: bool = True) -> Runtime:
    manifest = fixture(case)
    app_dir = root / "current-app"
    _copy_current_app(app_dir)
    legacy_dir = app_dir / "data"
    legacy_dir.mkdir()
    if copy_legacy_db:
        if case == "legacy-edge":
            wal_source = Path(manifest["wal_capture_dir"])
            for name in ("library.sqlite3", "library.sqlite3-wal", "library.sqlite3-shm"):
                source = wal_source / name
                if source.is_file():
                    shutil.copy2(source, legacy_dir / name)
        else:
            shutil.copy2(Path(manifest["db_file"]), legacy_dir / "library.sqlite3")

    if legacy_config and case != "legacy-empty":
        config = {
            "base_url": "https://invalid.example/v1",
            "model": "synthetic-config-model",
            "scan_interval": 2,
        }
        if case == "legacy-edge":
            # Old single-file credentials exercise the v0.208-to-current split migration.
            config["api_key"] = "SYNTHETIC_CONFIG_API_KEY_DO_NOT_USE"
            config["cookie_string"] = (
                "DedeUserID=9000001; SESSDATA=SYNTHETIC-NO-NETWORK-COOKIE"
            )
        else:
            (app_dir / "secrets.json").write_text(json.dumps({
                "cookie_string": "DedeUserID=9000001; SESSDATA=SYNTHETIC-NO-NETWORK-COOKIE"
            }), encoding="utf-8")
        (app_dir / "config.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    env = _isolated_env(root, data_override=data_override,
                        source_repository=SOURCE_REPOSITORY)
    if local_app_data_root is not None:
        env["LOCALAPPDATA"] = str(local_app_data_root)
        env["USERPROFILE"] = str(local_app_data_root)
        Path(local_app_data_root).mkdir(parents=True, exist_ok=True)
    local_app_data = Path(env["LOCALAPPDATA"]) / "BiliFavOrganizer"
    data_dir = data_override or (local_app_data / "data")
    return Runtime(
        root=root, app_dir=app_dir, local_app_data=local_app_data,
        data_dir=data_dir, db_file=data_dir / "library.sqlite3",
        encrypted_file=data_dir / "library.sqlite3.enc", env=env,
    )


def probe(runtime_case: Runtime, operation: str, *, password: str = "Synthetic.Passphrase.42",
          recovery_code: str = "", timeout: int = 30) -> dict:
    env = dict(runtime_case.env)
    env["BFO_PROBE_PASSWORD"] = password
    env["BFO_PROBE_RECOVERY"] = recovery_code
    result = subprocess.run(
        [sys.executable, "migration_probe.py", operation],
        cwd=runtime_case.app_dir, env=env, text=True,
        encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"current-code probe {operation!r} failed (exit={result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )
    for line in reversed(result.stdout.splitlines()):
        if line.startswith("MIGRATION_PROBE:"):
            return json.loads(line.removeprefix("MIGRATION_PROBE:"))
    raise RuntimeError(f"current-code probe emitted no result:\n{result.stdout}\n{result.stderr}")


def database_snapshot(db_file: Path, *, include_credentials: bool = True) -> dict:
    """Read only a named test database and hash normalized logical rows."""
    if not db_file.is_file():
        return {"exists": False, "path": str(db_file)}
    uri = f"file:{db_file.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        counts = {}
        logical_rows = {}
        for table in ALL_TABLES:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                counts[table] = 0
                logical_rows[table] = []
                continue
            counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
            rows.sort(key=lambda row: tuple(str(row.get(key, "")) for key in sorted(row)))
            if table == "schema_migrations":
                rows = [{"version": row["version"]} for row in rows]
            if table in ("providers", "models") and not include_credentials:
                for row in rows:
                    row.pop("api_key", None)
            logical_rows[table] = rows
        digest_input = json.dumps(
            logical_rows, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        ).encode("utf-8")
        return {
            "exists": True,
            "integrity_check": integrity,
            "foreign_key_violations": foreign_keys,
            "counts": counts,
            "sha256": hashlib.sha256(digest_input).hexdigest(),
            "logical_rows": logical_rows,
        }
    finally:
        connection.close()


def core_sha256(snapshot: dict) -> str:
    rows = {table: snapshot["logical_rows"].get(table, []) for table in CORE_TABLES}
    raw = json.dumps(rows, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
