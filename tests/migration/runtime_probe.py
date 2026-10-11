"""One-shot probe executed only in a copied source tree with temporary user paths."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

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
        return {"exists": False}
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
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


def _config_summary() -> dict:
    cfg = server.load_config()
    try:
        secrets_json = json.loads(server.SECRETS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        secrets_json = {}
    providers = store.list_providers()
    models = store.list_models()
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
        "migration_source_exists": (Path(server.APP_DIR) / "data" / "library.sqlite3").is_file(),
        "database": database_snapshot(Path(store.DB_FILE)),
        "config": _config_summary(),
    }

    if operation == "onboard":
        body = server.VaultInitIn(bound_mid="9000001", password=os.environ["BFO_PROBE_PASSWORD"])
        result["onboarding"] = _response_data(server.vault_onboarding(body))
        if result["onboarding"]["http_status"] == 200:
            result["recovery_code"] = result["onboarding"]["body"].get("recovery_code", "")
            result["completion"] = _response_data(server.vault_complete_onboarding())
            result["vault_locked_after_completion"] = not server.VAULT.is_unlocked()
            if server.VAULT_DB_ENC.is_file():
                decrypted = Path(os.environ["TEMP"]) / "decrypted-synthetic.sqlite3"
                server.VAULT.load_meta()
                server.VAULT.unlock_with_password(os.environ["BFO_PROBE_PASSWORD"])
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

    elif operation == "explicit-lock":
        server.VAULT.load_meta()
        server.VAULT.unlock_with_password(os.environ["BFO_PROBE_PASSWORD"])
        result["lock"] = _response_data(server.vault_lock())
        result["vault_locked"] = not server.VAULT.is_unlocked()
        result["plain_db_exists_after_lock"] = Path(store.DB_FILE).is_file()

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
    print("MIGRATION_PROBE:" + json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
