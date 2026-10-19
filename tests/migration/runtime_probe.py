"""One-shot probe executed only in a copied source tree with temporary user paths."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from contextlib import nullcontext
from unittest import mock
from urllib.parse import unquote
from pathlib import Path


_SQLITE_PATH_TRACE: list[dict] = []


def _trace_sqlite_path_events(event, args) -> None:
    """Keep file-operation stacks available only when a probe fails.

    The migration probes run in fresh child processes. If a locked profile ever
    gains a database/sidecar before the restore guard, this audit hook records
    Python-level connect/file-operation call sites for relevant SQLite paths.
    It does not observe file opens performed inside SQLite's native VFS, so an
    empty trace is not proof that no native SQLite access occurred.
    """
    if event not in {"sqlite3.connect", "open", "os.link", "os.rename", "os.remove"}:
        return
    paths = []
    for value in args:
        raw = str(value)
        if raw.startswith("file:"):
            raw = raw[5:].split("?", 1)[0]
        raw = unquote(raw).replace("\\", "/")
        if "/bfo-probe-snapshot-" in raw:
            # Do not report the diagnostic snapshot's own SQLite reads as a
            # writer/opening actor against the tested profile.
            continue
        name = raw.rsplit("/", 1)[-1]
        folded = name.casefold()
        if folded.endswith(".enc") and "library.sqlite3" in folded:
            kind = "encrypted_snapshot"
        elif folded.startswith(".vault-restore-") and folded.endswith(".sqlite3"):
            kind = "restore_candidate"
        elif folded.startswith(".vault-verify-") and folded.endswith(".sqlite3"):
            kind = "verification_candidate"
        elif folded.endswith(".sqlite3-wal"):
            kind = "wal_sidecar"
        elif folded.endswith(".sqlite3-shm"):
            kind = "shm_sidecar"
        elif folded.endswith(".sqlite3-journal"):
            kind = "journal_sidecar"
        elif "library.sqlite3" in folded:
            kind = "database_or_staged_copy"
        else:
            continue
        paths.append({"kind": kind, "name": name})
    if not paths:
        return
    stack = []
    frame = sys._getframe(1)
    while frame is not None and len(stack) < 10:
        code = frame.f_code
        stack.append(f"{Path(code.co_filename).name}:{frame.f_lineno}:{code.co_name}")
        frame = frame.f_back
    _SQLITE_PATH_TRACE.append({
        "event": event,
        "pid": os.getpid(),
        "paths": paths,
        "stack": stack,
    })
    del _SQLITE_PATH_TRACE[:-80]


sys.addaudithook(_trace_sqlite_path_events)

import store
import server
import vault as vault_module
from fastapi.responses import JSONResponse


TABLES = (
    "schema_migrations", "folders", "videos", "folder_items", "folder_scan_state",
    "scan_stage", "folder_profiles", "analyses", "plans", "app_state", "providers", "models",
)
CORE_TABLES = TABLES[1:9]


def database_snapshot(path: Path) -> dict:
    if not path.is_file():
        # The secure locked state deliberately has no plaintext working DB.
        return {"exists": False, "counts": {name: 0 for name in TABLES},
                "logical_rows": {}, "sha256": None,
                "integrity_check": [], "foreign_key_violations": []}
    # SQLite read-only connections may still create WAL/SHM files beside a WAL
    # database. Snapshot a private copy so observing a locked/unlocked test
    # profile cannot alter the evidence being tested or affect the next probe.
    with tempfile.TemporaryDirectory(prefix="bfo-probe-snapshot-") as snapshot_dir:
        snapshot_path = Path(snapshot_dir) / path.name
        shutil.copy2(path, snapshot_path)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(path) + suffix)
            if sidecar.is_file():
                try:
                    shutil.copy2(sidecar, Path(str(snapshot_path) + suffix))
                except FileNotFoundError:
                    # A concurrently removed transient sidecar is not part of
                    # the snapshot; the main database remains the source.
                    pass
        connection = sqlite3.connect(
            f"file:{snapshot_path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            counts = {}
            logical_rows = {}
            for table in TABLES:
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
                logical_rows[table] = rows
            canonical = json.dumps(
                logical_rows, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), default=str,
            ).encode("utf-8")
            foreign = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
            return {
                "exists": True,
                "integrity_check": [str(row[0]) for row in connection.execute("PRAGMA integrity_check")],
                "foreign_key_violations": foreign,
                "counts": counts,
                "sha256": hashlib.sha256(canonical).hexdigest(),
                "logical_rows": logical_rows,
            }
        finally:
            connection.close()


def _response_data(result) -> dict:
    if isinstance(result, JSONResponse):
        return {"http_status": result.status_code, "body": json.loads(result.body.decode("utf-8"))}
    return {"http_status": 200, "body": result}


def _manifest_summary() -> dict:
    manifest = server._vault_read_lock_manifest(required=True)
    archive_dir, manifest_path = server._vault_manifest_paths()
    return {
        "state": manifest["state"],
        "entries": [dict(entry) for entry in manifest["entries"]],
        "archive_files": sorted(path.name for path in archive_dir.iterdir()
                                 if path.name != manifest_path.name),
        "archive_hashes": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in archive_dir.iterdir() if path.name != manifest_path.name
        },
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "active_ciphertext_sha256": hashlib.sha256(
            server.VAULT_DB_ENC.read_bytes()).hexdigest(),
    }


def _database_sidecars() -> list[str]:
    return [str(candidate) for path in (store.DB_FILE, store.LEGACY_DB_FILE)
            for suffix in ("-wal", "-shm", "-journal")
            if (candidate := Path(str(path) + suffix)).exists()]


def _open_sqlite_descriptors() -> list[str]:
    descriptor_root = Path("/proc/self/fd")
    if not descriptor_root.is_dir():
        return []
    found = []
    for descriptor in descriptor_root.iterdir():
        try:
            target = os.readlink(descriptor)
        except OSError:
            continue
        if "library.sqlite3" in target:
            found.append(target)
    return sorted(found)


def _stage_synthetic_vault_sources() -> dict[str, Path]:
    legacy = Path(store.LEGACY_DATA_DIR)
    legacy.mkdir(parents=True, exist_ok=True)
    legacy_json = legacy / "vault-migration-synthetic.json"
    legacy_json.write_text('{"marker":"SYNTHETIC_LEGACY_JSON"}', encoding="utf-8")
    legacy_jsonl = legacy / "vault-migration-synthetic.jsonl"
    legacy_jsonl.write_text('{"marker":"SYNTHETIC_LEGACY_JSONL"}\n', encoding="utf-8")
    plain_backup = Path(str(store.DB_FILE) + ".plain-backup")
    plain_backup.write_bytes(store.DB_FILE.read_bytes())
    top_level = Path(store.DATA_DIR) / "vault-migration-synthetic.json"
    top_level.write_text('{"marker":"SYNTHETIC_PROFILE_JSON"}', encoding="utf-8")
    backups = Path(store.DATA_DIR) / "backups"
    pre_sqlite = backups / "pre-sqlite-synthetic"
    pre_sqlite.mkdir(parents=True, exist_ok=True)
    pre_file = pre_sqlite / "legacy.json"
    pre_file.write_text('{"marker":"SYNTHETIC_PRE_SQLITE_BACKUP"}', encoding="utf-8")
    auto_backup = backups / "bili-data-synthetic"
    auto_backup.mkdir(parents=True, exist_ok=True)
    auto_file = auto_backup / "snapshot.jsonl"
    auto_file.write_text('{"marker":"SYNTHETIC_MANAGED_BACKUP"}\n', encoding="utf-8")
    return {
        "legacy_json": legacy_json, "legacy_jsonl": legacy_jsonl,
        "plain_backup": plain_backup, "top_level": top_level,
        "pre_sqlite": pre_file, "auto_backup": auto_file,
    }


def _config_summary() -> dict:
    cfg = server.load_config()
    try:
        secrets_json = json.loads(server.SECRETS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        secrets_json = {}
    if store.DB_FILE.is_file() and store.database_state() == "READY":
        try:
            providers = store.list_providers()
            models = store.list_models()
        except (sqlite3.OperationalError, RuntimeError):
            providers, models = [], []
    else:
        # A SELECT must not implicitly create a blank working DB while locked.
        providers, models = [], []
    try:
        source_config = json.loads((Path(server.APP_DIR) / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        source_config = {}
    return {
        "cookie_preserved": bool(cfg.get("cookie_string")) and
                            "SYNTHETIC-NO-NETWORK-COOKIE" in cfg.get("cookie_string", ""),
        "provider_key_preserved": any(
            provider.get("api_key") == "SYNTHETIC_INVALID_API_KEY_DO_NOT_USE"
            for provider in providers
        ),
        "legacy_config_key_migrated": any(
            provider.get("api_key") == "SYNTHETIC_CONFIG_API_KEY_DO_NOT_USE"
            for provider in providers
        ),
        "api_key_remains_in_secrets": bool(secrets_json.get("api_key")),
        "legacy_cookie_still_in_source_config": bool(source_config.get("cookie_string")),
        "provider_count": len(providers),
        "model_count": len(models),
        "config_broken": bool(cfg.get("_config_broken")),
        "default_scan_interval": cfg.get("scan_interval"),
    }


def main() -> None:
    operation = sys.argv[1]
    result = {
        "operation": operation,
        "user_data_dir": str(server.USER_DATA_DIR),
        "store_data_dir": str(store.DATA_DIR),
        "db_file": str(store.DB_FILE),
        "encrypted_file": str(server.VAULT_DB_ENC),
        "database_sidecars_at_probe_start": _database_sidecars(),
        "encrypted_file_sha256_at_probe_start": (
            hashlib.sha256(server.VAULT_DB_ENC.read_bytes()).hexdigest()
            if server.VAULT_DB_ENC.is_file() else None),
        "migration_source_exists": (Path(server.APP_DIR) / "data" / "library.sqlite3").is_file(),
        "database": database_snapshot(Path(store.DB_FILE)),
        "config": _config_summary(),
    }

    if operation == "onboard":
        body = server.VaultInitIn(bound_mid="9000001", password=os.environ["BFO_PROBE_PASSWORD"])
        result["onboarding"] = _response_data(server.vault_onboarding(body))
        if result["onboarding"]["http_status"] == 200:
            result["recovery_code"] = result["onboarding"]["body"].get("recovery_code", "")
            failure = (mock.patch.object(server, "_vault_archive_sources", side_effect=
                       PermissionError(13, "synthetic archive access denied"))
                       if os.environ.get("BFO_PROBE_ARCHIVE_FAILURE") == "1"
                       else nullcontext())
            with failure:
                result["completion"] = _response_data(server.vault_complete_onboarding())
            result["vault_locked_after_completion"] = not server.VAULT.is_unlocked()
            # A ciphertext may have been installed before a later archive
            # failure. Keep the original completion error instead of raising
            # a secondary "manifest missing" while collecting diagnostics.
            if (result["completion"]["http_status"] == 200 and
                    server.VAULT_DB_ENC.is_file()):
                decrypted = Path(os.environ["TEMP"]) / "decrypted-synthetic.sqlite3"
                server.VAULT.load_meta()
                server.VAULT.unlock_with_password(os.environ["BFO_PROBE_PASSWORD"])
                result["manifest"] = _manifest_summary()
                server.VAULT.decrypt_file(server.VAULT_DB_ENC, decrypted)
                result["decrypted_snapshot"] = database_snapshot(decrypted)
                decrypted.unlink(missing_ok=True)

    elif operation == "unlock":
        server.VAULT.load_meta()
        unlock_body = server.VaultUnlockIn(
            password=os.environ.get("BFO_PROBE_PASSWORD", ""),
            recovery_code=os.environ.get("BFO_PROBE_RECOVERY", ""),
        )
        result["unlock"] = _response_data(server.vault_unlock(unlock_body))
        result["vault_unlocked"] = server.VAULT.is_unlocked()
        result["database_after_unlock"] = database_snapshot(Path(store.DB_FILE))

    elif operation == "selftest":
        server.VAULT.load_meta()
        server.VAULT.unlock_with_password(os.environ["BFO_PROBE_PASSWORD"])
        result["selftest"] = server._dek_selftest()

    elif operation == "explicit-lock":
        server.VAULT.load_meta()
        server.VAULT.unlock_with_password(os.environ["BFO_PROBE_PASSWORD"])
        server._vault_restore_db_if_needed()
        result["lock"] = _response_data(server.vault_lock())
        result["vault_locked"] = not server.VAULT.is_unlocked()
        result["plain_db_exists_after_lock"] = Path(store.DB_FILE).is_file()
        result["database_sidecars_after_lock"] = _database_sidecars()
        if server.VAULT.is_unlocked():
            server.VAULT.lock()
        if result["lock"]["http_status"] == 200:
            server.VAULT.unlock_with_password(os.environ["BFO_PROBE_PASSWORD"])
            result["manifest"] = _manifest_summary()
            server.VAULT.lock()

    elif operation in {"seal-sources", "seal-with-residue", "seal-paused"}:
        result["onboarding"] = _response_data(server.vault_onboarding(
            server.VaultInitIn(bound_mid="9000001",
                               password=os.environ["BFO_PROBE_PASSWORD"])))
        source_paths = _stage_synthetic_vault_sources()
        residue = Path(store.DATA_DIR) / ".vault-restore-interrupted.sqlite3"
        candidate_sidecar = (Path(store.DATA_DIR) /
                             ".library.sqlite3.interrupted.candidate-shm")
        if operation == "seal-with-residue":
            residue.write_bytes(b"SYNTHETIC_INTERRUPTED_CANDIDATE")
            candidate_sidecar.write_bytes(b"SYNTHETIC_CANDIDATE_SIDECAR")
        if operation == "seal-paused":
            original_remove_plaintext = server._vault_remove_plaintext
            marker = Path(os.environ["BFO_SEAL_PAUSE_MARKER"])
            release = Path(os.environ["BFO_SEAL_RELEASE_MARKER"])

            def pause_before_removal(sources, expected_hashes):
                marker.write_text("ready", encoding="ascii")
                deadline = time.monotonic() + 20
                while not release.exists():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("test did not release the vault sealing barrier")
                    time.sleep(0.01)
                return original_remove_plaintext(sources, expected_hashes)

            server._vault_remove_plaintext = pause_before_removal
        result["completion"] = _response_data(server.vault_complete_onboarding())
        result["vault_locked"] = not server.VAULT.is_unlocked()
        result["sources_after_lock"] = {
            name: path.exists() for name, path in source_paths.items()
        }
        result["residue_after_lock"] = residue.read_bytes().decode("ascii") if residue.exists() else None
        result["candidate_sidecar_after_lock"] = (
            candidate_sidecar.read_bytes().decode("ascii")
            if candidate_sidecar.exists() else None)
        if (result["completion"]["http_status"] == 200 and
                server.VAULT_DB_ENC.is_file()):
            if not server.VAULT.is_unlocked():
                server.VAULT.unlock_with_password(os.environ["BFO_PROBE_PASSWORD"])
            result["manifest"] = _manifest_summary()
            server.VAULT.lock()

    elif operation == "candidate-encryption-failure":
        server.VAULT.initialize(bound_mid="9000001", password=os.environ["BFO_PROBE_PASSWORD"])
        server._vault_encrypt_consistent_db()
        target = Path(server.VAULT_DB_ENC)
        previous = target.read_bytes()
        original_encrypt = server.VAULT.encrypt_file
        injected = {"destination": "", "raised": False}

        def fail_candidate_after_partial_write(src, dest):
            dest = Path(dest)
            injected["destination"] = str(dest)
            dest.write_bytes(b"SYNTHETIC_PARTIAL_CANDIDATE")
            injected["raised"] = True
            raise OSError("synthetic disk-full during candidate encryption")

        server.VAULT.encrypt_file = fail_candidate_after_partial_write
        try:
            try:
                server._vault_encrypt_consistent_db()
            except OSError as exc:
                result["first_error"] = str(exc)
        finally:
            server.VAULT.encrypt_file = original_encrypt
        result["injected"] = injected
        result["ciphertext_preserved_after_failure"] = target.read_bytes() == previous
        result["candidate_residue_after_failure"] = sorted(
            path.name for path in Path(store.DATA_DIR).glob(".vault-snapshot-cipher-*.tmp"))
        result["retry_snapshot"] = server._vault_encrypt_consistent_db()
        result["retry_succeeded"] = server._dek_selftest().get("ok")

    elif operation == "restore-only":
        server.VAULT.load_meta()
        server.VAULT.unlock_with_password(os.environ["BFO_PROBE_PASSWORD"])
        result["restored"] = server._vault_restore_db_if_needed()
        result["database_after_restore"] = database_snapshot(Path(store.DB_FILE))

    elif operation == "partial-encryption-write":
        server.VAULT.initialize(bound_mid="9000001", password=os.environ["BFO_PROBE_PASSWORD"])
        target = Path(server.VAULT_DB_ENC)
        original_encrypt = server.VAULT.encrypt_file

        def fail_after_partial_write(src, dest):
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(b"synthetic-partial-encrypted-file")
            raise OSError("synthetic disk-full during encrypted write")

        server.VAULT.encrypt_file = fail_after_partial_write
        result["first_attempt"] = server._vault_migrate_plain_db_if_needed()
        server.VAULT.encrypt_file = original_encrypt
        result["partial_file_exists"] = target.is_file()
        result["candidate_files_after_failure"] = sorted(
            path.name for path in Path(store.DATA_DIR).glob(".vault-snapshot-cipher-*.tmp"))
        result["source_db_after_failure"] = database_snapshot(Path(store.DB_FILE))
        result["retry_attempt"] = server._vault_migrate_plain_db_if_needed()
        result["encrypted_file_after_retry"] = target.is_file()

    elif operation == "interrupt-copy":
        # Run the same location migration against separate temporary paths after the
        # normal import path has initialized. Inject an OSError at SQLite's backup call.
        source = Path(os.environ["BFO_COPY_SOURCE"])
        target_dir = Path(os.environ["BFO_COPY_TARGET_DIR"])
        target_db = target_dir / "library.sqlite3"
        target_dir.mkdir(parents=True, exist_ok=True)
        store.LEGACY_DB_FILE = source
        store.DATA_DIR = target_dir
        store.DB_FILE = target_db
        real_connect = store.sqlite3.connect
        injected = {"raised": False}

        class FailingBackupConnection:
            def __init__(self, connection):
                self._connection = connection

            def backup(self, _target):
                injected["raised"] = True
                raise OSError("synthetic interruption during SQLite backup")

            def __getattr__(self, name):
                return getattr(self._connection, name)

        def connect(path, *args, **kwargs):
            connection = real_connect(path, *args, **kwargs)
            if Path(path) == source:
                return FailingBackupConnection(connection)
            return connection

        store.sqlite3.connect = connect
        try:
            try:
                store._migrate_database_location()
            except OSError as exc:
                result["first_error"] = str(exc)
        finally:
            store.sqlite3.connect = real_connect
        result["injected"] = injected["raised"]
        result["target_exists_after_failure"] = target_db.exists()
        result["temporary_files_after_failure"] = [p.name for p in target_dir.glob("*.migrating")]
        result["source_after_failure"] = database_snapshot(source)
        store._migrate_database_location()
        result["target_exists_after_retry"] = target_db.exists()
        result["target_after_retry"] = database_snapshot(target_db)

    result["final_database"] = database_snapshot(Path(store.DB_FILE))
    result["database_file_exists_at_probe_end"] = store.DB_FILE.is_file()
    result["database_sidecars_at_probe_end"] = _database_sidecars()
    result["open_sqlite_descriptors_at_probe_end"] = _open_sqlite_descriptors()
    result["database_state"] = store.database_state()
    if any(
            isinstance(result.get(key), dict) and
            int(result[key].get("http_status", 0)) >= 500
            for key in ("unlock", "lock", "completion", "onboarding")):
        result["sqlite_path_trace"] = list(_SQLITE_PATH_TRACE)
    print("MIGRATION_PROBE:" + json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        # Keep the same event trace available when an exception escapes before
        # main() can serialize its normal probe result.
        print("MIGRATION_PROBE_DIAGNOSTIC:" + json.dumps({
            "exception": f"{type(exc).__name__}: {exc}",
            "sqlite_path_trace": list(_SQLITE_PATH_TRACE),
        }, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        raise
