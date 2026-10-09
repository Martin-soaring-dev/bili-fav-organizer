# -*- coding: utf-8 -*-
"""B 站收藏夹智能整理 — 本地 Web 应用主程序。

启动:  python server.py [--port 8080]
打开:  http://127.0.0.1:8080/

四个阶段（前端分区操作，可分别独立运行）：
  1) 获取收藏明细    POST /api/scan
  2) LLM 分析        POST /api/analyze/start   + GET /api/analyze/status
  3) 预归类方案      GET  /api/plan  +  POST /api/plan/apply
  4) 确认执行        POST /api/apply/start     + GET /api/apply/status

LLM 配置写入 config.json（也可在前端 /api/config 修改）：
  { "base_url": "https://api.deepseek.com/v1", "api_key": "...", "model": "qwen3-8b" }
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sys
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional
from urllib.parse import urlsplit

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import vault as vault_mod

import bili_api
import llm_analyzer
import store

HERE = Path(__file__).resolve().parent
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else HERE
if os.environ.get("LOCALAPPDATA"):
    USER_DATA_DIR = Path(os.environ["LOCALAPPDATA"]) / "BiliFavOrganizer"
else:
    USER_DATA_DIR = Path.home() / ".local" / "share" / "BiliFavOrganizer"
CONFIG_FILE = USER_DATA_DIR / "config.json"
SECRETS_FILE = USER_DATA_DIR / "secrets.json"  # 凭据单独存放（api_key / cookie_string）
UPDATE_ERROR_FILE = USER_DATA_DIR / "update-error.json"  # 上次更新失败原因（临时目录会被清掉，所以存固定位置）
SECRET_KEYS = ("api_key", "cookie_string")
STATIC_DIR = HERE / "static"

LOG_FORMAT = "%(asctime)s [%(name)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("server")

app = FastAPI(title="B站收藏夹整理")

MODEL_TEST_TIMEOUT_SECONDS = 45

# 发布流程按下面三个标记注入真实构建信息（见 .github/workflows/release-windows.yml）。
# 版本那一行必须保持原样且全文件只出现一次，工作流替换失败会直接终止发布。
BUILD_VERSION = "dev"
BUILD_COMMIT = ""
BUILD_DATE = ""

GITHUB_REPOSITORY = "Martin-soaring-dev/bili-fav-organizer"
GITHUB_LATEST_RELEASE_URL = f"https://github.com/{GITHUB_REPOSITORY}/releases/latest"

# 署名与版权：界面、控制台、/api/version 与 exe 属性都会带上，
# 让被改名重打包的副本要么显示来源，要么必须显式删除这些字段。
AUTHOR = "Martin-soaring-dev"
HOMEPAGE = f"https://github.com/{GITHUB_REPOSITORY}"
LICENSE_NAME = "PolyForm Noncommercial License 1.0.0"
COPYRIGHT = f"© 2026 {AUTHOR}"
UPDATE_STATE = {
    "status": "idle",
    "current_version": "",
    "latest_version": "",
    "version_comparable": True,
    "downloaded_bytes": 0,
    "total_bytes": 0,
    "error": "",
}
_UPDATE_LOCK = threading.Lock()
_UPDATE_RELEASE = None
_UPDATE_CANCEL = threading.Event()   # 用户点「停止下载」时置位，下载循环据此中止
_SHUTDOWN = threading.Event()        # 更新替换前置位，用来断开 SSE 长连接
_UPDATE_STATUS_FILE: Path | None = None
_UVICORN_SERVER = None
_APP_PORT = 8080


class _UpdateCancelled(Exception):
    """用户主动取消更新下载（不是失败）。"""


def _force_exit_if_alive():
    """更新替换阶段的兜底硬退。

    只要还有一个 SSE 长连接没断，uvicorn 的优雅退出就可能一直等下去，
    更新助手会因"应用仍在运行"而超时取消——所以这里留一个硬退上限。
    正常情况下服务早已退出，走不到这一行。
    """
    log.warning("服务未在预期时间内退出，强制结束进程以完成更新替换")
    os._exit(0)


def _cleanup_stale_update_leftovers(app_parent: Path, system_temp: Path,
                                    min_age_hours: float = 2.0) -> int:
    """清掉更新失败/中断留下的临时目录；成功的路径由更新助手自己清。

    只删超过 min_age_hours 的，避免误删正在进行中的更新；只匹配这三个模式：
    便携更新的工作目录、旧程序备份目录、安装版更新的工作目录。
    """
    patterns = ((app_parent, ".bfo-update-*"),
                (app_parent, f"{APP_DIR.name}.previous-*"),
                (system_temp, ".bfo-setup-*"))
    removed = 0
    for base, pattern in patterns:
        try:
            candidates = [p for p in base.glob(pattern) if p.is_dir()]
        except OSError:
            continue
        for path in candidates:
            try:
                if time.time() - path.stat().st_mtime < min_age_hours * 3600:
                    continue
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
            except OSError:
                continue
    if removed:
        log.info("已清理 %d 个更新残留目录（超过 %g 小时未收尾）", removed, min_age_hours)
    return removed


def _app_version() -> str:
    """The release workflow embeds its tag into the frozen application."""
    return BUILD_VERSION


INSTALL_REGISTRY_KEY = r"Software\Martin-soaring-dev\BiliFavOrganizer"


def _installed_info() -> dict | None:
    """安装版标记（由安装包写入注册表）；便携版没有，返回 None。

    安装版更新时改为"下载安装包并静默运行"，便携版仍走 ZIP 覆盖。
    """
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, INSTALL_REGISTRY_KEY) as key:
                mode = str(winreg.QueryValueEx(key, "InstallMode")[0] or "").lower()
                if mode != "installed":
                    continue

                def read(name: str, default: str = "") -> str:
                    try:
                        return str(winreg.QueryValueEx(key, name)[0] or default)
                    except OSError:
                        return default

                return {
                    "install_dir": read("InstallDir"),
                    "scope": read("Scope", "current-user"),
                    "version": read("Version"),
                }
        except OSError:
            continue
    return None


def _log_attribution():
    """在控制台窗口与日志里留一条署名记录。

    便携版按说明会一直开着这个控制台窗口，署名因此始终可见；
    改名重打包的副本若要去掉它，必须显式改源码。
    """
    build = f"（提交 {BUILD_COMMIT}，{BUILD_DATE}）" if BUILD_COMMIT else ""
    log.info("BiliFavOrganizer %s%s", _app_version(), build)
    log.info("%s · %s · 许可 %s（保留署名与来源，不得商用）",
             COPYRIGHT, HOMEPAGE, LICENSE_NAME)
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(
                f"BiliFavOrganizer {_app_version()} · {AUTHOR}")
        except Exception:
            pass


def _version_key(version: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"[vV]?(\d+(?:\.\d+)*)", str(version or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _is_newer_version(latest: str, current: str) -> bool:
    latest_key, current_key = _version_key(latest), _version_key(current)
    if latest_key is None:
        raise ValueError(f"Release 版本标签格式不支持：{latest}")
    if current_key is None:
        return True
    width = max(len(latest_key), len(current_key))
    return latest_key + (0,) * (width - len(latest_key)) > \
        current_key + (0,) * (width - len(current_key))


def _write_update_error(message: str) -> None:
    """把更新失败原因写到固定文件。

    临时目录在收尾时会被整套删掉、进程也可能被替换流程重启，
    所以内存里的错误（以及助手里的错误）必须落到固定位置才有人看得到。
    """
    try:
        UPDATE_ERROR_FILE.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(UPDATE_ERROR_FILE, {"status": "error", "error": message})
    except OSError as exc:
        log.warning("无法记录更新失败原因：%s", exc)


def _update_state_snapshot() -> dict:
    with _UPDATE_LOCK:
        state = dict(UPDATE_STATE)
        status_file = _UPDATE_STATUS_FILE
    if status_file and status_file.is_file():
        try:
            helper_state = json.loads(status_file.read_text(encoding="utf-8-sig"))
            if helper_state.get("status") == "error":
                state.update(helper_state)
        except (OSError, ValueError, AttributeError):
            pass
    if not state.get("error") and UPDATE_ERROR_FILE.is_file():
        try:
            saved = json.loads(UPDATE_ERROR_FILE.read_text(encoding="utf-8-sig"))
            if isinstance(saved, dict) and saved.get("error"):
                state.update({"status": saved.get("status", "error"), "error": str(saved["error"])})
        except (OSError, ValueError, AttributeError):
            pass
    return state

# ---------- 运行状态（跨请求的全局状态） ----------
APP = {
    "session": None,          # bili_api.BiliSession
    "folders": [],            # 现有收藏夹清单
    "scan_run": None,
    "analyze_run": None,      # {running, done, total, failed, stop}
    "apply_run": None,
    "folder_merge_run": None,
    "folder_merge_ai_run": None,
    "folder_profile_run": None,
    "events": [],             # 事件缓冲(带自增 id)，供 SSE 推送
}
ANALYZE_WAKE = threading.Event()
_scan_start_lock = threading.Lock()
_evt_lock = threading.Lock()
_evt_seq = [0]
_JOB_START_LOCK = threading.Lock()
LONG_JOBS = ("scan_run", "analyze_run", "apply_run",
             "folder_merge_run", "folder_merge_ai_run", "folder_profile_run")

# 进程级本地 API token：内存持有；DPAPI blob 只作同 Windows 用户的恢复材料
_LOCAL_API_TOKEN = secrets.token_urlsafe(32)
_TOKEN_BLOB = USER_DATA_DIR / "token.blob"


def _insecure_local() -> bool:
    return os.environ.get("BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL", "").strip() == "1"


def _is_test_client_host(host: str) -> bool:
    return (host or "").split(":")[0].lower() == "testserver"


def _host_allowed(host: str) -> bool:
    hostname = (host or "").split(":")[0].strip().lower()
    if hostname.startswith("["):  # [::1]:port
        hostname = hostname.split("]")[0].lstrip("[")
    allowed = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}
    # 仅测试钩子打开时才接受 TestClient 的 Host，生产绝不因 Host 值放行
    if _insecure_local():
        allowed.add("testserver")
    return hostname in allowed


def _origin_allowed(origin: str, port: int | None) -> bool:
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme != "http":
        return False
    hostname = (parts.hostname or "").lower()
    if hostname not in ("127.0.0.1", "localhost", "::1"):
        return False
    origin_port = parts.port or (443 if parts.scheme == "https" else 80)
    expect = int(port or _APP_PORT)
    return int(origin_port) == expect


def _token_from_request(request) -> str:
    raw = request.headers.get("x-bilifav-token", "")
    if raw:
        return raw.strip()
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _token_ok(request) -> bool:
    provided = _token_from_request(request)
    return bool(provided) and secrets.compare_digest(provided, _LOCAL_API_TOKEN)


# ---- DPAPI（Windows 用户绑定）；非 Windows 回退为明文 blob 文件 ----
def _dpapi_protect(data: bytes) -> bytes:
    if sys.platform != "win32":
        return b"PLAIN\x00" + data
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_buf = ctypes.create_string_buffer(data)
    in_blob = DATA_BLOB(len(data), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_byte)))
    out_blob = DATA_BLOB()
    if not crypt32.CryptProtectData(
            ctypes.byref(in_blob), "BiliFavOrganizer", None, None, None, 0,
            ctypes.byref(out_blob)):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(blob: bytes) -> bytes:
    if blob.startswith(b"PLAIN\x00"):
        return blob[6:]
    if sys.platform != "win32":
        raise OSError("DPAPI blob 只能在原 Windows 用户环境下解密")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_buf = ctypes.create_string_buffer(blob)
    in_blob = DATA_BLOB(len(blob), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_byte)))
    out_blob = DATA_BLOB()
    if not crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), None, None, None, None, 0,
            ctypes.byref(out_blob)):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _persist_api_token() -> None:
    """把当前 token 用 DPAPI 绑到 Windows 用户后落盘（供同用户恢复，非明文）。"""
    try:
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        _TOKEN_BLOB.write_bytes(_dpapi_protect(_LOCAL_API_TOKEN.encode("utf-8")))
    except OSError as exc:
        log.warning("无法写入 token.blob：%s", exc)


def _load_persisted_token() -> str | None:
    """读回 token.blob（DPAPI 解密）。失败返回 None，不覆盖进程内 token。"""
    try:
        return _dpapi_unprotect(_TOKEN_BLOB.read_bytes()).decode("utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return None


# ---------- 应用密码（手动解锁；可选 DPAPI 本机自动解锁） ----------
APP_UNLOCK_BLOB = USER_DATA_DIR / "app_unlock.blob"
_APP_LOCK = {"enabled": False, "unlocked": True, "failed_attempts": 0}
_PBKDF2_ITERS = 200_000
_APP_LOCK_OPEN_PATHS = ("/api/app-lock/", "/static/", "/api/version", "/api/login/",
                        "/api/cookie", "/api/prompts/defaults", "/api/data/paths",
                        "/api/data/usage", "/api/vault/hello-verify")


def _app_lock_secrets() -> dict:
    data, _ = _read_json_file(SECRETS_FILE)
    return {
        "salt": str(data.get("app_lock_salt") or ""),
        "hash": str(data.get("app_lock_hash") or ""),
    }


def _save_app_lock_secrets(salt: str, hash_hex: str) -> None:
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    prev, _ = _read_json_file(SECRETS_FILE)
    if hash_hex:
        prev["app_lock_salt"] = salt
        prev["app_lock_hash"] = hash_hex
    else:
        prev.pop("app_lock_salt", None)
        prev.pop("app_lock_hash", None)
    _atomic_write(SECRETS_FILE, prev)


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS).hex()


def _password_matches(password: str) -> bool:
    sec = _app_lock_secrets()
    if not sec["hash"] or not sec["salt"]:
        return False
    try:
        salt = bytes.fromhex(sec["salt"])
    except ValueError:
        return False
    expected = sec["hash"]
    return secrets.compare_digest(_hash_password(password, salt), expected)


def _set_auto_unlock(enabled: bool) -> None:
    if enabled:
        try:
            USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
            APP_UNLOCK_BLOB.write_bytes(_dpapi_protect(b"app-unlock"))
        except OSError as exc:
            log.warning("无法写入本机自动解锁材料：%s", exc)
    else:
        try:
            APP_UNLOCK_BLOB.unlink(missing_ok=True)
        except OSError:
            pass


def _try_auto_unlock() -> bool:
    try:
        raw = APP_UNLOCK_BLOB.read_bytes()
    except OSError:
        return False
    try:
        return _dpapi_unprotect(raw) == b"app-unlock"
    except (OSError, ValueError):
        return False


def _app_lock_status() -> dict:
    return {
        "password_set": _APP_LOCK["enabled"],
        "unlocked": _APP_LOCK["unlocked"],
        "auto_unlock_available": APP_UNLOCK_BLOB.is_file(),
        "failed_attempts": _APP_LOCK["failed_attempts"],
    }


def _app_lock_refresh() -> None:
    """按 secrets.json 刷新进程内锁定状态（启动或修改密码后调用）。"""
    sec = _app_lock_secrets()
    _APP_LOCK["enabled"] = bool(sec["hash"] and sec["salt"])
    if not _APP_LOCK["enabled"]:
        _APP_LOCK["unlocked"] = True
        _APP_LOCK["failed_attempts"] = 0
        _set_auto_unlock(False)
        return
    if not _APP_LOCK["unlocked"]:
        _APP_LOCK["unlocked"] = _try_auto_unlock()


class AppLockPasswordIn(BaseModel):
    password: str = ""
    current_password: str = ""
    auto_unlock: bool = False


@app.get("/api/app-lock/status")
def app_lock_status():
    return _app_lock_status()


@app.post("/api/app-lock/set-password")
def app_lock_set_password(body: AppLockPasswordIn):
    password = body.password or ""
    if len(password) < 6:
        return JSONResponse({"ok": False, "error": "应用密码至少 6 位"}, status_code=400)
    if _APP_LOCK["enabled"]:
        if not _password_matches(body.current_password or ""):
            return JSONResponse({"ok": False, "error": "当前应用密码不正确"}, status_code=403)
    salt = secrets.token_bytes(16)
    _save_app_lock_secrets(salt.hex(), _hash_password(password, salt))
    _APP_LOCK["enabled"] = True
    _APP_LOCK["unlocked"] = True
    _APP_LOCK["failed_attempts"] = 0
    _set_auto_unlock(bool(body.auto_unlock))
    emit("ok", "已设置应用密码")
    return {"ok": True, **_app_lock_status()}


@app.post("/api/app-lock/unlock")
def app_lock_unlock(body: AppLockPasswordIn):
    if not _APP_LOCK["enabled"]:
        _APP_LOCK["unlocked"] = True
        return {"ok": True, **_app_lock_status()}
    if not _password_matches(body.password or ""):
        _APP_LOCK["failed_attempts"] += 1
        return JSONResponse({"ok": False, "error": "应用密码不正确",
                             "failed_attempts": _APP_LOCK["failed_attempts"]},
                            status_code=403)
    _APP_LOCK["unlocked"] = True
    _APP_LOCK["failed_attempts"] = 0
    if body.auto_unlock:
        _set_auto_unlock(True)
    return {"ok": True, **_app_lock_status()}


@app.post("/api/app-lock/lock")
def app_lock_lock():
    if _APP_LOCK["enabled"]:
        _APP_LOCK["unlocked"] = False
        _set_auto_unlock(False)
    return {"ok": True, **_app_lock_status()}


@app.post("/api/app-lock/remove")
def app_lock_remove(body: AppLockPasswordIn):
    if not _APP_LOCK["enabled"]:
        return {"ok": True, **_app_lock_status()}
    if not _password_matches(body.current_password or ""):
        return JSONResponse({"ok": False, "error": "当前应用密码不正确"}, status_code=403)
    _save_app_lock_secrets("", "")
    _set_auto_unlock(False)
    _APP_LOCK.update(enabled=False, unlocked=True, failed_attempts=0)
    emit("warn", "已移除应用密码")
    return {"ok": True, **_app_lock_status()}


# ---------- Secure Vault（DEK 多 KEK） ----------
VAULT = vault_mod.Vault(USER_DATA_DIR / "vault.json")
VAULT_DB_ENC = store.DATA_DIR / "library.sqlite3.enc"


def _vault_status_payload() -> dict:
    st = VAULT.state
    return {
        "configured": st.password_set,
        # 未配置金库 = 旧版开放模式，视为已解锁
        "unlocked": st.unlocked or not st.password_set,
        "onboarding_complete": st.onboarding_complete,
        "bound_mid": st.bound_mid,
        "has_recovery_wrap": st.has_recovery_wrap,
        "has_device_wrap": st.has_device_wrap,
        "has_account_wrap": st.has_account_wrap,
    }


def _vault_save_db_encrypted() -> None:
    """把当前 SQLite 库整库 AEAD 落盘。"""
    src = store.DB_FILE
    if not src.is_file() or not VAULT.is_unlocked():
        return
    VAULT.encrypt_file(src, VAULT_DB_ENC)


def _vault_migrate_plain_db_if_needed() -> str | None:
    """老用户明文库迁移：mid 一致则继承并加密。返回说明或 None。"""
    src = store.DB_FILE
    enc = VAULT_DB_ENC
    if not src.is_file() or enc.is_file() or not VAULT.is_unlocked():
        return None
    # 若金库已绑定 mid，且 cookie 中 mid 不一致则拒绝导入
    bound = str(VAULT.state.bound_mid or "")
    if bound:
        cfg = load_config()
        ck = cfg.get("cookie_string", "") or ""
        cookie_mid = ""
        for part in ck.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "DedeUserID":
                cookie_mid = v.strip()
                break
        if cookie_mid and cookie_mid != bound:
            return "检测到其它账号的明文数据，未导入"
    try:
        VAULT.encrypt_file(src, enc)
        backup = src.with_suffix(".sqlite3.plain-backup")
        try:
            if not backup.exists():
                shutil.copy2(src, backup)
        except OSError:
            pass
        return "已继承本地数据并完成加密"
    except (OSError, vault_mod.VaultError) as exc:
        log.warning("明文库迁移失败：%s", exc)
        return None


def _vault_restore_db_if_needed() -> bool:
    """若只有密文库且金库已解锁，则解出工作库。"""
    if store.DB_FILE.is_file() or not VAULT_DB_ENC.is_file() or not VAULT.is_unlocked():
        return False
    try:
        VAULT.decrypt_file(VAULT_DB_ENC, store.DB_FILE)
        return True
    except vault_mod.VaultError as exc:
        log.warning("无法解密数据库：%s", exc)
        return False


class VaultInitIn(BaseModel):
    bound_mid: str = ""
    password: str = ""
    recovery_code: str = ""


class VaultUnlockIn(BaseModel):
    password: str = ""
    recovery_code: str = ""
    bound_mid: str = ""


@app.get("/api/vault/status")
def vault_status():
    return _vault_status_payload()


@app.post("/api/vault/onboarding")
def vault_onboarding(body: VaultInitIn):
    """新用户：绑定 mid + 设密码，返回一次性恢复码明文。"""
    password = body.password or ""
    if len(password) < 6:
        return JSONResponse({"ok": False, "error": "应用密码至少 6 位"}, status_code=400)
    if not body.bound_mid:
        return JSONResponse({"ok": False, "error": "缺少绑定的 B 站用户 ID"}, status_code=400)
    try:
        code = VAULT.initialize(bound_mid=body.bound_mid, password=password)
    except vault_mod.VaultError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    migrated = _vault_migrate_plain_db_if_needed()
    _save_app_lock_secrets("", "")
    _APP_LOCK.update(enabled=False, unlocked=True)
    emit("ok", "本地加密已启用，请保存恢复码")
    return {"ok": True, "recovery_code": code, "migrated": migrated,
            **_vault_status_payload()}


@app.post("/api/vault/unlock-device")
def vault_unlock_device():
    """Windows Hello 验证通过后，用 DPAPI 中的设备材料解 DEK。"""
    if not VAULT.state.has_device_wrap:
        return JSONResponse({"ok": False, "error": "未绑定本机验证，请使用应用密码"},
                            status_code=400)
    hello = vault_hello_verify()
    if not hello.get("ok"):
        return JSONResponse({"ok": False, "error": hello.get("message") or "验证失败"},
                            status_code=403)
    try:
        secret = _dpapi_unprotect(APP_UNLOCK_BLOB.read_bytes()).decode("utf-8")
        VAULT.unlock_with_secret("device", secret)
    except (OSError, ValueError, vault_mod.VaultError) as exc:
        return JSONResponse({"ok": False, "error": f"设备材料不可用：{exc}"}, status_code=400)
    _APP_LOCK.update(unlocked=True)
    return {"ok": True, **_vault_status_payload()}


@app.post("/api/vault/unlock")
def vault_unlock(body: VaultUnlockIn):
    try:
        if body.recovery_code:
            VAULT.unlock_with_recovery(body.recovery_code)
            if body.password:
                VAULT.set_password(body.password)
        elif body.password:
            VAULT.unlock_with_password(body.password)
        else:
            return JSONResponse({"ok": False, "error": "请输入应用密码或恢复码"}, status_code=400)
    except vault_mod.VaultError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
    restored = _vault_restore_db_if_needed()
    migrated = _vault_migrate_plain_db_if_needed()
    _APP_LOCK.update(enabled=False, unlocked=True)
    return {"ok": True, "db_restored": restored, "migrated": migrated,
            **_vault_status_payload()}


@app.post("/api/vault/lock")
def vault_lock():
    try:
        _vault_save_db_encrypted()
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"保存加密库失败：{exc}"},
                            status_code=500)
    VAULT.lock()
    return {"ok": True, **_vault_status_payload()}


@app.post("/api/vault/complete-onboarding")
def vault_complete_onboarding():
    if not VAULT.is_unlocked():
        return JSONResponse({"ok": False, "error": "金库未解锁"}, status_code=403)
    VAULT.complete_onboarding()
    # 初始化完成后立刻上锁并加密落盘：刷新/重启后必须解锁才能用
    try:
        _vault_save_db_encrypted()
    except OSError as exc:
        log.warning("初始化后加密数据库失败：%s", exc)
    VAULT.lock()
    return {"ok": True, "locked": True, **_vault_status_payload()}


class VaultSetPasswordIn(BaseModel):
    current_password: str = ""
    password: str = ""


@app.post("/api/vault/set-password")
def vault_set_password(body: VaultSetPasswordIn):
    password = body.password or ""
    if len(password) < 6:
        return JSONResponse({"ok": False, "error": "应用密码至少 6 位"}, status_code=400)
    try:
        if VAULT.is_unlocked():
            VAULT.set_password(password)
        else:
            VAULT.set_password(password, current_password=body.current_password or None)
    except vault_mod.VaultError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=403)
    return {"ok": True, **_vault_status_payload()}


@app.post("/api/vault/bind-device")
def vault_bind_device():
    """绑定 Windows/DPAPI 设备包装（须先有应用密码并解锁）。"""
    if not VAULT.state.password_set:
        return JSONResponse({"ok": False, "error": "请先设置应用密码"}, status_code=400)
    if not VAULT.is_unlocked():
        return JSONResponse({"ok": False, "error": "请先解锁金库"}, status_code=403)
    secret = secrets.token_urlsafe(32)
    try:
        VAULT.bind_device_blob(secret)
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        APP_UNLOCK_BLOB.write_bytes(_dpapi_protect(secret.encode("utf-8")))
    except (vault_mod.VaultError, OSError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return {"ok": True, **_vault_status_payload()}


@app.post("/api/vault/hello-verify")
def vault_hello_verify():
    """调用 Windows Hello / PIN（UserConsentVerifier）。取消或失败返回明确状态。"""
    if sys.platform != "win32":
        return {"ok": False, "status": "unsupported", "message": "当前系统不支持 Windows Hello"}
    ps = r'''
$ErrorActionPreference = 'Stop'
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $null = [Windows.Security.Credentials.UI.UserConsentVerifier,Windows.Security.Credentials.UI,ContentType=WindowsRuntime]
  $null = [Windows.Security.Credentials.UI.ConsentResult,Windows.Security.Credentials.UI,ContentType=WindowsRuntime]

  # IAsyncOperation`1 → Task 的 AsTask 扩展
  $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1
  } | Select-Object -First 1
  if (-not $asTask) { Write-Output 'unsupported:AsTask扩展不可用'; exit 0 }

  $op = [Windows.Security.Credentials.UI.UserConsentVerifier]::VerifyAsync('验证以解锁本机数据')
  $generic = $asTask.MakeGenericMethod($op.GetType().GetGenericArguments()[0])
  $task = $generic.Invoke($null, @($op))
  if (-not $task.Wait(30000)) { Write-Output 'cancel'; exit 0 }

  $result = $task.Result
  # ConsentResult 枚举
  $name = $result.ToString()
  if ($name -eq 'Allow') { Write-Output 'ok'; exit 0 }
  if ($name -eq 'UserCancelled') { Write-Output 'cancel'; exit 0 }
  Write-Output ("deny:" + $name)
} catch {
  $msg = $_.Exception.Message -replace '[\r\n]+', ' '
  if ($msg -match 'not contain a method') { Write-Output ('unsupported:' + $msg) }
  else { Write-Output ('unsupported:' + $msg) }
}
'''
    try:
        # 必须允许交互：Windows Hello 需要弹出系统对话框
        out = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True, text=True, timeout=35, encoding="utf-8", errors="replace",
        )
        text = (out.stdout or "").strip().splitlines()[-1] if out.stdout else ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "status": "error", "message": str(exc)}
    if text == "ok":
        return {"ok": True, "status": "ok", "message": "验证通过"}
    if text == "cancel":
        return {"ok": False, "status": "cancel", "message": "已取消，请改用应用密码"}
    if text.startswith("unsupported"):
        detail = text.split(":", 1)[1] if ":" in text else ""
        msg = "无法使用 Windows Hello：未启用 PIN/人脸/指纹，或系统接口不可用"
        if detail:
            msg += f"（{detail}）"
        return {"ok": False, "status": "unsupported", "message": msg}
    return {"ok": False, "status": "unsupported",
            "message": text or "Windows Hello 不可用，请改用应用密码解锁"}


def _begin_job(key: str, state: dict, *, error: str = "") -> JSONResponse | None:
    """在锁内占坑：长任务两两互斥，避免 check-then-act 双开。"""
    with _JOB_START_LOCK:
        for other in LONG_JOBS:
            if other == key:
                continue
            if (APP.get(other) or {}).get("running"):
                return JSONResponse(
                    {"ok": False, "error": error or f"已有任务在运行（{other}），请先停止"},
                    status_code=409)
        if (APP.get(key) or {}).get("running"):
            return JSONResponse({"ok": False, "error": error or "任务已在运行中"},
                                status_code=409)
        APP[key] = state
        return None


def emit(level: str, text: str, **extra):
    """追加一条事件到缓冲（供 SSE 推送）。level: info/ok/warn/err。"""
    with _evt_lock:
        _evt_seq[0] += 1
        APP["events"].append({"id": _evt_seq[0], "t": time.strftime("%H:%M:%S"),
                              "level": level, "text": text, **extra})
        # 控制缓冲长度
        if len(APP["events"]) > 2000:
            del APP["events"][:500]


# 把事件钩子注入 bili_api，让长等待(412 退避等)提示也能推到前端
bili_api.EVENT_HOOK = lambda level, text: emit(level, text)
bili_api.WRITE_LOG_PATH = store.DATA_DIR / "write_operations.jsonl"


DEFAULT_CONFIG = {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "qwen3-8b",
    "active_model_id": "",
    "scan_interval": 2,         # 收藏夹请求最小间隔(秒)
    "analyze_concurrency": 1,   # LLM 分析并发数(仅影响模型请求)，1~4
    "analyze_batch": 20,        # 逻辑批大小上限，1~1000；发送前按上下文预算拆分
    "analyze_max_tokens": 32768,  # 单次输出上限；部分模型将思考计入此限制
    "model_context_tokens": 32768, # 模型上下文总长度，用于估算画像可容纳样本
    "model_tpm_limit": 20000, # 模型每分钟 token 上限；用于画像分页和本地限流
    "profile_request_interval": 2.0, # 画像分批请求的最小间隔，减少模型接口限流
    "scan_scope": "all",        # 扫描范围: all=全部收藏夹 / default=仅默认收藏夹
    "write_interval": 2,        # 执行阶段的写操作固定间隔(秒)
    "folder_merge_interval": 2,
    "apply_batch": 1000,        # 批量 move / batch-del 的单批条数，硬上限 1000
}

DEFAULT_FAVORITE_NAME = "默认收藏夹"


def _folder_attr_is_default(folder: dict) -> bool | None:
    """attr 位域 bit1=0 表示默认收藏夹；没有 attr 时返回 None（未知）。

    位域约定：bit0=是否私有，bit1=0 默认收藏夹 / 1 非默认收藏夹。
    """
    attr = folder.get("attr") if isinstance(folder, dict) else None
    if attr is None:
        return None
    try:
        return (int(attr) & 2) == 0
    except (TypeError, ValueError):
        return None


def _default_folder(folders: list | None = None) -> dict | None:
    """找出唯一的「默认收藏夹」：优先标题，其次 attr 位域（bit1=0）。

    只在能**明确**识别时返回，否则返回 None：既不会把所有收藏夹误判成默认夹，
    也不会因为「目录里只有一个夹」就把它当成默认夹而拒绝往里移入内容。
    """
    rows = store.load_folders() if folders is None else folders
    if not rows:
        return None
    for f in rows:
        if (f.get("title") or "").strip() == DEFAULT_FAVORITE_NAME:
            return f
    flagged = [f for f in rows if _folder_attr_is_default(f) is True]
    return flagged[0] if len(flagged) == 1 else None


def _default_folder_ids(folders: list | None = None) -> set[str]:
    row = _default_folder(folders)
    return {str(row["media_id"])} if row else set()


def _default_folder_names(folders: list | None = None) -> set[str]:
    """默认收藏夹当前的名称，用作「禁止移入」的精确匹配串。

    识别不出默认收藏夹时返回空集合：宁可不拦，也不要误拦正常的移入。
    """
    row = _default_folder(folders)
    title = str((row or {}).get("title") or "").strip()
    return {title} if title else set()


def _default_folder_destination_error(items: list[dict]) -> str:
    """拒绝把内容移入或新建为默认收藏夹。"""
    forbidden = _default_folder_names()
    for item in items:
        action = str(item.get("action", "skip"))
        target = str(item.get("target_folder", "")).strip()
        new_name = str(item.get("create_new_name") or target).strip()
        if action == "move_to_existing" and target in forbidden:
            return "默认收藏夹只能移出，不能作为内容整理的移入目标；请修改该条方案"
        if action == "create_new" and new_name in forbidden:
            return "默认收藏夹只能移出，不能作为新建或移入目标；请修改该条方案"
    return ""

# 常用 OpenAI 兼容供应商预设（URL 可在管理界面修改）
PROVIDER_PRESETS = [
    {"key": "siliconflow", "name": "SiliconFlow", "base_url": "https://api.siliconflow.cn/v1"},
    {"key": "qwen_token", "name": "千问 Token Plan", "base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"},
    {"key": "qwen_payg", "name": "千问按量付费", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    {"key": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1"},
    {"key": "mimo_token", "name": "MiMo Token Plan", "base_url": "https://token-plan-cn.xiaomimimo.com/v1"},
    {"key": "mimo_payg", "name": "MiMo 按量付费", "base_url": "https://api.xiaomimimo.com/v1"},
    {"key": "digitalocean", "name": "Digital Ocean", "base_url": "https://inference.do-ai.run/v1"},
    {"key": "amd_token_factory", "name": "AMD Token Factory", "base_url": "https://developer.amd.com.cn/radeon/api/v1"},
    {"key": "custom", "name": "其他", "base_url": ""},
]


# ============ 配置 ============
def _atomic_write(path: Path, obj):
    """原子写：先写 .tmp 再替换，避免读到半截 JSON。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json_file(path: Path):
    """返回 (数据, 错误信息)；文件不存在返回 ({}, None)。"""
    if not path.exists():
        return {}, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as e:
        return {}, str(e)


def _migrate_legacy_settings():
    """首次启动时把程序目录中的旧配置复制到用户数据目录，不覆盖已有配置。"""
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "secrets.json"):
        legacy = APP_DIR / name
        destination = USER_DATA_DIR / name
        if not destination.exists() and legacy.is_file():
            try:
                shutil.copy2(legacy, destination)
                log.info("已迁移旧设置文件到用户数据目录：%s", destination)
            except Exception as e:
                log.warning("无法迁移旧设置文件 %s：%s", legacy, e)


def _mask(v: str) -> str:
    """凭据脱敏：只保留尾 4 位。"""
    v = v or ""
    return ("****" + v[-4:]) if len(v) > 4 else ("****" if v else "")


def load_config() -> dict:
    """读取配置（非凭据来自 config.json，凭据来自 secrets.json）。

    解析失败不再静默回退默认值，而是打日志并置 _config_broken 标记。
    """
    _migrate_legacy_settings()
    cfg = dict(DEFAULT_CONFIG)
    cdata, cerr = _read_json_file(CONFIG_FILE)
    sdata, serr = _read_json_file(SECRETS_FILE)

    # 迁移：老版本把凭据写在 config.json 里
    if not sdata:
        legacy = {k: cdata.get(k) for k in SECRET_KEYS if cdata.get(k)}
        if legacy:
            try:
                _atomic_write(SECRETS_FILE, legacy)
                for k in SECRET_KEYS:
                    cdata.pop(k, None)
                _atomic_write(CONFIG_FILE, cdata)
                sdata = legacy
                log.info("已把凭据从 config.json 迁移到 secrets.json")
            except Exception as e:
                log.warning("凭据迁移失败：%s", e)

    cfg.update({k: v for k, v in cdata.items() if k not in SECRET_KEYS})
    cfg.update({k: v for k, v in sdata.items() if k in SECRET_KEYS})
    if cerr or serr:
        log.error("配置解析失败：config=%s secrets=%s", cerr, serr)
        cfg["_config_broken"] = True
    return cfg


def save_config(cur: dict):
    """拆分保存：非凭据 → config.json；凭据 → secrets.json。均原子写。"""
    pub = {k: v for k, v in cur.items()
           if k not in SECRET_KEYS and not k.startswith("_")}
    # API Key 归 SQLite providers 表管理；secrets.json 只保留 B 站 Cookie。
    sec = {"cookie_string": cur["cookie_string"]} if cur.get("cookie_string") else {}
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(CONFIG_FILE, pub)
    prev, _ = _read_json_file(SECRETS_FILE)
    if store.list_providers():
        prev.pop("api_key", None)
    prev.update(sec)
    _atomic_write(SECRETS_FILE, prev)


def _thinking_params(effort: str) -> dict:
    effort = (effort or "").strip().lower()
    if not effort or effort in ("default", "auto"):
        return {}
    if effort in ("off", "none", "disable", "disabled"):
        return {"enable_thinking": False}
    params: dict = {"enable_thinking": True}
    if effort in ("low", "medium", "high", "max"):
        params["reasoning_effort"] = effort
    elif effort == "xhigh":
        params["reasoning_effort"] = "max"
    return params


def _build_model_test_payload(config: llm_analyzer.LLMConfig) -> dict:
    """Build the same short JSON probe for both model-test entry points."""
    return llm_analyzer.build_chat_completion_payload(
        config,
        messages=[
            {"role": "system", "content": "Return only a valid JSON object."},
            {"role": "user", "content": 'Return exactly {"ok":true}.'},
        ],
        max_tokens=256,
        structured_output=True,
    )


def resolved_llm_settings(cfg: dict | None = None) -> dict:
    """合并激活模型（SQLite）与全局配置，返回实际用于调用 LLM 的设置。"""
    out = dict(cfg if cfg is not None else load_config())
    model_id = str(out.get("active_model_id") or "").strip()
    if not model_id:
        return out
    model = store.get_model(model_id)
    if not model:
        return out
    provider = store.get_provider(model.get("provider_id") or "")
    if provider:
        out["base_url"] = provider.get("base_url") or out.get("base_url") or ""
        out["api_key"] = provider.get("api_key") or ""
        out["model"] = model.get("name") or out.get("model") or ""
        out["_provider_id"] = provider.get("id")
        out["_provider_name"] = provider.get("name")
        out["model_capabilities"] = dict(model.get("capabilities") or {})
        # 模型记录的规格是权威来源：只要模型上写了值就采用，
        # 不再按 context_source/output_source 的来源标记放行，避免界面兜底值
        # 长期压过模型真实规格（例如把上下文抬到 1,000,000）。
        if int(model.get("context_tokens") or 0) > 0:
            out["model_context_tokens"] = int(model["context_tokens"])
        if int(model.get("max_output_tokens") or 0) > 0:
            out["analyze_max_tokens"] = llm_analyzer.cap_completion_tokens(
                provider.get("base_url") or "", model.get("name") or "",
                int(model["max_output_tokens"]))
        out["thinking_effort"] = model.get("thinking_effort") or ""
        out["llm_params"] = _thinking_params(out.get("thinking_effort", ""))
    return out


def make_llm_config(cfg: dict | None = None) -> llm_analyzer.LLMConfig:
    resolved = resolved_llm_settings(cfg)
    return llm_analyzer.LLMConfig(
        base_url=resolved.get("base_url", ""),
        api_key=resolved.get("api_key", ""),
        model=resolved.get("model", "qwen3-8b"),
        params=dict(resolved.get("llm_params") or {}),
        max_tokens=int(resolved.get("analyze_max_tokens", 32768) or 32768),
        context_window_tokens=int(resolved.get("model_context_tokens", 32768) or 32768),
        provider_name=resolved.get("_provider_name", ""),
        capabilities=dict(resolved.get("model_capabilities") or {}))


def _bootstrap_providers_from_legacy_config() -> None:
    """首次启动：把 config.json 里的 base_url/api_key/model 迁入 SQLite 供应商与模型。"""
    try:
        cfg = load_config()
        providers = store.list_providers()
        active_model = store.get_model(str(cfg.get("active_model_id") or ""))
        migrated_key = not bool(cfg.get("api_key"))

        # Migrate a legacy credential to the provider that matches its old URL.
        # If a provider already has another key, preserve the legacy key as its own
        # provider instead of silently discarding or overwriting either credential.
        legacy_key = str(cfg.get("api_key") or "")
        base_url = (cfg.get("base_url") or "").strip().rstrip("/")
        if legacy_key:
            matching = next((p for p in providers
                             if (p.get("base_url") or "").strip().rstrip("/") == base_url), None)
            if matching and (not matching.get("api_key") or matching.get("api_key") == legacy_key):
                if not matching.get("api_key"):
                    store.update_provider(matching["id"], api_key=legacy_key)
                migrated_key = True
            else:
                legacy_provider = store.create_provider(
                    "默认（旧配置迁移）", base_url or (matching or {}).get("base_url", ""), legacy_key)
                legacy_model = store.create_model(
                    legacy_provider["id"], (cfg.get("model") or "qwen3-8b").strip(),
                    context_tokens=int(cfg.get("model_context_tokens") or 32768),
                    max_output_tokens=int(cfg.get("analyze_max_tokens") or 8192),
                    context_source="legacy_config", output_source="legacy_config",
                    thinking_effort=str(cfg.get("thinking_effort") or ""))
                if not active_model:
                    cfg["active_model_id"] = legacy_model["id"]
                    active_model = legacy_model
                migrated_key = True

        if not providers and not active_model:
            # No legacy provider exists. Register the existing config URL/model even
            # when no key was set so the connection screen can edit it in place.
            base_url = base_url or "https://api.deepseek.com/v1"
            provider = store.create_provider("默认（配置迁移）", base_url, "")
            model = store.create_model(
                provider["id"], (cfg.get("model") or "qwen3-8b").strip(),
                context_tokens=int(cfg.get("model_context_tokens") or 32768),
                max_output_tokens=int(cfg.get("analyze_max_tokens") or 8192),
                context_source="legacy_config", output_source="legacy_config",
                thinking_effort=str(cfg.get("thinking_effort") or ""))
            cfg["active_model_id"] = model["id"]
            active_model = model

        if not cfg.get("active_model_id"):
            models = store.list_models()
            if models:
                cfg["active_model_id"] = models[0]["id"]

        if cfg.get("active_model_id") and not cfg.get("_config_broken"):
            save_config(cfg)

        # Do this only after the key is safely present in SQLite. This is idempotent
        # and also cleans up duplicate API keys left by earlier startup migrations.
        if migrated_key:
            sdata, serr = _read_json_file(SECRETS_FILE)
            if not serr and "api_key" in sdata:
                sdata.pop("api_key", None)
                _atomic_write(SECRETS_FILE, sdata)

        if providers:
            return
        if active_model:
            log.info("已初始化供应商与激活模型：model=%s", active_model["id"])
    except Exception as exc:
        log.warning("供应商/模型初始化失败：%s", exc)


_bootstrap_providers_from_legacy_config()


def get_session_cookie() -> bili_api.CookieInfo:
    """获取通过二维码或手动输入保存的 Cookie。"""
    cfg = load_config()
    cookie = str(cfg.get("cookie_string", "") or "").strip()
    if not cookie:
        raise bili_api.BiliApiError("尚未配置 Cookie；请在连接配置中扫码登录或手动输入 Cookie。")
    return bili_api.load_cookie_from_string(cookie)


@app.get("/api/cookie")
def get_cookie():
    """返回当前是否已配置 cookie（不回显原值，只给尾 4 位、长度与 mid）。"""
    cfg = load_config()
    ck = cfg.get("cookie_string", "") or ""
    mid = ""
    for part in ck.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "DedeUserID":
            mid = v.strip()
            break
    return {"configured": bool(ck.strip()), "length": len(ck), "masked": _mask(ck),
            "mid": mid}


class CookieIn(BaseModel):
    cookie_string: str = ""


@app.post("/api/cookie")
def set_cookie(body: CookieIn):
    """保存手动粘贴的 cookie（留空 / 脱敏值 → 不覆盖）。"""
    v = (body.cookie_string or "").strip()
    cur = load_config()
    if v and "****" not in v:
        cur["cookie_string"] = v
    save_config(cur)
    return {"ok": True}


@app.get("/api/config")
def get_config():
    """返回配置，但凭据只回**尾 4 位**（P0-1）。附带供应商/模型索引（密钥脱敏）。"""
    raw = load_config()
    cfg = resolved_llm_settings(raw)
    out = dict(raw)
    out["api_key"] = ""
    out["api_key_masked"] = _mask(cfg.get("api_key", ""))
    out["api_key_set"] = bool(cfg.get("api_key"))
    ck = raw.get("cookie_string", "") or ""
    out["cookie_string"] = ""
    out["cookie_masked"] = _mask(ck)
    out["cookie_set"] = bool(ck)
    # 激活模型解析后的展示字段（不覆盖 raw 中的全局默认，仅补充）
    out["active_base_url"] = cfg.get("base_url", "")
    out["active_model"] = cfg.get("model", "")
    out["active_context_tokens"] = cfg.get("model_context_tokens")
    out["active_max_output_tokens"] = cfg.get("analyze_max_tokens")
    out["thinking_effort"] = cfg.get("thinking_effort", "")
    out["providers"] = [
        {**{k: p[k] for k in ("id", "name", "base_url", "created_at", "updated_at")},
         "api_key": "", "api_key_masked": _mask(p.get("api_key") or ""),
         "api_key_set": bool(p.get("api_key"))}
        for p in store.list_providers()
    ]
    out["models"] = _effective_model_rows(store.list_models())
    out["provider_presets"] = PROVIDER_PRESETS
    return out


class ConfigIn(BaseModel):
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    active_model_id: Optional[str] = None
    scan_interval: Optional[int] = None
    analyze_concurrency: Optional[int] = None
    analyze_batch: Optional[int] = None
    analyze_max_tokens: Optional[int] = None
    model_context_tokens: Optional[int] = None
    model_tpm_limit: Optional[int] = None
    profile_request_interval: Optional[float] = None
    scan_scope: Optional[str] = None
    write_interval: Optional[int] = None
    folder_merge_interval: Optional[int] = None
    apply_batch: Optional[int] = None
    prompt_profile: Optional[str] = None
    prompt_analyze: Optional[str] = None
    prompt_merge: Optional[str] = None
    confidence_profile_min: Optional[float] = None
    confidence_analyze_min: Optional[float] = None
    confidence_merge_min: Optional[float] = None


@app.post("/api/config")
def set_config(cfg: ConfigIn):
    cur = load_config()
    if cfg.base_url is not None:
        cur["base_url"] = cfg.base_url
    if cfg.api_key is not None:
        v = (cfg.api_key or "").strip()
        # 留空 / 传回脱敏值 → 不覆盖原 key（P0-1 配套）
        if v and "****" not in v:
            cur["api_key"] = v
    if cfg.model is not None:
        cur["model"] = cfg.model
    if cfg.active_model_id is not None:
        mid = (cfg.active_model_id or "").strip()
        if mid and not store.get_model(mid):
            return JSONResponse({"ok": False, "error": "激活模型不存在"}, status_code=400)
        cur["active_model_id"] = mid
        if mid:
            resolved = resolved_llm_settings({**cur, "active_model_id": mid})
            if resolved.get("base_url"):
                cur["base_url"] = resolved["base_url"]
            if resolved.get("model"):
                cur["model"] = resolved["model"]
            if resolved.get("model_context_tokens"):
                cur["model_context_tokens"] = int(resolved["model_context_tokens"])
            if resolved.get("analyze_max_tokens"):
                cur["analyze_max_tokens"] = int(resolved["analyze_max_tokens"])
    if cfg.scan_interval is not None:
        cur["scan_interval"] = max(2, int(cfg.scan_interval))
        if APP.get("session"):
            APP["session"].read_interval = max(2, int(cfg.scan_interval))
    if cfg.analyze_concurrency is not None:
        cur["analyze_concurrency"] = max(1, min(4, int(cfg.analyze_concurrency)))
    if cfg.analyze_batch is not None:
        cur["analyze_batch"] = max(1, min(1000, int(cfg.analyze_batch)))
    if cfg.analyze_max_tokens is not None:
        cur["analyze_max_tokens"] = max(256, min(131072, int(cfg.analyze_max_tokens)))
    if cfg.model_context_tokens is not None:
        cur["model_context_tokens"] = max(4096, min(1000000, int(cfg.model_context_tokens)))
    if cfg.model_tpm_limit is not None:
        cur["model_tpm_limit"] = max(1000, min(10000000, int(cfg.model_tpm_limit)))
    if cfg.profile_request_interval is not None:
        cur["profile_request_interval"] = max(0.5, min(60.0, float(cfg.profile_request_interval)))
    if cfg.scan_scope is not None:
        cur["scan_scope"] = cfg.scan_scope if cfg.scan_scope in ("all", "default") else "all"
    if cfg.write_interval is not None:
        cur["write_interval"] = max(1, min(60, int(cfg.write_interval)))
        if APP.get("session"):
            APP["session"].write_interval = float(cur["write_interval"])
    if cfg.folder_merge_interval is not None:
        cur["folder_merge_interval"] = max(1, min(60, int(cfg.folder_merge_interval)))
    if cfg.apply_batch is not None:
        cur["apply_batch"] = max(1, min(1000, int(cfg.apply_batch)))
    if cfg.prompt_profile is not None:
        cur["prompt_profile"] = cfg.prompt_profile
    if cfg.prompt_analyze is not None:
        cur["prompt_analyze"] = cfg.prompt_analyze
    if cfg.prompt_merge is not None:
        cur["prompt_merge"] = cfg.prompt_merge
    if cfg.confidence_profile_min is not None:
        cur["confidence_profile_min"] = max(0.0, min(1.0, float(cfg.confidence_profile_min)))
    if cfg.confidence_analyze_min is not None:
        cur["confidence_analyze_min"] = max(0.0, min(1.0, float(cfg.confidence_analyze_min)))
    if cfg.confidence_merge_min is not None:
        cur["confidence_merge_min"] = max(0.0, min(1.0, float(cfg.confidence_merge_min)))
    save_config(cur)
    return {"ok": True, "config": get_config()}


DEFAULT_PROMPTS = {
    # 与 llm_analyzer 生产提示词同源（规则段）；运行时仍会拼接目录/画像上下文
    "prompt_profile": (
        "你是收藏夹内容画像整理助手。请根据输入的全部条目归纳收藏夹画像，收藏夹名称只是弱提示。"
        "输出 JSON：summary 简介、topics 主题列表、typical_content 典型内容、out_of_scope 范围外提示、"
        "coherence 一致性说明、confidence 0~1。只返回 JSON。"
    ),
    "prompt_analyze": (
        "你是 B 站收藏夹整理助手。任务是判断一批视频各自归属于哪个收藏夹。\n"
        "规则：\n"
        "1. 每个输入 id 必须有一条对应结果，id 原样返回。\n"
        "2. 归类以画像的实际主题和收纳范围为主要依据，收藏夹名称只作弱提示。\n"
        "3. action=move_to_existing 且 recommended 就是已所在夹时表示留下，不要用 skip 表示留下。\n"
        "4. in_default_inbox=true 表示尚未分拣，必须在有画像的收藏夹中选最合适的一个；"
        "所有画像都不符且新主题清楚时才 create_new；确实无处可去才 skip。\n"
        "5. 略微偏离且证据不明确时保留原位。\n"
        "6. 标题/简介/UP主全空或完全无法归类才 skip。\n"
        "7. confidence 为 0~1。\n"
        "8. reason 不超过 20 字。\n"
        "9. 默认收藏夹只能移出，绝不能作为移入目标。\n"
        "只返回纯 JSON。"
    ),
    "prompt_merge": (
        "你是 B 站收藏夹信息架构整理助手。目标是在保留有用主题边界的前提下提出合并组。"
        "输出 JSON groups：target_id、source_ids、final_name、reason、confidence。只返回 JSON。"
    ),
}
DEFAULT_CONFIDENCE = {
    "confidence_profile_min": 0.5,
    "confidence_analyze_min": 0.5,
    "confidence_merge_min": 0.5,
}


@app.get("/api/prompts/defaults")
def prompts_defaults():
    return {"ok": True, "defaults": {**DEFAULT_PROMPTS, **DEFAULT_CONFIDENCE}}


@app.get("/api/data/paths")
def data_paths():
    return {
        "ok": True,
        "data_dir": str(USER_DATA_DIR),
        "db_file": str(store.DB_FILE),
    }


@app.get("/api/data/usage")
def data_usage():
    def _dir_size(path: Path) -> int:
        total = 0
        if not path.exists():
            return 0
        if path.is_file():
            return path.stat().st_size
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
        return total

    proj = _dir_size(USER_DATA_DIR)
    disk_total = disk_free = None
    try:
        if sys.platform == "win32":
            import shutil as _sh
            usage = _sh.disk_usage(str(USER_DATA_DIR if USER_DATA_DIR.exists() else Path.home()))
            disk_total, disk_free = usage.total, usage.free
        else:
            import shutil as _sh
            usage = _sh.disk_usage(str(USER_DATA_DIR if USER_DATA_DIR.exists() else Path.home()))
            disk_total, disk_free = usage.total, usage.free
    except OSError:
        pass
    disk_used = (disk_total - disk_free) if disk_total is not None else None
    return {
        "ok": True,
        "project_bytes": proj,
        "disk_total_bytes": disk_total,
        "disk_used_bytes": disk_used,
        "disk_free_bytes": disk_free,
    }


@app.post("/api/data/open-folder")
def data_open_folder():
    try:
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            # explorer 打开并尽量置顶，避免落在后台
            subprocess.Popen(["explorer", str(USER_DATA_DIR)])
            time.sleep(0.4)
            try:
                import ctypes
                user32 = ctypes.windll.user32
                hwnd = user32.FindWindowW("CabinetWClass", None)
                if hwnd:
                    user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(USER_DATA_DIR)])
        else:
            subprocess.Popen(["xdg-open", str(USER_DATA_DIR)])
        return {"ok": True}
    except OSError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ============ 供应商 / 模型管理 ============
class ProviderIn(BaseModel):
    name: str = ""
    base_url: str = ""
    api_key: str = ""


class ProviderUpdateIn(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None


class ModelIn(BaseModel):
    provider_id: str
    name: str
    context_tokens: Optional[int] = None
    max_output_tokens: Optional[int] = None
    thinking_effort: str = ""
    capabilities: dict = Field(default_factory=dict)


class ModelUpdateIn(BaseModel):
    name: Optional[str] = None
    context_tokens: Optional[int] = None
    max_output_tokens: Optional[int] = None
    context_source: Optional[str] = None
    output_source: Optional[str] = None
    thinking_effort: Optional[str] = None
    provider_id: Optional[str] = None
    capabilities: Optional[dict] = None


class ModelSyncIn(BaseModel):
    provider_id: str
    mode: str = "add_new"  # overwrite | remove_stale | add_new | remove_selected | add_selected
    names: list[str] = Field(default_factory=list)
    remote_models: list[dict] = Field(default_factory=list)
    selected_names: list[str] = Field(default_factory=list)


class ModelTestIn(BaseModel):
    model_id: Optional[str] = None


def _public_provider(p: dict) -> dict:
    return {**{k: p[k] for k in ("id", "name", "base_url", "created_at", "updated_at")},
            "api_key": "", "api_key_masked": _mask(p.get("api_key") or ""),
            "api_key_set": bool(p.get("api_key"))}


def _effective_model_rows(models: list[dict]) -> list[dict]:
    """Expose model output limits after applying documented provider hard caps."""
    providers = {p["id"]: p for p in store.list_providers()}
    result = []
    for model in models:
        row = dict(model)
        provider = providers.get(str(row.get("provider_id") or ""))
        if provider and row.get("max_output_tokens"):
            stored_limit = int(row["max_output_tokens"])
            effective_limit = llm_analyzer.cap_completion_tokens(
                provider.get("base_url") or "", row.get("name") or "", stored_limit)
            row["max_output_tokens"] = effective_limit
            if effective_limit < stored_limit:
                row["output_source"] = "documented_cap"
        result.append(row)
    return result


def _model_output_limit_error(provider: dict, model_name: str,
                              requested: int | None) -> str | None:
    if requested is None or int(requested) <= 0:
        return None
    effective = llm_analyzer.cap_completion_tokens(
        provider.get("base_url") or "", model_name, int(requested))
    if effective < int(requested):
        return f"最大输出超过该模型 API 上限（{effective:,} tokens）"
    return None


@app.get("/api/providers/presets")
def provider_presets():
    return {"presets": PROVIDER_PRESETS}


@app.get("/api/providers")
def providers_list():
    return {"providers": [_public_provider(p) for p in store.list_providers()],
            "models": _effective_model_rows(store.list_models())}


@app.post("/api/providers")
def providers_create(body: ProviderIn):
    name = (body.name or "").strip()
    base_url = (body.base_url or "").strip()
    if not name or not base_url:
        return JSONResponse({"ok": False, "error": "需要名称和 base_url"}, status_code=400)
    api_key = (body.api_key or "").strip()
    # 脱敏回传不写入
    if "****" in api_key:
        api_key = ""
    provider = store.create_provider(name, base_url, api_key)
    return {"ok": True, "provider": _public_provider(provider), "models": []}


@app.put("/api/providers/{provider_id}")
def providers_update(provider_id: str, body: ProviderUpdateIn):
    api_key = body.api_key
    if api_key is not None and "****" in (api_key or ""):
        api_key = None  # 不覆盖
    provider = store.update_provider(provider_id, name=body.name, base_url=body.base_url,
                                     api_key=api_key)
    if not provider:
        return JSONResponse({"ok": False, "error": "供应商不存在"}, status_code=404)
    return {"ok": True, "provider": _public_provider(provider)}


@app.delete("/api/providers/{provider_id}")
def providers_delete(provider_id: str):
    cfg = load_config()
    models = store.list_models(provider_id)
    active = str(cfg.get("active_model_id") or "")
    if any(m["id"] == active for m in models):
        cur = dict(cfg)
        cur["active_model_id"] = ""
        save_config(cur)
    if not store.delete_provider(provider_id):
        return JSONResponse({"ok": False, "error": "供应商不存在"}, status_code=404)
    return {"ok": True}


@app.get("/api/models")
def models_list(provider_id: Optional[str] = None):
    return {"models": _effective_model_rows(store.list_models(provider_id))}


@app.delete("/api/providers/{provider_id}/models")
def provider_models_delete_all(provider_id: str):
    if not store.get_provider(provider_id):
        return JSONResponse({"ok": False, "error": "供应商不存在"}, status_code=404)
    cfg = load_config()
    active_model_id = str(cfg.get("active_model_id") or "")
    removed_ids = store.delete_provider_models(provider_id)
    if active_model_id and active_model_id in removed_ids:
        cfg["active_model_id"] = ""
        save_config(cfg)
    return {"ok": True, "removed": len(removed_ids)}


@app.post("/api/models")
def models_create(body: ModelIn):
    provider = store.get_provider(body.provider_id)
    if not provider:
        return JSONResponse({"ok": False, "error": "供应商不存在"}, status_code=404)
    name = (body.name or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "模型名称不能为空"}, status_code=400)
    limit_error = _model_output_limit_error(provider, name, body.max_output_tokens)
    if limit_error:
        return JSONResponse({"ok": False, "error": limit_error}, status_code=400)
    model = store.create_model(
        body.provider_id, name,
        context_tokens=body.context_tokens,
        max_output_tokens=body.max_output_tokens,
        thinking_effort=body.thinking_effort or "",
        capabilities=body.capabilities or {})
    return {"ok": True, "model": _effective_model_rows([model])[0]}


@app.put("/api/models/{model_id}")
def models_update(model_id: str, body: ModelUpdateIn):
    if body.provider_id and not store.get_provider(body.provider_id):
        return JSONResponse({"ok": False, "error": "目标供应商不存在"}, status_code=404)
    existing = store.get_model(model_id)
    if not existing:
        return JSONResponse({"ok": False, "error": "模型不存在"}, status_code=404)
    provider = store.get_provider(body.provider_id or existing["provider_id"])
    if not provider:
        return JSONResponse({"ok": False, "error": "供应商不存在"}, status_code=404)
    limit_error = _model_output_limit_error(
        provider, (body.name or existing["name"]).strip() or existing["name"],
        body.max_output_tokens)
    if limit_error:
        return JSONResponse({"ok": False, "error": limit_error}, status_code=400)
    model = store.update_model(
        model_id, name=body.name, context_tokens=body.context_tokens,
        max_output_tokens=body.max_output_tokens,
        context_source=body.context_source, output_source=body.output_source,
        thinking_effort=body.thinking_effort,
        capabilities=body.capabilities,
        provider_id=body.provider_id)
    if not model:
        return JSONResponse({"ok": False, "error": "模型不存在"}, status_code=404)
    return {"ok": True, "model": _effective_model_rows([model])[0]}


@app.delete("/api/models/{model_id}")
def models_delete(model_id: str):
    cfg = load_config()
    if str(cfg.get("active_model_id") or "") == str(model_id):
        cur = dict(cfg)
        cur["active_model_id"] = ""
        save_config(cur)
    if not store.delete_model(model_id):
        return JSONResponse({"ok": False, "error": "模型不存在"}, status_code=404)
    return {"ok": True}


def _remote_model_record(item) -> dict:
    """Normalize model limits and request capabilities exposed by model catalogs."""
    if isinstance(item, dict):
        name = str(item.get("model_id") or item.get("id") or
                   item.get("model") or item.get("name") or "").strip()
        source = item
    else:
        name = str(item or "").strip()
        source = {}
    if not name:
        return {}
    record = {"name": name}
    metadata_sources = [source]
    for key in ("model_info", "metadata", "limits", "top_provider", "capabilities"):
        nested = source.get(key)
        if isinstance(nested, dict):
            metadata_sources.append(nested)
    aliases = {
        "context_tokens": ("context_tokens", "context_length", "context_window",
                           "max_context_length", "max_model_len"),
        "max_output_tokens": ("max_output_tokens", "max_completion_tokens", "max_tokens", "output_limit"),
    }
    for target, keys in aliases.items():
        for metadata in metadata_sources:
            for key in keys:
                value = metadata.get(key)
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    continue
                if parsed > 0:
                    record[target] = parsed
                    record["context_source" if target == "context_tokens" else "output_source"] = "api"
                    break
            if target in record:
                break
    for metadata in metadata_sources:
        for key in ("thinking_effort", "reasoning_effort"):
            value = metadata.get(key)
            if isinstance(value, str):
                record["thinking_effort"] = value
                break
        if "thinking_effort" in record:
            break
    capabilities = {}
    for metadata in metadata_sources:
        supported = metadata.get("supported_parameters")
        if isinstance(supported, list):
            capabilities["supported_parameters"] = [str(value) for value in supported]
            break
        if isinstance(supported, dict):
            capabilities["supported_parameters"] = [
                str(key) for key, enabled in supported.items() if enabled]
            break
    for target, aliases in {
        "json_output": ("json_output", "supports_json_output"),
        "structured_outputs": ("structured_outputs",),
    }.items():
        for metadata in metadata_sources:
            for key in aliases:
                value = metadata.get(key)
                if isinstance(value, bool):
                    capabilities[target] = value
                    break
            if target in capabilities:
                break
    for metadata in metadata_sources:
        reasoning = metadata.get("reasoning")
        nested_levels = reasoning.get("effort_levels") if isinstance(reasoning, dict) else None
        levels = (metadata.get("reasoning_effort_levels") or
                  metadata.get("supported_reasoning_efforts") or
                  metadata.get("reasoning_efforts") or nested_levels or
                  metadata.get("reasoning_effort"))
        if isinstance(levels, (list, tuple)) and all(isinstance(value, (str, int, float)) for value in levels):
            capabilities["reasoning_effort_levels"] = [str(value) for value in levels]
            break
    for metadata in metadata_sources:
        field = metadata.get("completion_token_field") or metadata.get("max_tokens_field")
        if field in ("max_tokens", "max_completion_tokens"):
            capabilities["completion_token_field"] = field
            break
    if capabilities:
        record["capabilities"] = capabilities
    return record


def _fetch_remote_models(provider: dict) -> list[dict]:
    """Fetch visible model IDs plus documented capability metadata when available."""
    base = (provider.get("base_url") or "").strip().rstrip("/")
    if not base:
        raise ValueError("供应商缺少 base_url")
    headers = {"Accept": "application/json"}
    if provider.get("api_key"):
        headers["Authorization"] = "Bearer " + provider["api_key"]
    def get_json(url, params=None):
        response = requests.get(url, headers=headers, params=params, timeout=30)
        if not response.ok:
            raise ValueError(f"HTTP {response.status_code}")
        return response.json()

    data = get_json(base + "/models")
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list) and isinstance(data, dict):
        output = data.get("output")
        items = output.get("models") if isinstance(output, dict) else None
    if not isinstance(items, list):
        raise ValueError("模型列表格式无法识别")
    models: list[dict] = []
    seen: set[str] = set()
    for item in items:
        model = _remote_model_record(item)
        if model and model["name"] not in seen:
            models.append(model)
            seen.add(model["name"])

    # DigitalOcean exposes model limits on its GenAI catalog, separate from the
    # OpenAI-compatible inference /v1/models endpoint.
    if "inference.do-ai.run" in base.lower():
        try:
            catalog_by_name = {}
            def model_name_keys(*names):
                keys = set()
                for value in names:
                    clean = str(value or "").strip().casefold()
                    if clean:
                        keys.add(clean)
                        keys.add(clean.rsplit("/", 1)[-1])
                return keys

            for page in range(1, 51):
                catalog = get_json("https://api.digitalocean.com/v2/gen-ai/models/catalog",
                                   {"page": page, "limit": 200})
                rows = catalog.get("data") if isinstance(catalog, dict) else None
                if not isinstance(rows, list) or not rows:
                    break
                for item in rows:
                    record = _remote_model_record(item)
                    if record:
                        for key in model_name_keys(record["name"], item.get("model_id"),
                                                  item.get("hugging_face_id"), item.get("name")):
                            catalog_by_name[key] = record
                meta = catalog.get("meta") or {}
                pages = int(meta.get("pages") or 0)
                if pages and page >= pages:
                    break
                if not pages and len(rows) < 200:
                    break
            for model in models:
                catalog_record = next((catalog_by_name[key]
                                       for key in model_name_keys(model["name"])
                                       if key in catalog_by_name), None)
                if catalog_record:
                    model.update({key: value for key, value in catalog_record.items()
                                  if key != "name"})
                else:
                    model["metadata_warning"] = "DigitalOcean 目录未返回此模型的规格"
        except Exception:
            for model in models:
                model["metadata_warning"] = "无法读取 DigitalOcean 规格目录；当前只拿到模型列表"

    # Model Studio's detailed catalog is workspace/region scoped. Its response
    # includes model_info.context_window and model_info.max_output_tokens.
    from urllib.parse import urlsplit, urlunsplit
    parsed_base = urlsplit(base)
    hostname = (parsed_base.hostname or "").lower()
    if ((hostname.endswith(".maas.aliyuncs.com") and not hostname.startswith("token-plan.")) or hostname in (
            "dashscope-intl.aliyuncs.com", "cn-hongkong.dashscope.aliyuncs.com")):
        catalog_origin = urlunsplit((parsed_base.scheme, parsed_base.netloc, "", "", ""))
        try:
            catalog_by_name = {}
            for page in range(1, 101):
                catalog = get_json(catalog_origin + "/api/v1/models",
                                   {"page_no": page, "page_size": 100})
                output = catalog.get("output") if isinstance(catalog, dict) else None
                rows = output.get("models") if isinstance(output, dict) else None
                if not isinstance(rows, list) or not rows:
                    break
                for item in rows:
                    record = _remote_model_record(item)
                    if record:
                        catalog_by_name[record["name"]] = record
                total = int(output.get("total") or 0)
                if total and page * 100 >= total:
                    break
            for model in models:
                catalog_record = catalog_by_name.get(model["name"])
                if catalog_record:
                    model.update({key: value for key, value in catalog_record.items()
                                  if key != "name"})
                else:
                    model["metadata_warning"] = "百炼目录未返回此模型的规格"
        except Exception:
            for model in models:
                model["metadata_warning"] = "无法读取百炼详细目录；当前只拿到模型列表"
    return models


@app.post("/api/providers/{provider_id}/remote-models")
def provider_remote_models(provider_id: str):
    provider = store.get_provider(provider_id)
    if not provider:
        return JSONResponse({"ok": False, "error": "供应商不存在"}, status_code=404)
    try:
        remote_models = _fetch_remote_models(provider)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    names = [m["name"] for m in remote_models]
    metadata_warnings = {str(m["metadata_warning"]) for m in remote_models
                         if m.get("metadata_warning")}
    missing_context = sum(1 for model in remote_models if not model.get("context_tokens"))
    missing_output = sum(1 for model in remote_models if not model.get("max_output_tokens"))
    if missing_context:
        metadata_warnings.add(f"API 未提供 {missing_context}/{len(remote_models)} 个模型的上下文窗口")
    if missing_output:
        metadata_warnings.add(f"API 未提供 {missing_output}/{len(remote_models)} 个模型的最大输出")
    metadata_warnings = sorted(metadata_warnings)
    local = {m["name"] for m in store.list_models(provider_id)}
    remote = set(names)
    return {
        "ok": True,
        "remote": sorted(remote),
        "remote_models": remote_models,
        "metadata_warnings": metadata_warnings,
        "local": sorted(local),
        "added": sorted(remote - local),      # 远程有、本地无 → 新增
        "removed": sorted(local - remote),    # 本地有、远程无 → 失效
        "common": sorted(local & remote),
    }


@app.post("/api/models/sync")
def models_sync(body: ModelSyncIn):
    provider = store.get_provider(body.provider_id)
    if not provider:
        return JSONResponse({"ok": False, "error": "供应商不存在"}, status_code=404)
    mode = body.mode
    if mode not in ("overwrite", "remove_stale", "add_new", "remove_selected", "add_selected"):
        return JSONResponse({"ok": False, "error": "未知同步模式"}, status_code=400)
    records_by_name = {}
    for item in body.remote_models:
        record = _remote_model_record(item)
        if record:
            records_by_name[record["name"]] = record
    remote_names = list(dict.fromkeys(
        [str(x).strip() for x in body.names if str(x).strip()] + list(records_by_name)))
    records = [records_by_name.get(name, {"name": name}) for name in remote_names]
    selected_names = {str(name).strip() for name in body.selected_names if str(name).strip()}
    if mode in ("remove_selected", "add_selected") and not selected_names:
        return JSONResponse({"ok": False, "error": "请选择要处理的模型"}, status_code=400)
    if not remote_names and mode != "remove_selected":
        return JSONResponse({"ok": False, "error": "远程模型列表为空"}, status_code=400)
    cfg = load_config()
    active = str(cfg.get("active_model_id") or "")
    local = store.list_models(body.provider_id)
    local_by_name = {m["name"]: m for m in local}

    if mode == "overwrite":
        store.replace_provider_models(body.provider_id, records)
        if active and not store.get_model(active):
            cfg["active_model_id"] = ""
            save_config(cfg)
    elif mode in ("remove_stale", "remove_selected"):
        remote_set = set(remote_names)
        for m in local:
            selected_match = mode == "remove_stale" or m["name"] in selected_names
            if selected_match and m["name"] not in remote_set:
                if m["id"] == active:
                    cfg["active_model_id"] = ""
                    save_config(cfg)
                    cfg = load_config()
                    active = ""
                store.delete_model(m["id"])
    else:  # add_new / add_selected
        for record in records:
            name = record["name"]
            selected_match = mode == "add_new" or name in selected_names
            if not selected_match:
                continue
            existing = local_by_name.get(name)
            if existing:
                # Refresh request capabilities even when the model was already
                # present; catalog capability changes affect how future calls
                # must be shaped, while local token limits remain user-owned.
                if record.get("capabilities") is not None:
                    store.update_model(existing["id"], capabilities=record["capabilities"])
                continue
            store.create_model(
                body.provider_id, name,
                context_tokens=record.get("context_tokens"),
                max_output_tokens=record.get("max_output_tokens"),
                context_source=record.get("context_source", "unknown"),
                output_source=record.get("output_source", "unknown"),
                thinking_effort=record.get("thinking_effort", ""),
                capabilities=record.get("capabilities") or {})
    return {"ok": True, "models": _effective_model_rows(store.list_models(body.provider_id))}


@app.post("/api/models/{model_id}/test")
def models_test(model_id: str):
    model = store.get_model(model_id)
    if not model:
        return JSONResponse({"ok": False, "error": "模型不存在"}, status_code=404)
    provider = store.get_provider(model["provider_id"])
    if not provider:
        return JSONResponse({"ok": False, "error": "供应商不存在"}, status_code=404)
    store.update_model(model_id, test_status="testing", test_message="测试中…")
    try:
        url = (provider.get("base_url") or "").rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if provider.get("api_key"):
            headers["Authorization"] = "Bearer " + provider["api_key"]
        test_config = llm_analyzer.LLMConfig(
            base_url=provider.get("base_url") or "",
            api_key=provider.get("api_key") or "",
            model=model["name"],
            params=_thinking_params(model.get("thinking_effort") or ""),
            max_tokens=256,
            provider_name=provider.get("name", ""),
            capabilities=dict(model.get("capabilities") or {}),
        )
        payload = _build_model_test_payload(test_config)
        r = requests.post(url, json=payload, headers=headers,
                          timeout=MODEL_TEST_TIMEOUT_SECONDS)
        if not r.ok:
            msg = f"HTTP {r.status_code}: {r.text[:180]}"
            store.update_model(model_id, test_status="fail", test_message=msg,
                               test_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            return {"ok": False, "error": msg, "model": store.get_model(model_id)}
        content = llm_analyzer.extract_chat_completion_content(r.json())
        if not isinstance(llm_analyzer.extract_json_object(content), dict):
            msg = "接口可连接，但模型未返回可解析的 JSON 对象"
            store.update_model(model_id, test_status="fail", test_message=msg,
                               test_at=time.strftime("%Y-%m-%d %H:%M:%S"))
            return {"ok": False, "error": msg, "model": store.get_model(model_id)}
        store.update_model(model_id, test_status="ok", test_message="连接正常，JSON 输出有效",
                           test_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        return {"ok": True, "message": "连接正常，JSON 输出有效", "model": store.get_model(model_id)}
    except Exception as exc:
        store.update_model(model_id, test_status="fail", test_message=str(exc)[:200],
                           test_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        return {"ok": False, "error": str(exc), "model": store.get_model(model_id)}


# ============ 项目数据：导出 / 导入 / 清除 ============
INVALID_TITLE = "已失效视频"


def _is_invalid(video: dict) -> bool:
    """防火墙：只有标题**恰好**是「已失效视频」才算失效视频。"""
    return (video.get("title") or "").strip() == INVALID_TITLE


def _autobackup_data(tag: str = "autobackup") -> str:
    """备份 data/ 下的 SQLite 数据库、JSON 和 JSONL 到带时间戳的目录。

    两条策略：
    1. 只保留最近 1 份，更旧的备份自动删除；
    2. 数据几乎为空时不新建备份、也不动已有备份 —— 否则连续点两次「清除数据」
       会把唯一一份好备份换成空壳（历史上因此产生过 15 个空目录）。
    """
    import shutil
    try:
        files = [*store.DATA_DIR.glob("*.json"), *store.DATA_DIR.glob("*.jsonl")]
        total = sum(p.stat().st_size for p in files)
        if store.DB_FILE.exists():
            total += store.DB_FILE.stat().st_size
        backup_dir = store.DATA_DIR / "backups"
        existing = sorted(backup_dir.glob("bili-data-*")) if backup_dir.exists() else []
        if (not files and not store.DB_FILE.exists()) or total < 10 * 1024:
            log.info("数据几乎为空（%d 字节），跳过备份（已有 %d 份备份保持不变）",
                     total, len(existing))
            return str(existing[-1]) if existing else ""
        backup_dir.mkdir(parents=True, exist_ok=True)
        dst = backup_dir / f"bili-data-{tag}-{time.strftime('%Y%m%d_%H%M%S')}"
        dst.mkdir(parents=True, exist_ok=True)
        for p in files:
            shutil.copy2(p, dst / p.name)
        if store.DB_FILE.exists():
            store.backup_database(dst / store.DB_FILE.name)
        for old in sorted(backup_dir.glob("bili-data-*")):
            if old != dst:
                shutil.rmtree(old, ignore_errors=True)
        return str(dst)
    except Exception as e:
        log.warning("自动备份失败: %s", e)
        return ""


class DataIn(BaseModel):
    bundle: dict = {}
    scope: str = "all"
    scopes: list[str] = []


@app.get("/api/data/export")
def data_export():
    """导出全部项目数据为一个 JSON 文件下载（不含 cookie / api_key）。"""
    bundle = store.export_all()
    fn = "bili_fav_data_" + time.strftime("%Y%m%d_%H%M%S") + ".json"
    return JSONResponse(content=bundle,
                        headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@app.post("/api/data/import")
def data_import(body: DataIn):
    """导入项目数据（覆盖当前数据）。导入前自动备份当前数据。"""
    if not body.bundle:
        return JSONResponse({"ok": False, "error": "未收到数据"}, status_code=400)
    bk = _autobackup_data("before-import")
    try:
        st = store.import_all(body.bundle)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"导入失败：{e}"}, status_code=400)
    emit("warn", f"已导入项目数据（覆盖原有）：{st}；原数据已备份到 {bk}")
    return {"ok": True, "stats": st, "backup": bk}


@app.post("/api/data/clear")
def data_clear(body: DataIn):
    """清除指定范围的数据。scope: scan / analysis / plan / all。清除前**自动备份**。"""
    valid = {"scan", "analysis", "plan", "folder_merge", "folder_profile", "all"}
    scopes = [x for x in body.scopes if x in valid]
    if not scopes:
        scopes = [body.scope if body.scope in valid else "all"]
    if "all" in scopes:
        scopes = ["all"]
    bk = _autobackup_data("before-clear")
    cleared = []
    for scope in scopes:
        cleared.extend(store.clear_scope(scope))
    cleared = list(dict.fromkeys(cleared))
    emit("warn", f"已清除数据（{', '.join(scopes)}）：{', '.join(cleared)}；清除前已自动备份到 {bk}")
    return {"ok": True, "scopes": scopes, "cleared": cleared,
            "backup": bk, "stats": store.stats()}


# ============ 事件流（SSE） ============
@app.get("/api/events/stream")
async def events_stream(since: int = 0):
    """服务器推送事件流；空闲时不发送任何数据（事件驱动，非轮询）。"""
    from fastapi.responses import StreamingResponse

    async def gen():
        last = int(since)
        # 建立连接时先把历史事件(最近 200 条)补发，避免刷新丢日志
        with _evt_lock:
            backlog = [e for e in APP["events"]][-200:]
        for e in backlog:
            if e["id"] > last:
                last = e["id"]
                yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
        while True:
            await asyncio.sleep(0.4)
            if _SHUTDOWN.is_set():
                return      # 更新替换前主动断开长连接，否则 uvicorn 退不出去
            with _evt_lock:
                new = [e for e in APP["events"] if e["id"] > last]
            for e in new:
                last = e["id"]
                yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ============ 阶段1: 收藏夹目录与扫描 ============
class ScanIn(BaseModel):
    folder_ids: Optional[list[str]] = None
    mode: str = "resume"       # resume / rebuild


class ScanSelectionIn(BaseModel):
    folder_ids: list[str]


class ScanRecoveryIn(BaseModel):
    action: str = "retry"  # retry / clean / refresh
    confirmed: bool = False


def _pending_scan_issues() -> list[dict]:
    """Include pre-upgrade inconsistent scans without treating a gap as invalid content."""
    saved = store.load_scan_issues()
    states = store.load_folder_scan_states()
    folders = {str(f["media_id"]): f for f in store.load_folders()}
    ids = {mid for mid, issue in saved.items() if issue.get("status") != "resolved"}
    ids.update(mid for mid, state in states.items() if state.get("status") == "inconsistent")
    result = []
    for mid in sorted(ids):
        state, folder = states.get(mid, {}), folders.get(mid, {})
        issue = {"media_id": mid, "title": folder.get("title", mid), "status": "pending",
                 "expected_count": state.get("expected_count", folder.get("count", 0)),
                 "fetched_count": state.get("fetched_count", 0),
                 "unique_count": state.get("fetched_count", 0), "history": [],
                 "last_error": state.get("last_error"), **saved.get(mid, {})}
        result.append(issue)
    return result


@app.middleware("http")
async def scan_issue_gate(request, call_next):
    blocked_paths = {"/api/analyze/start", "/api/analyze/continuous", "/api/plan/mark_invalid",
                     "/api/plan/apply", "/api/apply/start", "/api/folder-profiles/generate",
                     "/api/folder-organize/suggest", "/api/folder-organize/plan",
                     "/api/folder-organize/start"}
    if request.method in ("POST", "PUT") and (
            request.url.path in blocked_paths or request.url.path.endswith("/publish-intro")):
        issues = _pending_scan_issues()
        if issues:
            return JSONResponse({"ok": False, "code": "scan_issue_pending",
                                 "error": "收藏夹扫描异常尚未解决，请先处理提示窗口中的问题。",
                                 "issues": issues}, status_code=409)
    return await call_next(request)


@app.middleware("http")
async def local_api_guard(request, call_next):
    """Host / Origin / Sec-Fetch / Token —— 挡 CSRF 与 DNS rebinding。

    注册在 scan_issue_gate 之后：Starlette 后注册的在外层，安全校验最先执行。
    """
    host = request.headers.get("host", "")
    if not _host_allowed(host):
        return JSONResponse({"ok": False, "error": "非法 Host"}, status_code=403)
    path = request.url.path
    method = request.method.upper()
    # 应用密码锁定：只放行解锁/状态与静态资源
    if (_APP_LOCK["enabled"] and not _APP_LOCK["unlocked"]
            and path.startswith("/api/")
            and not path.startswith(_APP_LOCK_OPEN_PATHS)):
        return JSONResponse({"ok": False, "code": "app_locked",
                             "error": "应用已锁定，请先解锁"}, status_code=403)
    # 金库强制门闩：未初始化 / 未完成引导 / 未解锁时，业务 API 一律拒绝
    # 测试钩子 BILI_FAV_ORGANIZER_ALLOW_INSECURE_LOCAL=1 时跳过（与 token 同理）
    if (path.startswith("/api/") and not _insecure_local()
            and not path.startswith(_APP_LOCK_OPEN_PATHS + ("/api/vault/",))):
        try:
            vst = VAULT.state
        except Exception:
            vst = None
        if vst is None or not vst.password_set:
            return JSONResponse({"ok": False, "code": "app_onboarding",
                                 "error": "首次使用请先设置应用密码并保存恢复码"},
                                status_code=403)
        if not vst.onboarding_complete:
            return JSONResponse({"ok": False, "code": "app_onboarding",
                                 "error": "请先完成金库初始化（保存恢复码）"}, status_code=403)
        if not vst.unlocked:
            return JSONResponse({"ok": False, "code": "app_locked",
                                 "error": "金库已锁定，请先解锁"}, status_code=403)
    need_token = (method not in ("GET", "HEAD", "OPTIONS") or
                  path in ("/api/data/export", "/api/cookie"))
    if need_token:
        origin = request.headers.get("origin")
        if origin and not _origin_allowed(origin, request.url.port or _APP_PORT):
            return JSONResponse({"ok": False, "error": "跨源请求被拒绝"}, status_code=403)
        sfs = request.headers.get("sec-fetch-site")
        if sfs and sfs not in ("same-origin", "none"):
            return JSONResponse({"ok": False, "error": "跨站请求被拒绝"}, status_code=403)
        # 仅开发/测试钩子跳过 token；生产始终校验
        if not _insecure_local() and not _token_ok(request):
            return JSONResponse({"ok": False, "error": "缺少或无效的本地 API token"},
                                status_code=403)
    return await call_next(request)


@app.get("/api/scan/issues")
def scan_issues_get():
    issues = _pending_scan_issues()
    return {"issues": issues, "blocked": bool(issues),
            "running": bool((APP.get("scan_run") or {}).get("running"))}


@app.post("/api/scan/issues/{media_id}/recover")
def scan_issue_recover(media_id: str, body: ScanRecoveryIn):
    issue = next((row for row in _pending_scan_issues() if row["media_id"] == media_id), None)
    if not issue:
        return JSONResponse({"ok": False, "error": "该收藏夹没有待处理的扫描异常"}, status_code=404)
    if body.action not in ("retry", "clean", "refresh"):
        return JSONResponse({"ok": False, "error": "不支持的处理方式"}, status_code=400)
    if body.action == "clean":
        if not body.confirmed:
            return JSONResponse({"ok": False, "error": "自动清理会修改 B 站收藏夹，请先确认"}, status_code=400)
        if issue.get("cleanup_unknown"):
            return JSONResponse({"ok": False,
                                 "error": "上次清理结果不确定，请前往 B 站核对后重试扫描，不能重复清理。"}, status_code=409)
    return _start_scan(ScanIn(folder_ids=[media_id], mode="rebuild"), recovery=body.action)


def _folder_view(folders: list) -> dict:
    done = {str(x) for x in store.load_scan_done()}
    states = store.load_folder_scan_states()
    selected = set(store.load_scan_selection())
    rows = [{**f, "media_id": str(f["media_id"]),
             "done": (states.get(str(f["media_id"]), {}).get("status") == "complete" and
                     states.get(str(f["media_id"]), {}).get("expected_count") == int(f.get("count", 0) or 0))
                     or (str(f["media_id"]) in done and not states.get(str(f["media_id"]))),
             "scan_state": states.get(str(f["media_id"]), {}).get("status", "never"),
             "scan_strategy": states.get(str(f["media_id"]), {}).get("strategy"),
             "scanned_count": states.get(str(f["media_id"]), {}).get("fetched_count", 0),
             "scanned_at": states.get(str(f["media_id"]), {}).get("snapshot_completed_at"),
             "selected": str(f["media_id"]) in selected} for f in folders]
    return {"folders": rows, "selected_ids": list(selected)}


@app.get("/api/folders")
def folders_get():
    return _folder_view(store.load_folders())


def _refresh_folder_directory() -> list | None:
    """拉取 B 站收藏夹目录并落库。成功返回目录，失败返回 None（不抛到调用方）。"""
    if APP.get("scan_run") and APP["scan_run"].get("running"):
        return None
    try:
        session = APP.get("session") or bili_api.BiliSession(get_session_cookie())
        folders = session.list_folders()
        store.save_folders(folders)
        APP["folders"] = folders
        return folders
    except Exception as e:
        emit("warn", f"自动刷新收藏夹目录失败：{e}")
        return None


@app.post("/api/folders/refresh")
def folders_refresh():
    if APP["scan_run"] and APP["scan_run"].get("running"):
        return JSONResponse({"ok": False, "error": "扫描运行中，暂不刷新目录"}, status_code=409)
    try:
        session = bili_api.BiliSession(get_session_cookie())
        folders = session.list_folders()
        store.save_folders(folders)
        APP["folders"] = folders
        emit("ok", f"已刷新收藏夹目录：{len(folders)} 个")
        return {"ok": True, **_folder_view(folders)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.put("/api/scan/selection")
def scan_selection(body: ScanSelectionIn):
    valid = {str(f["media_id"]) for f in store.load_folders()}
    ids = list(dict.fromkeys(str(x) for x in body.folder_ids))
    unknown = [x for x in ids if x not in valid]
    if unknown:
        return JSONResponse({"ok": False, "error": f"包含不存在的收藏夹：{unknown[:3]}"}, status_code=400)
    store.save_scan_selection(ids)
    return {"ok": True, "folder_ids": ids}


@app.post("/api/scan")
def scan(body: Optional[ScanIn] = None):
    return _start_scan(body)


def _start_scan(body: Optional[ScanIn] = None, *, recovery: str | None = None):
    """按文件夹选择批量元数据或分页读取，校验后更新该夹快照。"""
    with _scan_start_lock:
        run_id = uuid.uuid4().hex
        app_state = {"id": run_id, "running": True, "step": "init", "done": 0,
                     "total": 0, "error": None, "strategy": "", "stop": False,
                     "folder_done": 0, "folder_total": 0, "current": "",
                     "recovery_action": recovery}
        conflict = _begin_job("scan_run", app_state, error="扫描已在运行中")
        if conflict:
            return conflict

    def run():
        try:
            cfg = load_config()
            session = bili_api.BiliSession(get_session_cookie())
            session.read_interval = max(2, int(cfg.get("scan_interval", 2) or 2))
            APP["session"] = session
            emit("info", f"开始扫描（请求间隔 ≥{session.read_interval} 秒）", kind="scan_start")

            if recovery:
                mid = body.folder_ids[0]
                existing = next(row for row in _pending_scan_issues() if row["media_id"] == mid)
                store.update_scan_issue(mid, **{k: v for k, v in existing.items() if k != "media_id"},
                                        event="开始刷新目录并重新扫描" if recovery == "refresh" else
                                        "开始重试扫描" if recovery == "retry" else "用户已确认自动清理")
                if recovery == "clean":
                    app_state["step"] = "cleaning"
                    session.write_interval = max(2, float(cfg.get("write_interval", 2) or 2))
                    # Persist before sending: a crash/transport failure must never replay this write.
                    store.update_scan_issue(mid, cleanup_unknown=True, event="正在调用 B 站失效内容清理")
                    try:
                        session.clean_invalid_folder(mid, should_stop=lambda: app_state["stop"])
                    except (bili_api.RateLimitedError, bili_api.BiliApiError) as exc:
                        if not isinstance(exc, bili_api.WriteUncertainError):
                            store.update_scan_issue(mid, cleanup_unknown=False)
                        raise
                    store.update_scan_issue(mid, cleanup_unknown=False, event="B 站已确认清理请求，等待重扫验证")

            app_state["step"] = "folders"
            folders = session.list_folders()
            store.save_folders(folders)
            APP["folders"] = folders
            requested = body.folder_ids if body and body.folder_ids is not None else store.load_scan_selection()
            requested = {str(x) for x in requested}
            picked = [f for f in folders if not requested or str(f["media_id"]) in requested]
            if not picked:
                raise ValueError("未选择任何收藏夹")
            if not recovery:
                store.save_scan_selection([str(f["media_id"]) for f in picked])
            app_state["selected_ids"] = [str(f["media_id"]) for f in picked]
            app_state["mode"] = body.mode if body else "resume"
            app_state["step"] = "videos"
            app_state["total"] = sum(int(f.get("count", 0) or 0) for f in picked)
            app_state["folder_total"] = len(picked)
            states = store.load_folder_scan_states()
            rebuild = bool(body and body.mode == "rebuild")
            eligible = [f for f in picked if rebuild or
                        states.get(str(f["media_id"]), {}).get("status") != "complete" or
                        states.get(str(f["media_id"]), {}).get("expected_count") != int(f.get("count", 0) or 0)]
            app_state["folder_done"] = len(picked) - len(eligible)
            done = sum(int(states.get(str(f["media_id"]), {}).get("fetched_count", 0) or 0)
                       for f in picked if f not in eligible)
            app_state["done"] = done
            emit("info", f"扫描范围 {len(picked)} 个夹，{len(eligible)} 个需扫描，目录计数 {app_state['total']} 条")
            should_stop = lambda: bool(app_state.get("stop"))

            for folder in sorted(eligible, key=lambda x: int(x.get("count", 0) or 0)):
                if should_stop():
                    app_state["error"] = "已手动停止（进度已保存）"
                    break
                mid = str(folder["media_id"])
                expected = int(folder.get("count", 0) or 0)
                strategy = ("indexed_ids" if 0 <= expected <= bili_api.FAV_BULK_CANDIDATE_LIMIT
                            else "paged")
                app_state["current"] = folder["title"]
                app_state["strategy"] = strategy
                store.begin_folder_scan(run_id, mid, expected, strategy)
                emit("info", f"开始扫描「{folder['title']}」：" +
                     ("ID 清单 + 本地索引" if strategy == "indexed_ids" else "分页明细"),
                     kind="scan_progress", done=done, total=app_state["total"],
                     fdone=app_state["folder_done"], ftotal=app_state["folder_total"],
                     current=folder["title"], strategy=strategy)

                staged: dict[str, dict] = {}
                fetched = 0

                def add_item(item, old=None):
                    nonlocal fetched
                    key = str(item.get("bvid") or item.get("resource_key") or
                              f"{item.get('type', 2)}:{item.get('id') or item.get('aid') or ''}")
                    old = old or {}
                    rec = {**old, **item}
                    memberships = {str(x) for x in old.get("folder_ids", [])}
                    if old.get("source_folder_id"):
                        memberships.add(str(old["source_folder_id"]))
                    memberships.add(mid)
                    rec["folder_ids"] = sorted(memberships)
                    rec["source_folder_id"] = old.get("source_folder_id") or mid
                    staged[key] = rec
                    fetched += 1

                if strategy == "indexed_ids":
                    try:
                        resource_ids = session.get_folder_resource_ids(mid, expected,
                                                                       should_stop=should_stop)
                        if should_stop():
                            break
                        cached = store.load_video_index_for_resources(resource_ids)
                        missing_ids = [resource for resource in resource_ids
                                       if f"{resource['type']}:{resource['id']}" not in cached]
                        fresh_records = session.get_resource_infos_bulk(
                            mid, missing_ids, should_stop=should_stop)
                        fresh_by_id = {
                            f"{int(item.get('type', 2) or 2)}:{item.get('id') or item.get('aid') or ''}": item
                            for item in fresh_records
                        }
                        for resource in resource_ids:
                            cache_key = f"{resource['type']}:{resource['id']}"
                            item = cached.get(cache_key)
                            if item is not None:
                                item = dict(item)
                                item["id"] = str(resource["id"])
                                item["type"] = int(resource["type"])
                                add_item(item, old=cached[cache_key])
                            else:
                                item = fresh_by_id.get(cache_key)
                                if item is None:
                                    raise bili_api.BiliApiError(f"缺少资源元数据：{cache_key}")
                                add_item(item)
                        fetched = len(resource_ids)
                        cache_hits = len(resource_ids) - len(missing_ids)
                        emit("info", f"「{folder['title']}」索引命中 {cache_hits}/{len(resource_ids)} 条，新增获取 {len(missing_ids)} 条元数据",
                             kind="scan_progress", current=folder["title"], strategy=strategy)
                    except bili_api.RateLimitedError:
                        raise
                    except bili_api.BiliApiError as exc:
                        if "登录态失效" in str(exc):
                            raise
                        emit("warn", f"ID/元数据索引路径未通过完整性检查（{exc}），改用分页明细",
                             kind="scan_progress", current=folder["title"])
                        strategy = "paged_index_fallback"
                        app_state["strategy"] = strategy
                        staged.clear()
                        fetched = 0
                        store.begin_folder_scan(run_id, mid, expected, strategy)
                    else:
                        if staged:
                            store.save_scan_stage(run_id, mid, staged)
                        store.update_folder_scan_progress(mid, len(staged))
                        done += fetched
                        app_state["done"] = done
                        ANALYZE_WAKE.set()
                        emit("progress", "", kind="scan_progress", done=done,
                             total=app_state["total"], fdone=app_state["folder_done"],
                             ftotal=app_state["folder_total"], current=folder["title"],
                             unique=len(staged), strategy=strategy)

                if strategy.startswith("paged"):
                    batch = {}
                    page_index = 1
                    def on_page_event(ev):
                        if ev.get("type") == "req":
                            emit("info", f"「{folder['title']}」分页读取中，预计 {ev['total_pages']} 页",
                                 kind="scan_progress", current=folder["title"], strategy=strategy)
                    for item in session.iter_folder_videos(mid, expected,
                                                          should_stop=should_stop,
                                                          on_event=on_page_event):
                        add_item(item)
                        key = str(item.get("bvid") or item.get("resource_key") or
                                  f"{item.get('type', 2)}:{item.get('id') or item.get('aid') or ''}")
                        batch[key] = staged[key]
                        if len(batch) >= bili_api.PAGE_SIZE:
                            store.save_scan_stage(run_id, mid, batch)
                            batch = {}
                            store.update_folder_scan_progress(mid, len(staged), page_index + 1)
                            page_index += 1
                            done += bili_api.PAGE_SIZE
                            app_state["done"] = done
                            ANALYZE_WAKE.set()
                            emit("progress", "", kind="scan_progress", done=done,
                                 total=app_state["total"], fdone=app_state["folder_done"],
                                 ftotal=app_state["folder_total"], current=folder["title"],
                                 unique=len(staged), strategy=strategy)
                    if batch:
                        store.save_scan_stage(run_id, mid, batch)
                    if should_stop():
                        store.update_folder_scan_progress(mid, len(staged), page_index,
                                                          "用户停止；该夹快照尚未完成")
                        app_state["error"] = "已手动停止（进度已保存）"
                        emit("warn", f"已停止；「{folder['title']}」暂存了 {len(staged)} 条，下次重扫该夹",
                             kind="scan_end")
                        break
                    remainder = fetched - max(0, page_index - 1) * bili_api.PAGE_SIZE
                    done += max(0, remainder)
                    app_state["done"] = done

                if should_stop():
                    store.update_folder_scan_progress(mid, len(staged), None,
                                                      "用户停止；该夹快照尚未完成")
                    app_state["error"] = "已手动停止（进度已保存）"
                    emit("warn", f"已停止；「{folder['title']}」暂存了 {len(staged)} 条，下次重扫该夹",
                         kind="scan_end")
                    break
                unique_count = len(staged)
                if fetched != expected or unique_count != expected:
                    error = f"目录计数 {expected}，接口条数 {fetched}，唯一资源 {unique_count}；保留旧完整快照"
                    store.update_folder_scan_progress(mid, unique_count, None, error)
                    store.mark_folder_scan(mid, "inconsistent", error)
                    store.update_scan_issue(mid, title=folder["title"], status="pending", scan_run_id=run_id,
                                            expected_count=expected, fetched_count=fetched, unique_count=unique_count,
                                            strategy=strategy, last_error=None,
                                            event=f"数量仍不一致：B 站 {expected} 条，读取 {fetched} 条，去重 {unique_count} 条")
                    app_state["error"] = app_state.get("error") or "收藏夹数量不一致，请先处理扫描异常提示"
                    emit("warn", f"「{folder['title']}」{error}", kind="scan_progress", current="")
                    continue

                store.finish_folder_scan(run_id, mid, expected, unique_count)
                if recovery:
                    store.update_scan_issue(mid, last_error=None, cleanup_unknown=False,
                                            event=f"重新扫描验证通过：B 站 {expected} 条，读取 {unique_count} 条")
                app_state["current"] = ""
                app_state["folder_done"] += 1
                ANALYZE_WAKE.set()
                strategy_label = {
                    "indexed_ids": "ID 清单核对 + 本地索引",
                    "bulk_ids_infos": "批量元数据",
                    "paged": "分页明细",
                    "paged_fallback": "批量失败后分页",
                    "paged_index_fallback": "索引路径失败后分页",
                }.get(strategy, strategy)
                emit("ok", f"「{folder['title']}」完成：{unique_count} 条（{strategy_label}）",
                     kind="scan_progress", done=done, total=app_state["total"],
                     fdone=app_state["folder_done"], ftotal=app_state["folder_total"],
                     current="", unique=unique_count, strategy=strategy)

            app_state["step"] = "done"
            app_state["done"] = done
            if not app_state.get("error"):
                picked_ids = {str(f["media_id"]) for f in picked}
                scoped_unique = len([v for v in store.load_videos()
                                     if picked_ids.intersection(str(x) for x in v.get("folder_ids", []))])
                emit("ok", f"扫描完成：当前范围共 {scoped_unique} 条去重内容（目录计数 {done} 条）",
                     kind="scan_end", unique=scoped_unique, done=done)
            else:
                emit("warn", f"扫描结束但有未完整收藏夹：{app_state['error']}", kind="scan_end")
        except bili_api.RateLimitedError as exc:
            app_state["error"] = f"风控：{exc}"
            emit("err", f"风控：{exc}", kind="scan_end")
        except bili_api.BiliApiError as exc:
            app_state["error"] = str(exc)
            emit("err", f"扫描出错：{exc}", kind="scan_end")
        except Exception as exc:
            app_state["error"] = f"{exc}\n{traceback.format_exc()}"
            emit("err", f"扫描异常：{exc}", kind="scan_end")
        finally:
            if recovery and app_state.get("error"):
                mid = body.folder_ids[0]
                store.update_scan_issue(mid, last_error=app_state["error"].split("\n")[0],
                                        event=app_state["error"].split("\n")[0])
            app_state["running"] = False
            app_state["current"] = ""
            ANALYZE_WAKE.set()
            emit("progress", "", kind="scan_idle")

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "started": True, "run_id": run_id}

@app.get("/api/scan/status")
def scan_status():
    return APP["scan_run"] or {"running": False, "error": None}


@app.post("/api/scan/stop")
def scan_stop():
    if APP["scan_run"]:
        APP["scan_run"]["stop"] = True
        return {"ok": True}
    return {"ok": False, "error": "没有运行中的扫描"}


@app.get("/api/scan/tree")
def scan_tree():
    """返回收藏夹树：每个夹的名称/应有条数/已扫条数/是否完成，供前端弹窗展示。"""
    folders = store.load_folders()
    states = store.load_folder_scan_states()
    videos = store.load_videos_raw()
    counts: dict = {}
    for v in videos.values():
        memberships = v.get("folder_ids") or [v.get("source_folder_id", "")]
        for sid in {str(x) for x in memberships}:
            counts[sid] = counts.get(sid, 0) + 1
    tree = []
    for f in sorted(folders, key=lambda x: int(x.get("count", 0))):
        mid = f["media_id"]
        tree.append({
            "media_id": mid,
            "title": f["title"],
            "expected": int(f["count"]),
            "scanned": max(counts.get(str(mid), 0),
                            int(states.get(str(mid), {}).get("fetched_count", 0) or 0)),
            "done": (states.get(str(mid), {}).get("status") == "complete" and
                     states.get(str(mid), {}).get("expected_count") == int(f.get("count", 0) or 0)),
            "status": states.get(str(mid), {}).get("status", "never"),
            "strategy": states.get(str(mid), {}).get("strategy", ""),
            "scanned_at": states.get(str(mid), {}).get("snapshot_completed_at"),
        })
    return {
        "folders": tree,
        "done_count": len([f for f in tree if f["done"]]),
        "total_count": len(folders),
    }


@app.get("/api/library/folders/{media_id}/items")
def library_folder_items(media_id: str, q: str = "", offset: int = 0, limit: int = 100):
    """浏览本地 SQLite 中已扫描的收藏夹内容，不触发 B 站网络请求。"""
    if not any(str(folder.get("media_id")) == str(media_id) for folder in store.load_folders()):
        return JSONResponse({"error": "收藏夹不存在"}, status_code=404)
    items, total = store.load_folder_items(media_id, query=q, offset=offset, limit=limit)
    safe_offset = max(0, int(offset))
    safe_limit = max(1, min(100, int(limit)))
    return {
        "media_id": str(media_id),
        "offset": safe_offset,
        "limit": safe_limit,
        "total": total,
        "items": items,
    }


# ============ 收藏夹整理：合并方案与执行 ============
class FolderMergeGroupIn(BaseModel):
    target_id: str
    source_ids: list[str]
    final_name: str
    delete_sources: bool = True
    reason: str = ""
    confidence: float = 0.0
    level: str = ""
    merge_type: str = ""
    risk: str = ""
    profile_evidence: str = ""
    profile_context_ids: list[str] = Field(default_factory=list)
    profile_context_versions: dict[str, str] = Field(default_factory=dict)
    profile_basis_versions: dict[str, str] = Field(default_factory=dict)


class FolderMergePlanIn(BaseModel):
    groups: list[FolderMergeGroupIn]


@app.get("/api/folder-organize")
def folder_organize_get():
    saved_run = store.load_folder_merge_state()
    if not APP.get("folder_merge_run") and saved_run.get("running"):
        saved_run.update(running=False, status="interrupted")
    current_profiles = {row["id"]: row for row in _current_folder_profile_contexts()}
    current_profile_versions = {mid: row["revision"] for mid, row in current_profiles.items()}
    def decorate(groups):
        decorated = []
        for group in groups:
            row = dict(group)
            context_ids = {str(x) for x in row.get("profile_context_ids", [])}
            context_versions = {str(k): str(v) for k, v in
                                (row.get("profile_context_versions") or {}).items()}
            basis_versions = {str(k): str(v) for k, v in
                              (row.get("profile_basis_versions") or {}).items()}
            basis_match = bool(basis_versions) and basis_versions == current_profile_versions
            versions_match = bool(context_ids) and all(
                current_profiles.get(mid, {}).get("revision") and
                context_versions.get(mid) == current_profiles[mid]["revision"]
                for mid in context_ids)
            row["profile_basis_state"] = (
                "current" if basis_match else
                ("stale" if basis_versions else "none"))
            row["profile_context_state"] = (
                "current" if basis_match or versions_match
                else ("stale" if context_ids or basis_versions else "none"))
            decorated.append(row)
        return decorated
    return {"folders": store.load_folders(), "draft": decorate(store.load_folder_merge_draft()),
            "plan": decorate(store.load_folder_merge_plan()),
            "run": APP.get("folder_merge_run") or saved_run,
            "ai_run": APP.get("folder_merge_ai_run") or {"running": False}}


@app.post("/api/folder-organize/suggest")
def folder_organize_suggest():
    if APP.get("folder_merge_ai_run") and APP["folder_merge_ai_run"].get("running"):
        return JSONResponse({"ok": False, "error": "AI 合并分析已在运行"}, status_code=409)
    readiness = _organization_profile_readiness()
    if not readiness["ready"]:
        names = "、".join(row["name"] for row in readiness["missing"][:8])
        extra = f"；尚未就绪：{names}" if names else ""
        return JSONResponse({"ok": False,
                             "error": readiness["message"] + extra,
                             "readiness": readiness}, status_code=409)
    folders = [f for f in store.load_folders() if int(f.get("count", 0) or 0) > 0]
    videos = store.load_videos()
    current_profiles = {row["id"]: row for row in _current_folder_profile_contexts()}
    all_profile_ids = sorted(current_profiles)
    all_profile_versions = {mid: current_profiles[mid]["revision"] for mid in all_profile_ids}
    if not folders:
        return JSONResponse({"ok": False, "error": "请先刷新收藏夹目录"}, status_code=400)
    llm_cfg = make_llm_config()
    if not llm_cfg.configured:
        return JSONResponse({"ok": False, "error": "LLM 未配置"}, status_code=400)
    state = {"running": True, "error": None, "count": 0}
    conflict = _begin_job("folder_merge_ai_run", state, error="AI 合并分析已在运行")
    if conflict:
        return conflict

    def run():
        try:
            by_folder = {str(f["media_id"]): [] for f in folders}
            overlap_counts: dict[tuple[str, str], int] = {}
            for v in videos:
                memberships = {str(x) for x in v.get("folder_ids", [])}
                if not memberships and v.get("source_folder_id") is not None:
                    memberships.add(str(v.get("source_folder_id")))
                mids = sorted(x for x in memberships if x in by_folder)
                for i, left in enumerate(mids):
                    for right in mids[i + 1:]:
                        overlap_counts[(left, right)] = overlap_counts.get((left, right), 0) + 1
                sample = {"title": (v.get("title") or "")[:100]}
                if v.get("upper_name"): sample["upper"] = str(v["upper_name"])[:40]
                for fid in memberships:
                    bucket = by_folder.get(fid)
                    if bucket is not None:
                        bucket.append(sample)

            def spread_samples(items: list, limit: int = 9) -> list:
                if len(items) <= limit:
                    return items
                indexes = sorted({round(i * (len(items) - 1) / (limit - 1)) for i in range(limit)})
                return [items[i] for i in indexes]

            profiles = []
            for f in folders:
                fid = str(f["media_id"])
                overlaps = []
                for (left, right), n in overlap_counts.items():
                    if fid == left: overlaps.append({"folder_id": right, "count": n})
                    elif fid == right: overlaps.append({"folder_id": left, "count": n})
                overlaps.sort(key=lambda x: x["count"], reverse=True)
                profile_row = {"id": fid, "name": f["title"],
                               "count": int(f.get("count", 0) or 0),
                               "samples": spread_samples(by_folder.get(fid, [])),
                               "overlaps": overlaps[:8]}
                saved_profile = current_profiles.get(fid)
                if saved_profile:
                    profile_row["current_content_profile"] = {
                        "summary": saved_profile.get("summary", ""),
                        "topics": saved_profile.get("topics", []),
                        "typical_content": saved_profile.get("typical_content", []),
                        "out_of_scope": saved_profile.get("out_of_scope", []),
                        "coherence": saved_profile.get("coherence", ""),
                        "confidence": saved_profile.get("confidence"),
                    }
                profiles.append(profile_row)
            raw_groups = llm_analyzer.suggest_folder_merges(llm_cfg, profiles)
            known = {str(f["media_id"]): f for f in folders}
            used = set()
            groups = []
            for raw in raw_groups:
                target = str(raw.get("target_id", ""))
                sources = list(dict.fromkeys(str(x) for x in raw.get("source_ids", [])))
                try: confidence = float(raw.get("confidence", 0) or 0)
                except Exception: confidence = 0.0
                level = str(raw.get("level", "")).lower()
                if level not in ("high", "medium"):
                    level = "high" if confidence >= 0.82 else "medium"
                members = {target, *sources}
                if (target not in known or not sources or target in sources or
                        any(x not in known for x in sources) or used.intersection(members) or
                        known[target]["title"] == "默认收藏夹" or
                        any(known[x]["title"] == "默认收藏夹" for x in sources) or
                        confidence < 0.68):
                    continue
                used.update(members)
                groups.append({"target_id": target, "target_name": known[target]["title"],
                               "source_ids": sources,
                               "source_names": [known[x]["title"] for x in sources],
                               "final_name": str(raw.get("final_name") or known[target]["title"]).strip(),
                               "delete_sources": True, "status": "pending",
                               "reason": str(raw.get("reason", "")).strip(),
                               "profile_evidence": str(raw.get("profile_evidence", "")).strip(),
                               "profile_context_ids": all_profile_ids,
                               "profile_context_versions": all_profile_versions,
                               "profile_basis_versions": all_profile_versions,
                               "risk": str(raw.get("risk", "")).strip(),
                               "merge_type": str(raw.get("merge_type", "")).strip(),
                               "level": level, "confidence": confidence})
            store.save_folder_merge_draft(groups)
            state["count"] = len(groups)
            emit("ok", f"AI 合并分析完成：生成 {len(groups)} 个候选组",
                 phase="folder_merge_ai", kind="folder_merge_ai_end")
        except Exception as e:
            state["error"] = str(e)
            emit("err", f"AI 合并分析失败：{e}", phase="folder_merge_ai",
                 kind="folder_merge_ai_end")
        finally:
            state["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "started": True}


@app.put("/api/folder-organize/draft")
def folder_organize_draft(body: FolderMergePlanIn):
    known = {str(f["media_id"]): f for f in store.load_folders()}
    groups = []
    for raw in body.groups:
        target = str(raw.target_id)
        sources = list(dict.fromkeys(str(x) for x in raw.source_ids if str(x) != target))
        members = {target, *sources}
        groups.append({"target_id": target,
                       "target_name": (known.get(target) or {}).get("title", ""),
                       "source_ids": sources,
                       "source_names": [(known.get(x) or {}).get("title", x) for x in sources],
                       "final_name": raw.final_name.strip(), "delete_sources": raw.delete_sources,
                       "reason": raw.reason.strip(), "confidence": raw.confidence,
                       "profile_evidence": raw.profile_evidence.strip(),
                       "profile_context_ids": sorted(members.intersection(str(x) for x in raw.profile_context_ids)),
                       "profile_context_versions": {
                           str(mid): str(version) for mid, version in raw.profile_context_versions.items()
                           if str(mid) in members},
                       "profile_basis_versions": {
                           str(mid): str(version) for mid, version in raw.profile_basis_versions.items()},
                       "risk": raw.risk.strip(), "merge_type": raw.merge_type.strip(),
                       "level": raw.level.strip(),
                       "status": "pending"})
    store.save_folder_merge_draft(groups)
    return {"ok": True, "groups": groups}


@app.put("/api/folder-organize/plan")
def folder_organize_plan(body: FolderMergePlanIn):
    if body.groups:
        readiness = _organization_profile_readiness()
        if not readiness["ready"]:
            return JSONResponse({"ok": False, "error": readiness["message"],
                                 "readiness": readiness}, status_code=409)
    known = {str(f["media_id"]): f for f in store.load_folders()}
    current_contexts = _current_folder_profile_contexts()
    profile_ids = {row["id"] for row in current_contexts}
    profile_versions = {row["id"]: row["revision"] for row in current_contexts}
    old_groups = store.load_folder_merge_plan()
    old_by_key = {(str(g.get("target_id")), tuple(sorted(str(x) for x in g.get("source_ids", []))),
                   (g.get("final_name") or "").strip()): g for g in old_groups}
    used_folders = set()
    groups = []
    for i, raw in enumerate(body.groups, 1):
        target = str(raw.target_id)
        sources = list(dict.fromkeys(str(x) for x in raw.source_ids))
        if target not in known or target not in profile_ids:
            return JSONResponse({"ok": False, "error": f"第 {i} 组目标夹不存在或没有当前画像"}, status_code=400)
        if not sources or any(x not in known or x not in profile_ids for x in sources):
            return JSONResponse({"ok": False, "error": f"第 {i} 组来源夹无效或没有当前画像"}, status_code=400)
        basis_versions = {str(k): str(v) for k, v in raw.profile_basis_versions.items()}
        if basis_versions and basis_versions != profile_versions:
            return JSONResponse({"ok": False,
                                 "error": f"第 {i} 组建议使用的画像版本已变化，请重新生成 AI 合并建议"},
                                status_code=409)
        if (not basis_versions and
                (raw.confidence > 0 or raw.level or raw.merge_type or raw.profile_evidence)):
            return JSONResponse({"ok": False,
                                 "error": f"第 {i} 组是旧 AI 建议且缺少画像版本记录，请重新生成 AI 合并建议"},
                                status_code=409)
        if target in sources:
            return JSONResponse({"ok": False, "error": f"第 {i} 组的目标夹不能同时是来源夹"}, status_code=400)
        members = {target, *sources}
        if used_folders.intersection(members):
            return JSONResponse({"ok": False, "error": f"第 {i} 组包含已被其他组使用的收藏夹"}, status_code=400)
        used_folders.update(members)
        group = {"target_id": target, "target_name": known[target]["title"],
                       "source_ids": sources,
                       "source_names": [known[x]["title"] for x in sources],
                       "final_name": raw.final_name.strip() or known[target]["title"],
                       "delete_sources": raw.delete_sources, "status": "pending",
                       "reason": raw.reason.strip(), "confidence": raw.confidence,
                       "profile_evidence": raw.profile_evidence.strip(),
                       "profile_context_ids": sorted(profile_ids),
                       "profile_context_versions": profile_versions,
                       "profile_basis_versions": profile_versions}
        group.update(risk=raw.risk.strip(), merge_type=raw.merge_type.strip(), level=raw.level.strip())
        key = (target, tuple(sorted(sources)), group["final_name"])
        previous = old_by_key.get(key)
        if previous and previous.get("status") in ("done", "unknown"):
            group.update(status=previous["status"], results=previous.get("results", []))
            if previous.get("error"): group["error"] = previous["error"]
        groups.append(group)
    store.save_folder_merge_plan(groups)
    return {"ok": True, "groups": groups}


@app.post("/api/folder-organize/start")
def folder_organize_start():
    if APP.get("folder_merge_run") and APP["folder_merge_run"].get("running"):
        return JSONResponse({"ok": False, "error": "收藏夹合并已在运行"}, status_code=409)
    if any(APP.get(k) and APP[k].get("running") for k in ("scan_run", "apply_run")):
        return JSONResponse({"ok": False, "error": "扫描或内容执行正在运行，请稍后再合并"}, status_code=409)
    plan = store.load_folder_merge_plan()
    pending = [g for g in plan if g.get("status") not in ("done", "unknown")]
    if not pending:
        return JSONResponse({"ok": False, "error": "没有待执行的收藏夹合并方案"}, status_code=400)
    readiness = _organization_profile_readiness()
    if not readiness["ready"]:
        return JSONResponse({"ok": False, "error": readiness["message"],
                             "readiness": readiness}, status_code=409)
    profile_ids = {row["id"] for row in _current_folder_profile_contexts()}
    if any(str(g.get("target_id", "")) not in profile_ids or
           any(str(mid) not in profile_ids for mid in g.get("source_ids", []))
           for g in pending):
        return JSONResponse({"ok": False, "error": "待执行方案包含没有当前画像的收藏夹，请重新生成并提交方案"}, status_code=409)
    current_versions = readiness["profile_versions"]
    if any(set(str(x) for x in g.get("profile_context_ids", [])) != set(current_versions) or
           {str(k): str(v) for k, v in (g.get("profile_context_versions") or {}).items()} != current_versions
           for g in pending):
        return JSONResponse({"ok": False, "error": "合并方案使用的画像版本已变化，请重新生成并提交合并建议"}, status_code=409)
    try:
        session = bili_api.BiliSession(get_session_cookie())
        cfg = load_config()
        session.read_interval = int(cfg.get("scan_interval", 2) or 2)
        session.write_interval = float(cfg.get("folder_merge_interval", 2) or 2)
        owner_mid = session.get_mid()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Cookie 不可用：{e}"}, status_code=400)
    state = {"running": True, "stop": False, "done": 0,
             "total": len(pending), "error": None, "status": "running"}
    conflict = _begin_job("folder_merge_run", state, error="收藏夹合并已在运行")
    if conflict:
        return conflict

    def save():
        store.save_folder_merge_plan(plan)
        store.save_folder_merge_state(dict(state))

    def chunks(items, size=1000):
        for i in range(0, len(items), size):
            yield items[i:i + size]

    def run():
        try:
            # 先把远端目录变化同步到 SQLite，并在所有写请求之前重验画像版本。
            live_folders = session.list_folders()
            store.save_folders(live_folders)
            readiness_now = _organization_profile_readiness()
            if not readiness_now["ready"]:
                state.update(status="blocked", error=readiness_now["message"], stop=True)
                emit("err", f"收藏夹合并已阻止：{readiness_now['message']}", phase="folder_merge")
                return
            if any(set(str(x) for x in g.get("profile_context_ids", [])) !=
                   set(readiness_now["profile_versions"]) or
                   {str(k): str(v) for k, v in (g.get("profile_context_versions") or {}).items()} !=
                   readiness_now["profile_versions"] for g in pending):
                state.update(status="blocked", error="画像版本在执行前发生变化，请重新生成合并建议",
                             stop=True)
                emit("err", "画像版本在执行前发生变化，合并未发送写请求；请重新生成合并建议",
                     phase="folder_merge")
                return
            emit("info", f"开始收藏夹合并：{len(pending)} 组", phase="folder_merge")
            for group in pending:
                if state["stop"]: break
                group["status"] = "running"
                group["results"] = []
                group_ok = True
                live = {str(f["media_id"]): f for f in session.list_folders()}
                target_id = str(group["target_id"])
                if target_id not in live:
                    group.update(status="failed", error="目标收藏夹不存在")
                    state["done"] += 1; save(); continue
                for source_id in group["source_ids"]:
                    if state["stop"]: break
                    live = {str(f["media_id"]): f for f in session.list_folders()}
                    source = live.get(str(source_id))
                    result = {"source_id": str(source_id), "status": "running"}
                    group["results"].append(result)
                    if not source:
                        result["status"] = "already_absent"; save(); continue
                    count = int(source.get("count", 0) or 0)
                    videos = list(session.iter_folder_videos(str(source_id), count,
                                  should_stop=lambda: state["stop"])) if count else []
                    normal = [int(v.get("aid", 0) or 0) for v in videos
                              if int(v.get("aid", 0) or 0) > 0 and not _is_invalid(v)]
                    invalid = [int(v.get("aid", 0) or 0) for v in videos
                               if int(v.get("aid", 0) or 0) > 0 and _is_invalid(v)]
                    hidden_invalid = max(0, count - len(videos))
                    result.update(before=count, movable=len(normal), invalid=len(invalid) + hidden_invalid)
                    try:
                        for batch in chunks(normal):
                            session.move_batch(str(source_id), target_id, batch, mid=owner_mid,
                                               should_stop=lambda: state["stop"])
                        for batch in chunks(invalid):
                            session.batch_delete(str(source_id), batch,
                                                 should_stop=lambda: state["stop"])
                        if hidden_invalid:
                            session.clean_invalid_folder(str(source_id),
                                                         should_stop=lambda: state["stop"])
                        # B 站的收藏夹计数在 move 后可能短暂延迟，等待其收敛再决定是否删夹。
                        after = count
                        for attempt in range(6):
                            live_after = {str(f["media_id"]): f for f in session.list_folders()}
                            after = int((live_after.get(str(source_id)) or {}).get("count", 0) or 0)
                            if after == 0 or state["stop"]:
                                break
                            if attempt < 5:
                                time.sleep(2)
                        result["after"] = after
                        if after:
                            result.update(status="not_empty", error=f"仍有 {after} 条，未删除夹")
                            group_ok = False
                        elif group.get("delete_sources", True):
                            session.delete_folder(str(source_id), should_stop=lambda: state["stop"])
                            result["status"] = "merged_and_deleted"
                        else:
                            result["status"] = "merged_kept"
                    except (bili_api.WriteUncertainError, bili_api.RateLimitedError):
                        raise
                    except bili_api.BiliApiError as e:
                        result.update(status="failed", error=str(e)); group_ok = False
                    save()
                if state["stop"]: break
                if group_ok:
                    final_name = (group.get("final_name") or "").strip()
                    current = {str(f["media_id"]): f for f in session.list_folders()}.get(target_id)
                    if current and final_name and current.get("title") != final_name:
                        session.rename_folder(target_id, final_name, should_stop=lambda: state["stop"])
                    group["status"] = "done"
                else:
                    group["status"] = "partial"
                state["done"] += 1
                emit("ok" if group_ok else "warn",
                     f"合并组完成：{group.get('final_name')}", phase="folder_merge")
                save()
            state["status"] = "stopped" if state["stop"] else "done"
        except bili_api.WriteUncertainError as e:
            state.update(status="unknown", error=str(e), stop=True)
            for g in plan:
                if g.get("status") == "running": g["status"] = "unknown"
            emit("err", f"合并结果不确定，已停止：{e}", phase="folder_merge")
        except bili_api.RateLimitedError as e:
            state.update(status="rate_limited", error=str(e), stop=True)
            emit("err", f"触发风控，已停止：{e}", phase="folder_merge")
        except Exception as e:
            state.update(status="error", error=str(e), stop=True)
            emit("err", f"收藏夹合并异常：{e}", phase="folder_merge")
        finally:
            state["running"] = False
            save()
            try:
                folders = session.list_folders(); store.save_folders(folders); APP["folders"] = folders
            except Exception: pass
            emit("ok" if state["status"] == "done" else "warn", "收藏夹合并任务已结束",
                 phase="folder_merge", kind="folder_merge_end")

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "started": True, "total": len(pending)}


@app.post("/api/folder-organize/stop")
def folder_organize_stop():
    state = APP.get("folder_merge_run")
    if state and state.get("running"):
        state["stop"] = True
        return {"ok": True}
    return {"ok": False, "error": "没有运行中的收藏夹合并"}


# ============ 连接测试 ============
@app.post("/api/test/cookie")
def test_cookie():
    """测试已保存 Cookie 与 B站登录态(nav)。"""
    try:
        ci = get_session_cookie()
        s = bili_api.BiliSession(ci)
        mid = s.get_mid()
        if not mid:
            return {"ok": False, "error": "cookie 读到了，但 B站未返回 mid（可能未登录）"}
        return {"ok": True, "mid": mid, "message": f"Cookie 可用，已登录 mid={mid}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/test/llm")
def test_llm(body: Optional[ModelTestIn] = None):
    """测试选中的模型；未传模型 ID 时测试当前激活模型。"""
    raw = load_config()
    if body and body.model_id:
        if not store.get_model(body.model_id):
            return {"ok": False, "error": "所选模型不存在"}
        raw["active_model_id"] = body.model_id
    cfg = resolved_llm_settings(raw)
    if not cfg.get("base_url") or not cfg.get("model"):
        return {"ok": False, "error": "请先选择供应商与模型"}
    try:
        import requests as _rq
        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if cfg.get("api_key"):
            headers["Authorization"] = "Bearer " + cfg["api_key"]
        test_config = llm_analyzer.LLMConfig(
            base_url=cfg["base_url"], api_key=cfg.get("api_key", ""),
            model=cfg["model"], params=cfg.get("llm_params") or {}, max_tokens=256,
            provider_name=cfg.get("_provider_name", ""),
            capabilities=dict(cfg.get("model_capabilities") or {}))
        payload = _build_model_test_payload(test_config)
        r = _rq.post(url, json=payload, headers=headers,
                     timeout=MODEL_TEST_TIMEOUT_SECONDS)
        if not r.ok:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        content = llm_analyzer.extract_chat_completion_content(r.json())
        if not isinstance(llm_analyzer.extract_json_object(content), dict):
            return {"ok": False, "error": "接口可连接，但模型未返回可解析的 JSON 对象"}
        return {"ok": True, "message": f"模型连接成功，JSON 输出有效（{cfg['model']}）"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============ 扫码登录获取 Cookie ============
LOGIN = {"session": None, "qrcode_key": ""}
PASSPORT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


@app.post("/api/login/qr/generate")
def login_qr_generate():
    """生成 B站登录二维码（返回 base64 图片 + qrcode_key）。"""
    try:
        import base64
        import io
        import qrcode

        s = requests.Session()
        s.headers.update({"User-Agent": PASSPORT_UA, "Referer": "https://www.bilibili.com/"})
        r = s.get("https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
                  timeout=15)
        d = r.json()
        if d.get("code") != 0:
            return {"ok": False, "error": f"生成二维码失败：{d.get('message')}"}
        data = d["data"]
        LOGIN["session"] = s
        LOGIN["qrcode_key"] = data["qrcode_key"]

        img = qrcode.make(data["url"])
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        return {"ok": True, "image": "data:image/png;base64," + b64,
                "key": data["qrcode_key"]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/login/qr/poll")
def login_qr_poll():
    """轮询扫码状态；成功后解析并保存 cookie。"""
    s = LOGIN.get("session")
    if not s:
        return {"ok": False, "status": "error", "message": "请先生成二维码"}
    try:
        import urllib.parse
        r = s.get("https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                  params={"qrcode_key": LOGIN.get("qrcode_key", "")}, timeout=15)
        d = r.json()
        data = d.get("data") or {}
        code = data.get("code")
        if code == 0:
            url = data.get("url", "")
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            sessdata = (q.get("SESSDATA") or [""])[0] or s.cookies.get("SESSDATA", "")
            jct = (q.get("bili_jct") or [""])[0] or s.cookies.get("bili_jct", "")
            uid = (q.get("DedeUserID") or [""])[0] or s.cookies.get("DedeUserID", "")
            if sessdata and jct and uid:
                cur = load_config()
                cur["cookie_string"] = f"SESSDATA={sessdata}; bili_jct={jct}; DedeUserID={uid}"
                save_config(cur)
                LOGIN["session"] = None
                return {"ok": True, "status": "ok", "message": "登录成功，Cookie 已保存",
                        "mid": str(uid)}
            return {"ok": False, "status": "error", "message": "登录成功但未解析到 cookie"}
        mapping = {86101: ("waiting", "未扫码"),
                   86090: ("scanned", "已扫码，请在手机上确认"),
                   86038: ("expired", "二维码已过期，请刷新")}
        st, msg = mapping.get(code, ("unknown", f"未知状态 {code}"))
        return {"ok": True, "status": st, "message": msg}
    except Exception as e:
        return {"ok": False, "status": "error", "message": str(e)}


@app.get("/api/login/status")
def login_status():
    cfg = load_config()
    ck = cfg.get("cookie_string", "") or ""
    mid = ""
    for part in ck.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "DedeUserID":
            mid = v.strip()
            break
    return {"configured": bool(ck.strip()), "mid": mid}


# ============ 收藏夹画像 ============
class FolderProfileGenerateIn(BaseModel):
    folder_ids: list[str]
    rebuild: bool = False


def _folder_profile_data_complete(profile: dict | None) -> bool:
    if not profile:
        return False
    if (not str(profile.get("summary", "")).strip() or
            not isinstance(profile.get("topics"), list) or len(profile.get("topics", [])) < 2 or
            not isinstance(profile.get("typical_content"), list) or
            not isinstance(profile.get("out_of_scope"), list) or
            profile.get("coherence") not in ("coherent", "mixed", "insufficient")):
        return False
    try:
        if not 0 <= float(profile.get("confidence")) <= 1:
            return False
    except (TypeError, ValueError):
        return False
    return True


def _folder_profile_is_current(profile: dict | None, folder: dict, scan: dict, item_count: int) -> bool:
    if not _folder_profile_data_complete(profile):
        return False
    try:
        source_count = int(profile.get("source_item_count", -1))
    except (TypeError, ValueError):
        return False
    return (str(profile.get("folder_name", "")) == str(folder.get("title", "")) and
            source_count == int(item_count) and
            str(profile.get("source_snapshot_at", "")) ==
            str(scan.get("snapshot_completed_at", "")))


def _current_folder_profile_contexts() -> list[dict]:
    """Profiles are useful to automation only when bound to the current complete snapshot."""
    folders = store.load_folders()
    default_ids = _default_folder_ids(folders)
    profiles = store.load_folder_profiles()
    scans = store.load_folder_scan_states()
    item_counts = store.load_folder_item_counts()
    result = []
    for folder in folders:
        mid = str(folder["media_id"])
        # 默认收藏夹是「未分拣收件箱」，不是归类目标：它不携带画像，
        # 里面的内容要按其余收藏夹的画像去归属。
        if mid in default_ids:
            continue
        profile = profiles.get(mid)
        count = item_counts.get(mid, 0)
        scan = scans.get(mid, {})
        remote_count = int(folder.get("count", 0) or 0)
        # Empty folders contain no evidence and are not classification targets.
        if remote_count <= 0:
            continue
        complete = (scan.get("status") == "complete" and
                    int(scan.get("expected_count") or 0) == remote_count and
                    count == remote_count)
        if not complete or not _folder_profile_is_current(profile, folder, scan, count):
            continue
        result.append({
            "id": mid,
            "name": str(folder.get("title", "")),
            "summary": str(profile.get("summary", "")),
            "topics": list(profile.get("topics") or []),
            "typical_content": list(profile.get("typical_content") or []),
            "out_of_scope": list(profile.get("out_of_scope") or []),
            "coherence": str(profile.get("coherence", "")),
            "confidence": profile.get("confidence"),
            "revision": str(profile.get("profile_revision") or profile.get("generated_at") or
                             profile.get("updated_at") or ""),
        })
    return result


def _organization_profile_readiness() -> dict:
    """Require a complete current profile for every non-empty active folder.

    默认收藏夹是未分拣收件箱，不要求画像，也不算作归类目标。
    """
    folders = store.load_folders()
    default_ids = _default_folder_ids(folders)
    profiles = store.load_folder_profiles()
    scans = store.load_folder_scan_states()
    item_counts = store.load_folder_item_counts()
    required = []
    missing = []
    current_versions = {}
    empty_count = 0
    inbox_count = 0
    for folder in folders:
        mid = str(folder["media_id"])
        remote_count = int(folder.get("count", 0) or 0)
        if remote_count <= 0:
            empty_count += 1
            continue
        if mid in default_ids:
            inbox_count += 1
            continue
        required.append(mid)
        scan = scans.get(mid, {})
        local_count = int(item_counts.get(mid, 0) or 0)
        complete = (scan.get("status") == "complete" and
                    int(scan.get("expected_count") or 0) == remote_count and
                    local_count == remote_count)
        profile = profiles.get(mid)
        current = complete and _folder_profile_is_current(profile, folder, scan, local_count)
        if current:
            current_versions[mid] = str(profile.get("profile_revision") or profile.get("generated_at") or
                                        profile.get("updated_at") or "")
            continue
        if not complete:
            reason = "扫描未完成或本地数量与目录不一致"
        elif not profile:
            reason = "尚未生成画像"
        elif not _folder_profile_data_complete(profile):
            reason = "画像结构不完整，请重建"
        else:
            reason = "画像对应的名称、成员数或扫描快照已变化"
        missing.append({"id": mid, "name": str(folder.get("title", "")),
                        "count": remote_count, "local_count": local_count,
                        "scan_complete": complete,
                        "profile_state": "stale" if profile else "missing",
                        "reason": reason})
    # 只有收件箱（默认收藏夹）有内容时也算就绪：LLM 仍可给出「新建收藏夹」建议，
    # 不会因为缺少可移入的现成夹而完全无法整理。
    scan_issues = _pending_scan_issues()
    ready = (not missing) and not scan_issues and bool(required or inbox_count)
    return {"ready": ready, "required_count": len(required),
            "current_count": len(required) - len(missing),
            "empty_count": empty_count, "inbox_count": inbox_count,
            "inbox_folder_ids": sorted(default_ids),
            "missing": missing, "scan_issues": scan_issues,
            "profile_versions": current_versions,
            "message": ("收藏夹扫描异常尚未解决，请先处理扫描异常提示。" if scan_issues else
                        ("所有有内容的 active 收藏夹均有当前完整画像。默认收藏夹是未分拣收件箱，"
                         "其中内容会按其余收藏夹的画像归类。") if ready else
                        ("没有可整理的收藏夹内容。" if not (required or inbox_count) else
                         ("默认收藏夹是未分拣收件箱，其中内容会按其余收藏夹的画像归类，"
                          "如需调整去向请在预归类方案中修改。" if not required else
                          "请先完成未扫描的收藏夹扫描，再生成或重建这些收藏夹的画像。")))}


@app.get("/api/organization/readiness")
def organization_readiness_get():
    return _organization_profile_readiness()


@app.get("/api/folder-profiles")
def folder_profiles_get():
    folders = store.load_folders()
    default_ids = _default_folder_ids(folders)
    profiles = store.load_folder_profiles()
    scans = store.load_folder_scan_states()
    item_counts = store.load_folder_item_counts()
    rows = []
    for folder in folders:
        mid = str(folder["media_id"])
        profile = profiles.get(mid)
        scan = scans.get(mid, {})
        item_count = item_counts.get(mid, 0)
        complete = (scan.get("status") == "complete" and
                    int(scan.get("expected_count") or 0) == int(folder.get("count", 0) or 0) and
                    item_count == int(folder.get("count", 0) or 0))
        current = complete and _folder_profile_is_current(profile, folder, scan, item_count)
        rows.append({**folder, "local_count": item_count, "scan_complete": complete,
                     "scan_state": scan.get("status", "never"),
                     "scan_completed_at": scan.get("snapshot_completed_at"),
                     "profile": profile,
                     "is_default": mid in default_ids,
                     "profile_state": "current" if current else ("stale" if profile else "missing")})
    return {"folders": rows, "run": APP.get("folder_profile_run") or {"running": False}}


@app.delete("/api/folder-profiles/{media_id}")
def folder_profile_delete(media_id: str):
    """删除某个收藏夹的本地画像；不会改动 B 站收藏夹简介。"""
    folders = store.load_folders()
    folder = next((row for row in folders if str(row.get("media_id")) == str(media_id)), None)
    if not folder:
        return JSONResponse({"ok": False, "error": "收藏夹不存在或已归档"}, status_code=404)
    if not store.delete_folder_profile(str(media_id)):
        return JSONResponse({"ok": False, "error": "该收藏夹当前没有可删除的画像"}, status_code=404)
    emit("warn", f"已删除「{folder.get('title')}」的收藏夹画像；如需恢复请重新生成")
    return {"ok": True, "media_id": str(media_id), "title": folder.get("title", "")}


class FolderIntroPublishIn(BaseModel):
    intro: str = Field(max_length=200)


@app.get("/api/folder-profiles/{media_id}/bilibili")
def folder_profile_bilibili_info(media_id: str):
    """按需读取 B 站收藏夹简介，用于上传前对比。"""
    folder = next((row for row in store.load_folders()
                   if str(row.get("media_id")) == str(media_id)), None)
    if not folder:
        return JSONResponse({"ok": False, "error": "收藏夹不存在或已归档"}, status_code=404)
    try:
        session = bili_api.BiliSession(get_session_cookie())
        session.read_interval = int(load_config().get("scan_interval", 2) or 2)
        remote = session.get_folder_info(media_id)
        attr = remote.get("attr")
        try:
            privacy = int(attr) & 1 if attr is not None else None
        except (TypeError, ValueError):
            privacy = None
        return {"ok": True, "media_id": str(media_id),
                "title": str(remote.get("title") or ""),
                "intro": str(remote.get("intro") or ""),
                "privacy": privacy}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/folder-profiles/{media_id}/publish-intro")
def folder_profile_publish_intro(media_id: str, body: FolderIntroPublishIn):
    """把用户确认的画像简介写回 B 站；保留远端标题、隐私状态与封面。"""
    folder = next((row for row in store.load_folders()
                   if str(row.get("media_id")) == str(media_id)), None)
    if not folder:
        return JSONResponse({"ok": False, "error": "收藏夹不存在或已归档"}, status_code=404)
    profile = store.load_folder_profiles().get(str(media_id))
    if not profile or not str(profile.get("summary") or "").strip():
        return JSONResponse({"ok": False, "error": "该收藏夹还没有可上传的画像简介"}, status_code=409)
    try:
        session = bili_api.BiliSession(get_session_cookie())
        cfg = load_config()
        session.read_interval = int(cfg.get("scan_interval", 2) or 2)
        session.write_interval = float(cfg.get("write_interval", 2) or 2)
        remote = session.get_folder_info(media_id)
        remote_title = str(remote.get("title") or "").strip()
        local_title = str(folder.get("title") or "").strip()
        if not remote_title or remote_title != local_title:
            return JSONResponse({"ok": False,
                                 "error": "B 站收藏夹名称与本地目录不一致；请先刷新收藏夹目录，再检查画像后上传"},
                                status_code=409)
        result = session.update_folder_intro(media_id, remote, body.intro)
        saved_intro = str(result.get("intro", body.intro))
        emit("ok", f"已上传「{local_title}」的收藏夹简介到 B 站（{len(body.intro)} 字）")
        return {"ok": True, "media_id": str(media_id), "title": local_title,
                "intro": saved_intro}
    except bili_api.WriteUncertainError as exc:
        return JSONResponse({"ok": False,
                             "error": f"B 站未返回写入确认，结果可能已保存。请重新打开预览核对后再决定是否重试：{exc}"},
                            status_code=502)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/folder-profiles/generate")
def folder_profiles_generate(body: FolderProfileGenerateIn):
    current = APP.get("folder_profile_run") or {}
    if current.get("running"):
        return JSONResponse({"ok": False, "error": "收藏夹画像任务已在运行"}, status_code=409)
    folders = store.load_folders()
    active = {str(folder["media_id"]): folder for folder in folders}
    default_ids = _default_folder_ids(folders)
    requested = list(dict.fromkeys(str(mid) for mid in body.folder_ids))
    if not requested:
        return JSONResponse({"ok": False, "error": "请至少选择一个 active 收藏夹"}, status_code=400)
    if any(mid not in active for mid in requested):
        return JSONResponse({"ok": False, "error": "所选收藏夹不存在或已归档，请刷新列表"}, status_code=400)
    if any(mid in default_ids for mid in requested):
        return JSONResponse({"ok": False,
                             "error": "默认收藏夹是未分拣收件箱，不生成画像；"
                                      "请取消勾选默认收藏夹，只对目标收藏夹生成画像"}, status_code=400)
    cfg = load_config()
    llm_cfg = make_llm_config(cfg)
    context_tokens = int(resolved_llm_settings(cfg).get("model_context_tokens", 32768) or 32768)
    tpm_limit_tokens = int(cfg.get("model_tpm_limit", 20000) or 20000)
    request_interval = float(cfg.get("profile_request_interval", 2.0) or 0)
    if not llm_cfg.configured:
        return JSONResponse({"ok": False, "error": "LLM 未配置，请先选择供应商与模型"}, status_code=400)

    state = {
        "running": True, "stop": False, "rebuild": bool(body.rebuild),
        "total": len(requested), "done": 0, "generated": 0,
        "skipped": 0, "failed": 0, "current": "", "error": None,
    }
    conflict = _begin_job("folder_profile_run", state, error="收藏夹画像任务已在运行")
    if conflict:
        return conflict

    def run():
        try:
            scans = store.load_folder_scan_states()
            profiles = store.load_folder_profiles()
            for index, mid in enumerate(requested, 1):
                if state["stop"]:
                    break
                folder = active[mid]
                state["current"] = folder.get("title", mid)
                total_items, samples = store.load_folder_profile_samples(mid)
                scan = scans.get(mid, {})
                old_profile = profiles.get(mid)
                remote_count = int(folder.get("count", 0) or 0)
                complete = (scan.get("status") == "complete" and
                            int(scan.get("expected_count") or 0) == remote_count and
                            total_items == remote_count)
                if total_items <= 0:
                    state["skipped"] += 1
                    emit("warn", f"「{folder.get('title')}」没有本地条目，未生成画像",
                         phase="folder_profile", kind="folder_profile_progress")
                elif not complete:
                    state["skipped"] += 1
                    emit("warn", f"「{folder.get('title')}」扫描不完整（本地 {total_items} / 目录 {remote_count}），请先完成扫描",
                         phase="folder_profile", kind="folder_profile_progress")
                elif not body.rebuild and _folder_profile_is_current(old_profile, folder, scan, total_items):
                    state["skipped"] += 1
                    emit("info", f"「{folder.get('title')}」画像已是最新，跳过；如需重算请使用重建",
                         phase="folder_profile", kind="folder_profile_progress")
                else:
                    compact = [{
                        "title": str(item.get("title", ""))[:140],
                        "description": str(item.get("desc", ""))[:260],
                        "uploader": str(item.get("upper_name", ""))[:60],
                        "tags": [str(tag)[:80] for tag in (item.get("tags") or [])[:12]]
                                if isinstance(item.get("tags"), list) else [],
                    } for item in samples]
                    try:
                        emit("info", f"「{folder.get('title')}」开始生成画像，读取 {len(compact)}/{total_items} 条本地内容",
                             phase="folder_profile")
                        result = llm_analyzer.generate_folder_profile(
                            llm_cfg, folder, compact, total_items,
                            context_window_tokens=context_tokens,
                            tpm_limit_tokens=tpm_limit_tokens,
                            request_interval=request_interval,
                            retry_callback=lambda attempt, total, delay, detail: emit(
                                "warn", f"「{folder.get('title')}」模型请求失败，{int(delay)} 秒后重试（第 {attempt}/{total} 次）：{detail}",
                                phase="folder_profile"),
                            throttle_callback=lambda delay, reason: emit(
                                "info", f"「{folder.get('title')}」触发本地 {reason} 保护，约 {int(delay)} 秒后继续发送",
                                phase="folder_profile"),
                            progress_callback=lambda stage, done, total: emit(
                                "info", f"「{folder.get('title')}」{stage} {done}/{total} 批",
                                phase="folder_profile"),
                            should_stop=lambda: state["stop"])
                        used_sample_count = int(result.pop("sample_count", len(compact)))
                        api_prompt_tokens = int(result.pop("api_prompt_tokens", 0) or 0)
                        api_completion_tokens = int(result.pop("api_completion_tokens", 0) or 0)
                        api_calls = int(result.pop("api_calls", 0) or 0)
                        profile = {
                            **result,
                            "folder_name": str(folder.get("title", "")),
                            "source_item_count": total_items,
                            "remote_item_count": remote_count,
                            "sample_count": used_sample_count,
                            "source_snapshot_at": str(scan.get("snapshot_completed_at", "")),
                            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "profile_revision": uuid.uuid4().hex,
                            "model": llm_cfg.model,
                        }
                        store.save_folder_profile(mid, profile)
                        profiles[mid] = profile
                        state["generated"] += 1
                        emit("info", f"「{folder.get('title')}」使用 {used_sample_count}/{total_items} 条内容生成画像",
                             phase="folder_profile")
                        emit("info", f"「{folder.get('title')}」模型请求 {api_calls} 次；实际用量：输入 {api_prompt_tokens:,}，输出 {api_completion_tokens:,} tokens",
                             phase="folder_profile")
                        emit("ok", f"「{folder.get('title')}」画像已{'重建' if body.rebuild else '生成'}",
                             phase="folder_profile", kind="folder_profile_progress")
                    except llm_analyzer.ProfileCancelledError as exc:
                        state["skipped"] += 1
                        emit("warn", f"「{folder.get('title')}」{exc}；不会再启动新的模型请求",
                             phase="folder_profile", kind="folder_profile_progress")
                    except llm_analyzer.ProfileRateLimitError as exc:
                        state["failed"] += 1
                        state["error"] = str(exc)
                        state["stop"] = True
                        emit("err", f"「{folder.get('title')}」画像生成遇到持续限流：{exc}；已暂停后续收藏夹",
                             phase="folder_profile", kind="folder_profile_progress")
                    except llm_analyzer.ProfileOutputTruncatedError as exc:
                        state["failed"] += 1
                        state["error"] = str(exc)
                        state["stop"] = True
                        emit("err", f"「{folder.get('title')}」模型输出持续截断：{exc}；已暂停后续收藏夹",
                             phase="folder_profile", kind="folder_profile_progress")
                    except Exception as exc:
                        state["failed"] += 1
                        state["error"] = str(exc)
                        emit("err", f"「{folder.get('title')}」画像生成失败：{exc}",
                             phase="folder_profile", kind="folder_profile_progress")
                state["done"] = index
                emit("progress", "", phase="folder_profile", kind="folder_profile_progress",
                     done=state["done"], total=state["total"], generated=state["generated"],
                     skipped=state["skipped"], failed=state["failed"], current=state["current"])
        except Exception as exc:
            state["error"] = str(exc)
            emit("err", f"收藏夹画像任务失败：{exc}", phase="folder_profile")
        finally:
            state["running"] = False
            state["current"] = ""
            emit("ok" if not state.get("error") else "warn",
                 f"画像任务结束：生成 {state['generated']}，跳过 {state['skipped']}，失败 {state['failed']}",
                 phase="folder_profile", kind="folder_profile_end", done=state["done"],
                 total=state["total"], generated=state["generated"],
                 skipped=state["skipped"], failed=state["failed"])

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "started": True, "total": len(requested)}


@app.get("/api/folder-profiles/status")
def folder_profiles_status():
    return APP.get("folder_profile_run") or {"running": False, "done": 0, "total": 0}


@app.post("/api/folder-profiles/stop")
def folder_profiles_stop():
    state = APP.get("folder_profile_run")
    if state and state.get("running"):
        state["stop"] = True
        return {"ok": True}
    return {"ok": False, "error": "没有运行中的画像任务"}


# ============ 阶段2: LLM 分析 ============
class AnalyzeIn(BaseModel):
    continuous: bool = False
    folder_ids: Optional[list[str]] = None
    # incremental：只分析尚无结果的（有结果就跳过，不重复劳动）
    # stale_too：额外重算画像已过期的结论
    # force_all：忽略结果标记，全部重发 LLM
    rebuild: str = "incremental"   # incremental / missing_only / stale_too / force_all


class AnalyzeContinuousIn(BaseModel):
    enabled: bool


# ---------- 分析结果状态：细粒度失效 ----------

_ANALYSIS_STATUS_STAMP = {"key": ""}


def _analysis_candidate_key(known_ids: set) -> str:
    """候选收藏夹集合指纹（只含 ID，不含画像版本）。

    集合变了（新建/归档/删除夹）意味着「可选去向」变了，所有结论都要重判；
    某张画像内容变了则只影响**参考过它**的条目，由 dependent_ids 精确判定。
    """
    return hashlib.sha256("|".join(sorted(str(x) for x in known_ids)).encode("utf-8")).hexdigest()[:16]


def _analysis_dependency_ids(video: dict, result: dict, known_ids: set, name_to_id: dict) -> list:
    """这条结论参考了哪些收藏夹的画像：内容当时所在的夹 + 被推荐去的夹。"""
    deps = {str(x) for x in (video.get("folder_ids") or [])}
    if video.get("source_folder_id"):
        deps.add(str(video["source_folder_id"]))
    deps.intersection_update(str(x) for x in known_ids)
    if str(result.get("action", "")) == "move_to_existing":
        target = name_to_id.get(str(result.get("recommended", "")).strip())
        if target:
            deps.add(str(target))
    return sorted(deps)


def _analysis_scope_maps(known_ids: set) -> dict:
    folders = store.load_folders()
    return {
        "known_ids": set(str(x) for x in known_ids),
        "name_to_id": {str(f.get("title", "")): str(f["media_id"]) for f in folders},
    }


def refresh_analysis_statuses(force: bool = False) -> dict:
    """重算并落库每条分析结果的 status（current / stale）。

    判据：该条冻结的 dependent_ids 中，任一收藏夹的画像版本与当时记录的不一致
    → stale。没有画像依赖的条目（例如只在收件箱里、建议新建）不会因为改画像而过期。
    候选集合变化则整体置为 stale。结果按指纹缓存，重复调用很便宜。
    """
    readiness = _organization_profile_readiness()
    revisions = {str(k): str(v) for k, v in readiness["profile_versions"].items()}
    known_ids = set(revisions) | {str(x) for x in (readiness.get("inbox_folder_ids") or [])}
    candidate_key = _analysis_candidate_key(known_ids)
    revision_key = hashlib.sha256(
        "|".join(f"{k}:{v}" for k, v in sorted(revisions.items())).encode("utf-8")).hexdigest()[:16]
    stamp = f"{candidate_key}|{revision_key}"
    scope = _analysis_scope_maps(known_ids)
    counts = store.analysis_status_counts()
    result = {"current": int(counts.get("current", 0)), "stale": int(counts.get("stale", 0)),
              "candidate_key": candidate_key, "profile_ready": bool(readiness["ready"]),
              "scope": scope}
    if not force and _ANALYSIS_STATUS_STAMP["key"] == stamp:
        result["cached"] = True
        return result
    if store.load_analysis_scope_key() != candidate_key:
        # 可选去向变了：一律作废，下面的逐条判定会把无关的重新算回 current
        store.mark_all_analyses_stale()
        store.save_analysis_scope_key(candidate_key)
    videos = {str(v.get("bvid")): v for v in store.load_videos() if v.get("bvid")}
    updates = {}
    for key, row in store.load_analysis_rows().items():
        record = row["record"]
        deps = list(row["dependent_ids"] or [])
        if not deps:
            # 旧数据没冻结依赖：按当前归属回填一次，此后不再随内容移动而改变
            deps = _analysis_dependency_ids(
                videos.get(str(record.get("bvid") or key), {}), record,
                scope["known_ids"], scope["name_to_id"])
        stored = {str(k): str(v) for k, v in
                  (record.get("organization_profile_versions") or {}).items()}
        status = "stale" if any(revisions.get(d) != stored.get(d) for d in deps) else "current"
        if row["status"] != status or row["dependent_ids"] != deps:
            updates[key] = (status, deps)
    if updates:
        store.set_analysis_statuses(updates)
    _ANALYSIS_STATUS_STAMP["key"] = stamp
    counts = store.analysis_status_counts()
    return {"current": int(counts.get("current", 0)), "stale": int(counts.get("stale", 0)),
            "candidate_key": candidate_key, "profile_ready": bool(readiness["ready"]),
            "scope": scope, "rewritten": len(updates)}


def _pending_analysis_ids(rebuild_mode: str, statuses: dict) -> set:
    """按「重建索引」档位返回已视为完成、不必再发给 LLM 的 key 集合。

    默认（incremental）与「有结果就跳过」一致：库里有分析结论就不再算，
    只有真正新增（无记录）的才会发送。画像过期不会自动触发大规模重算。
    """
    if rebuild_mode == "force_all":
        return set()
    if rebuild_mode == "stale_too":
        # 只跳过仍然有效的结论；过期的会重发
        return {k for k, v in statuses.items() if v == "current"}
    # incremental / missing_only：有记录就算完成，忽略过期
    return set(statuses.keys())


def _make_analyzer(folders: list[str], folder_profiles: list[dict] | None = None) -> llm_analyzer.LLMAnalyzer:
    def report_context_split(item_count, prompt_tokens, requested_output, context_tokens):
        emit("warn", f"上下文保护正在拆分 {item_count} 条内容后重试"
                     f"（估算输入 {prompt_tokens} + 输出上限 {requested_output}，"
                     f"上下文 {context_tokens}）", phase="analyze")
    folder_rows = store.load_folders()
    default_folder = _default_folder(folder_rows)
    inbox = ([{"id": str(default_folder["media_id"]),
               "name": str(default_folder.get("title") or DEFAULT_FAVORITE_NAME)}]
             if default_folder else [])
    return llm_analyzer.LLMAnalyzer(make_llm_config(),
        folders=folders, folder_profiles=folder_profiles,
        default_folders=inbox, on_context_split=report_context_split)


@app.post("/api/analyze/start")
def analyze_start(body: Optional[AnalyzeIn] = None):
    if APP["analyze_run"] and APP["analyze_run"].get("running"):
        return JSONResponse({"ok": False, "error": "分析已在运行中"}, status_code=400)
    readiness = _organization_profile_readiness()
    if not readiness["ready"]:
        names = "、".join(row["name"] for row in readiness["missing"][:8])
        extra = f"；未就绪收藏夹：{names}" if names else ""
        return JSONResponse({"ok": False,
                             "error": "内容归类要求所有有内容的 active 收藏夹都有当前完整画像。"
                                      + readiness["message"] + extra,
                             "readiness": readiness}, status_code=409)
    body = body or AnalyzeIn()
    profile_versions = dict(readiness["profile_versions"])
    selected = {str(x) for x in (body.folder_ids or store.load_scan_selection())}
    profiled_folder_ids = {row["id"] for row in _current_folder_profile_contexts()}
    # 默认收藏夹（未分拣收件箱）没有画像，但它的内容正是要被归类的对象，必须纳入候选。
    inbox_folder_ids = set(readiness.get("inbox_folder_ids") or _default_folder_ids())
    known_folder_ids = profiled_folder_ids | inbox_folder_ids

    def candidates():
        rows = store.load_videos()
        result = []
        for video in rows:
            memberships = {str(x) for x in (video.get("folder_ids") or [])}
            if video.get("source_folder_id"):
                memberships.add(str(video["source_folder_id"]))
            memberships.intersection_update(known_folder_ids)
            if (video.get("bvid") and memberships and
                    (not selected or bool(selected.intersection(memberships)))):
                result.append(video)
        return result
    videos = candidates()
    scan_running_at_start = bool(APP.get("scan_run") and APP["scan_run"].get("running"))
    if not videos and not (body.continuous and scan_running_at_start):
        return JSONResponse({"ok": False, "error": "尚无已扫描数据，请先执行扫描"}, status_code=400)

    folder_contexts = _current_folder_profile_contexts()
    folders_titles = [row["name"] for row in folder_contexts]
    analyzer = _make_analyzer(folders_titles, folder_contexts)
    if not analyzer.config.configured:
        return JSONResponse({"ok": False, "error": "LLM 未配置，请先选择供应商与模型"}, status_code=400)

    cfg = load_config()
    concurrency = max(1, min(4, int(cfg.get("analyze_concurrency", 1) or 1)))
    batch_size = max(1, min(1000, int(cfg.get("analyze_batch", 20) or 20)))
    rebuild_mode = str(body.rebuild or "incremental")
    if rebuild_mode not in ("incremental", "missing_only", "stale_too", "force_all"):
        rebuild_mode = "incremental"
    # 启动时先算一遍待发送量，方便前端区分「候选总量」和「本次要跑几条」
    done_ids0 = _pending_analysis_ids(rebuild_mode, store.analysis_status_map())
    pending0 = sum(1 for v in videos if v.get("bvid")
                   and v["bvid"] not in done_ids0 and not _is_invalid(v))
    state = {"running": True, "done": 0, "total": len(videos),
             "pending": pending0,
             "failed": 0, "stop": False, "error": None,
             "concurrency": concurrency, "batch": batch_size,
             "continuous": bool(body.continuous), "waiting": False,
             "round": 0, "selected_ids": sorted(selected),
             "rebuild": rebuild_mode, "inflight": 0}
    conflict = _begin_job("analyze_run", state, error="分析已在运行中")
    if conflict:
        return conflict

    def run():
        from concurrent.futures import ThreadPoolExecutor
        lock = threading.Lock()
        fail_streak = [0]
        failed_session: set[str] = set()

        def work(batch, ctx):
            """处理一个批次（一次请求多条）。"""
            if state["stop"]:
                return
            with lock:
                state["inflight"] += len(batch)
                inflight = state["inflight"]
                current_done = state["done"]
                current_total = state["total"]
                current_failed = state["failed"]
                current_pending = state.get("pending", 0)
            # 请求已交给模型：立即推送浅色「等待响应」进度。
            emit("progress", "", phase="analyze", kind="analyze_progress",
                 done=current_done, total=current_total, failed=current_failed,
                 inflight=inflight, pending=current_pending)
            err = ""
            try:
                got = analyzer.analyze_batch(batch)
            except Exception as e:
                got = {}
                err = f"{type(e).__name__}: {e}"
            # 落库时冻结「这条结论当初参考了哪几个夹」，之后内容挪动不会追溯性判它过期
            scope = ctx["scope"]
            batch_by_bvid = {str(v.get("bvid")): v for v in batch if v.get("bvid")}
            records, meta = {}, {}
            for bvid, res in (got or {}).items():
                if not res:
                    continue
                stored = dict(res)
                stored["organization_profile_versions"] = profile_versions
                deps = _analysis_dependency_ids(batch_by_bvid.get(str(bvid), {}), stored,
                                                scope["known_ids"], scope["name_to_id"])
                records[str(bvid)] = stored
                meta[str(bvid)] = {"status": "current", "dependent_ids": deps}
            okn = len(records)
            with lock:
                if records:
                    store.save_analysis(records, meta)
                state["failed"] += (len(batch) - okn)
                state["inflight"] = max(0, state["inflight"] - len(batch))
                state["pending"] = max(0, int(state.get("pending", 0)) - len(batch))
                ctx["done"] += len(batch)
                state["done"] = ctx["done"]
                d, f, inflight = ctx["done"], state["failed"], state["inflight"]
                pend = state.get("pending", 0)
                if len(batch) and okn == 0:
                    fail_streak[0] += 1
                else:
                    fail_streak[0] = 0
                streak = fail_streak[0]
                if err:
                    state["error"] = state.get("error") or err
            # 静默进度事件：前端只推进度条，不写日志
            emit("progress", "", phase="analyze", kind="analyze_progress",
                 done=d, total=state["total"], failed=f, inflight=inflight, pending=pend)
            emit("ok" if okn == len(batch) else "warn",
                 f"批完成 +{okn}/{len(batch)} · 累计 {d}/{len(videos)}（失败 {f}）",
                 phase="analyze", done=d, total=state["total"], inflight=inflight, pending=pend)
            if err:
                emit("err", f"本批异常（{len(batch)} 条均失败）：{err}", phase="analyze")
            # 连续多批全失败 → 疑似配置/额度问题，自动停止
            if streak >= 5 and not state["stop"]:
                state["stop"] = True
                emit("err", f"连续 {streak} 批全部失败，已自动停止分析；"
                            f"请检查模型配置、key 或额度", phase="analyze", kind="analyze_end")

        try:
            ctx = {"done": 0, "scope": _analysis_scope_maps(known_folder_ids)}
            emit("info", f"开始分析，批大小 {batch_size} × 并发 {concurrency}"
                 + ("，已启用连续分析" if state["continuous"] else "")
                 + {"stale_too": "，含画像过期项",
                    "missing_only": "，只补缺失（忽略画像过期）",
                    "force_all": "，强制全部重算"}.get(rebuild_mode, "，只分析新增"),
                 phase="analyze", kind="analyze_start", done=0, total=len(videos),
                 pending=pending0, failed=0, inflight=0)
            while not state["stop"]:
                current = candidates()
                # 用持久化的 status 判定「哪些已经完成」，而不是每次比对整个画像字典
                ctx["scope"] = refresh_analysis_statuses()["scope"]
                done_ids = _pending_analysis_ids(rebuild_mode, store.analysis_status_map())
                pending = [v for v in current if v.get("bvid")
                           and v["bvid"] not in done_ids
                           and v["bvid"] not in failed_session
                           and not _is_invalid(v)]
                state["total"] = len(current)
                state["pending"] = len(pending)
                state["done"] = len([v for v in current if _is_invalid(v) or
                                     v.get("bvid") in done_ids])
                ctx["done"] = state["done"]
                if pending:
                    state["waiting"] = False
                    state["round"] += 1
                    batches = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
                    if concurrency <= 1:
                        for b in batches:
                            if state["stop"]: break
                            work(b, ctx)
                    else:
                        with ThreadPoolExecutor(max_workers=concurrency) as ex:
                            list(ex.map(lambda b: work(b, ctx), batches))
                    refresh_analysis_statuses(force=True)
                    after_status = store.analysis_status_map()
                    failed_session.update(v["bvid"] for v in pending
                                          if after_status.get(v["bvid"]) != "current")
                    continue
                scan_running = bool(APP.get("scan_run") and APP["scan_run"].get("running"))
                if not state["continuous"] or not scan_running:
                    break
                state["waiting"] = True
                emit("progress", "", phase="analyze", kind="analyze_progress",
                     done=state["done"], total=state["total"], failed=state["failed"],
                     inflight=state["inflight"], waiting=True)
                ANALYZE_WAKE.clear()
                ANALYZE_WAKE.wait(3)

            if state["stop"]:
                emit("warn", "已停止分析（进度已保存）", phase="analyze",
                     kind="analyze_end", done=ctx["done"], total=state["total"])
            else:
                emit("ok" if not state["failed"] else "warn",
                     f"分析结束：完成 {ctx['done']}，失败 {state['failed']}",
                     phase="analyze", kind="analyze_end",
                     done=ctx["done"], total=state["total"])
        except Exception as e:
            state["error"] = str(e)
            emit("err", f"分析出错：{e}", phase="analyze", kind="analyze_end")
        finally:
            state["running"] = False
            state["stop"] = False
            state["continuous"] = False
            state["waiting"] = False

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "started": True, "total": len(videos), "pending": pending0}


@app.get("/api/analyze/status")
def analyze_status():
    return APP["analyze_run"] or {"running": False, "done": 0, "total": 0,
                                  "pending": 0,
                                  "failed": 0, "stop": False, "error": None}


@app.post("/api/analyze/stop")
def analyze_stop():
    if APP["analyze_run"]:
        APP["analyze_run"]["stop"] = True
        ANALYZE_WAKE.set()
        return {"ok": True}
    return {"ok": False, "error": "没有运行中的分析"}


@app.post("/api/analyze/continuous")
def analyze_continuous(body: AnalyzeContinuousIn):
    state = APP.get("analyze_run")
    if not state or not state.get("running"):
        return {"ok": True, "enabled": False}
    state["continuous"] = bool(body.enabled)
    ANALYZE_WAKE.set()
    return {"ok": True, "enabled": state["continuous"]}


# ============ 阶段3: 预归类方案 ============
@app.get("/api/plan")
def get_plan():
    """返回分析结果 + 现有收藏夹，供前端展示归类方案。"""
    readiness = _organization_profile_readiness()
    profile_versions = readiness["profile_versions"]
    status_info = refresh_analysis_statuses()
    statuses = store.analysis_status_map()
    analysis = store.load_analysis()
    folders = store.load_folders()
    active_folder_ids = {str(folder["media_id"]) for folder in folders}
    inbox_folder_ids = set(readiness.get("inbox_folder_ids") or _default_folder_ids(folders))
    videos = {}
    for video in store.load_videos():
        if not video.get("bvid"):
            continue
        memberships = {str(x) for x in (video.get("folder_ids") or [])}
        if video.get("source_folder_id"):
            memberships.add(str(video["source_folder_id"]))
        if memberships.intersection(active_folder_ids):
            videos[video["bvid"]] = video
    analysis = [row for row in analysis if str(row.get("bvid", "")) in videos]
    stale_analysis_count = 0
    decorated_analysis = []
    for row in analysis:
        key = str(row.get("bvid", ""))
        # 直接读持久化的 status：判定口径与「开始分析」完全一致，不会两处算出不同结果
        current = readiness["ready"] and statuses.get(key) == "current"
        stale_analysis_count += int(not current)
        decorated_analysis.append({**row, "organization_profile_current": current,
                                   "analysis_status": statuses.get(key, "missing")})
    analysis = decorated_analysis
    current_profiles = _current_folder_profile_contexts()
    profile_current_ids = [row["id"] for row in current_profiles]
    profile_current_id_set = set(profile_current_ids)
    saved_profile_ids = set(store.load_folder_profiles())
    current_profile_id_set = set(profile_current_ids)
    profile_stale_ids = [str(folder["media_id"]) for folder in folders
                         if str(folder["media_id"]) in saved_profile_ids and
                         str(folder["media_id"]) not in current_profile_id_set]
    invalid = [v["bvid"] for v in videos.values() if _is_invalid(v)]
    plan_rows = {}
    for bvid, item in store.load_plan_raw().items():
        if bvid not in videos:
            continue
        row = dict(item)
        if row.get("action") in ("move_to_existing", "create_new"):
            # 与底层分析结果共用同一个 status，避免「分析有效但方案过期」这类矛盾显示
            row["profile_context_current"] = (
                readiness["ready"] and statuses.get(str(bvid)) == "current")
        else:
            row["profile_context_current"] = True
        plan_rows[bvid] = row
    return {
        "analysis": analysis,
        # 移入目标只列有当前画像的收藏夹；默认收藏夹是未分拣收件箱，永远不是移入目标。
        "existing_folders": [folder for folder in folders
                             if str(folder["media_id"]) in profile_current_id_set
                             and str(folder["media_id"]) not in inbox_folder_ids],
        "videos_by_bvid": videos,
        "inbox_folder_ids": sorted(inbox_folder_ids),
        "inbox_folder_names": sorted(_default_folder_names(folders)),
        "invalid_count": len(invalid),
        "invalid_bvids": invalid,
        "profile_current_ids": profile_current_ids,
        "profile_current_revisions": {row["id"]: row["revision"] for row in current_profiles},
        "profile_stale_ids": profile_stale_ids,
        "organization_profile_versions": profile_versions,
        "stale_analysis_count": stale_analysis_count,
        "analysis_counts": {
            "current": int(status_info["current"]),
            "stale": int(status_info["stale"]),
            "not_analyzed": max(0, len(videos) - len(analysis)),
            "total_videos": len(videos),
        },
        "plan": plan_rows,
    }


@app.post("/api/plan/mark_invalid")
def plan_mark_invalid():
    """把所有标题恰为「已失效视频」的视频标记为删除（写入 plan.json，等执行）。

    防火墙：只挑选标题**恰好**等于「已失效视频」的条目。
    """
    active_ids = {str(folder["media_id"]) for folder in store.load_folders()}
    videos = store.load_videos()
    inv = [v for v in videos if v.get("bvid") and _is_invalid(v) and
           active_ids.intersection({str(x) for x in (v.get("folder_ids") or [])} |
                                   ({str(v.get("source_folder_id"))} if v.get("source_folder_id") else set()))]
    if not inv:
        return {"ok": True, "count": 0, "message": "没有发现失效视频"}
    plan = {}
    for v in inv:
        plan[v["bvid"]] = {"bvid": v["bvid"], "action": "delete_invalid",
                           "target_folder": "", "create_new_name": ""}
    store.save_plan(plan)
    emit("warn", f"已把 {len(inv)} 条失效视频标记为删除（到阶段❹点「开始执行」生效）")
    return {"ok": True, "count": len(inv)}


class PlanReview(BaseModel):
    apply_list: list  # [{bvid, action, target_folder, create_new_name?}]


@app.post("/api/plan/apply")
def apply_plan(body: PlanReview):
    """把前端确认后的归类方案存入 plan.json，作为执行阶段的输入。

    关键：**保留已存在的 done 状态**，否则已执行过的条目会被重置为待办 → 重复执行。
    """
    destination_error = _default_folder_destination_error(body.apply_list)
    if destination_error:
        return JSONResponse({"ok": False, "error": destination_error}, status_code=409)
    classification_actions = {"move_to_existing", "create_new"}
    profile_versions = {}
    if any(str(item.get("action", "skip")) in classification_actions
           for item in body.apply_list):
        readiness = _organization_profile_readiness()
        if not readiness["ready"]:
            return JSONResponse({"ok": False, "error": readiness["message"],
                                 "readiness": readiness}, status_code=409)
        profile_versions = readiness["profile_versions"]
        refresh_analysis_statuses()
        statuses = store.analysis_status_map()
        if any(str(item.get("action", "skip")) in classification_actions and
               statuses.get(str(item.get("bvid", ""))) != "current"
               for item in body.apply_list):
            return JSONResponse({"ok": False,
                                 "error": "有条目的归类结果已过期或缺少分析记录，请重新分析后再确认"},
                                status_code=409)
        profile_ids = {row["id"] for row in _current_folder_profile_contexts()}
        profile_names = {str(folder.get("title", "")) for folder in store.load_folders()
                         if str(folder["media_id"]) in profile_ids}
        invalid_targets = [str(item.get("target_folder", "")) for item in body.apply_list
                           if str(item.get("action", "skip")) == "move_to_existing" and
                           str(item.get("target_folder", "")) not in profile_names]
        if invalid_targets:
            return JSONResponse({"ok": False,
                                 "error": "内容归类目标必须是有当前完整画像的收藏夹，请刷新预归类方案"},
                                status_code=409)

    old = store.load_plan_raw()
    plan = {}
    kept_done = 0
    for item in body.apply_list:
        bvid = item.get("bvid", "")
        if not bvid:
            continue
        entry = {
            "bvid": bvid,
            "action": item.get("action", "skip"),
            "target_folder": item.get("target_folder", ""),
            "create_new_name": item.get("create_new_name", ""),
            "status": "pending",
        }
        if entry["action"] in classification_actions:
            entry["organization_profile_versions"] = profile_versions
        prev = old.get(bvid) or {}
        if prev.get("status") == "done":
            entry["status"] = "done"
            entry["result"] = prev.get("result", "")
            entry["at"] = prev.get("at", "")
            kept_done += 1
        plan[bvid] = entry
    # 保留 plan 中原本存在、但本次未提交的条目（例如"标记失效视频"）
    for bvid, prev in old.items():
        plan.setdefault(bvid, prev)
    store.save_plan(plan)
    msg = f"已写入方案 {len(plan)} 条"
    if kept_done:
        msg += f"（其中 {kept_done} 条已完成状态已保留，不会重复执行）"
    emit("info", msg)
    return {"ok": True, "planned": len(plan), "kept_done": kept_done}


class PlanStatusIn(BaseModel):
    bvids: list = []          # 要处理的所有 bvid
    status: str = "pending"   # pending / done
    all: bool = False         # True 表示对 plan 中全部条目生效


@app.post("/api/plan/set_status")
def plan_set_status(body: PlanStatusIn):
    """把条目在「待操作 / 已完成」之间移动（例如把已完成的移回待操作）。"""
    st = body.status if body.status in ("pending", "done") else "pending"
    plan = store.load_plan_raw()
    targets = set(plan.keys()) if body.all else set(body.bvids or [])
    n = 0
    for bvid, item in plan.items():
        if bvid in targets:
            item["status"] = st
            if st == "pending":
                item["result"] = ""
            n += 1
    store.save_plan(plan)
    emit("info", f"已把 {n} 条改为「{'待操作' if st == 'pending' else '已完成'}」")
    return {"ok": True, "count": n, "status": st}


class PlanRemoveIn(BaseModel):
    bvids: list = []


@app.post("/api/plan/remove")
def plan_remove(body: PlanRemoveIn):
    """把条目从方案中彻底移除（只删本地方案，不动 B站）。"""
    plan = store.load_plan_raw()
    targets = set(body.bvids or [])
    n = 0
    for b in list(plan.keys()):
        if b in targets:
            plan.pop(b, None)
            n += 1
    store.replace_plan(plan)
    emit("warn", f"已从方案中移除 {n} 条")
    return {"ok": True, "count": n}


# ============ 阶段4: 执行 ============
@app.post("/api/apply/start")
def apply_start():
    """批量执行 plan：失效视频 batch-del，其余视频 move。

    明确业务失败记录后继续；风控停止；超时/无响应标记 unknown 并停止，
    交由用户人工复核。
    """
    if APP["apply_run"] and APP["apply_run"].get("running"):
        return JSONResponse({"ok": False, "error": "执行已在运行中"}, status_code=400)
    plan = store.load_plan_raw()
    if not plan:
        return JSONResponse({"ok": False, "error": "没有可执行的方案，请先在阶段3确认"}, status_code=400)
    # 执行阶段按需自建会话（不依赖是否先扫描过）
    if APP["session"] is None:
        try:
            APP["session"] = bili_api.BiliSession(get_session_cookie())
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"Cookie 不可用：{e}"}, status_code=400)
    session = APP["session"]
    cfg = load_config()
    session.write_interval = float(cfg.get("write_interval", 2) or 2)
    batch_size = max(1, min(1000, int(cfg.get("apply_batch", 1000) or 1000)))
    videos = {v["bvid"]: v for v in store.load_videos() if v.get("bvid")}

    # 迁移：把"上次执行断点"之前的条目补上已完成状态（早期版本没记录结果）
    prev = store.load_apply_state()
    done_idx = int(prev.get("last_ok", 0) or 0)
    migrated = 0
    if done_idx:
        for i, (_b, it) in enumerate(plan.items()):
            if i < done_idx and it.get("status") != "done":
                it["status"] = "done"
                it["result"] = it.get("result") or "（历史执行，按断点视为已完成）"
                it["at"] = it.get("at") or ""
                migrated += 1
        if migrated:
            store.save_plan(plan)
        store.save_apply_state({})   # 迁移只做一次；之后一律以 status 为准

    # unknown 必须人工复核后移回 pending，不自动重发。
    pending = [(b, it) for b, it in plan.items()
               if it.get("status") not in ("done", "unknown")]
    destination_error = _default_folder_destination_error([it for _, it in pending])
    if destination_error:
        return JSONResponse({"ok": False, "error": destination_error}, status_code=409)
    needs_profiles = any(it.get("action") in ("move_to_existing", "create_new")
                         for _, it in pending)
    if needs_profiles:
        readiness = _organization_profile_readiness()
        if not readiness["ready"]:
            return JSONResponse({"ok": False, "error": readiness["message"],
                                 "readiness": readiness}, status_code=409)
        refresh_analysis_statuses()
        statuses = store.analysis_status_map()
        if any(it.get("action") in ("move_to_existing", "create_new") and
               statuses.get(str(b)) != "current" for b, it in pending):
            return JSONResponse({"ok": False,
                                 "error": "待执行方案里有已过期的归类结果，请重新分析并确认方案"},
                                status_code=409)
    total = len(pending)
    already_done = len([1 for it in plan.values() if it.get("status") == "done"])
    held_unknown = len([1 for it in plan.values() if it.get("status") == "unknown"])

    state = {"running": True, "done": 0, "total": total,
             "ok": 0, "failed": 0, "skip": 0, "deleted": 0,
             "unknown": 0, "stop": False, "error": None, "log": [],
             "batch_size": batch_size, "batch_done": 0, "batch_total": 0}
    conflict = _begin_job("apply_run", state, error="执行已在运行中")
    if conflict:
        return conflict

    def _mark(item, status: str, result: str):
        """写入一条方案的执行结果。"""
        item["status"] = status
        item["result"] = result
        item["at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def chunks(items):
        for i in range(0, len(items), batch_size):
            yield items[i:i + batch_size]

    def run():
        def save_progress():
            store.save_plan(plan)
            store.save_videos(videos)

        def finish_items(entries, status, message, *, target_id="", source_id=""):
            for bvid, item, video in entries:
                _mark(item, status, message)
                if status == "done" and (target_id or source_id):
                    old_source = str(source_id or video.get("source_folder_id") or "")
                    memberships = {str(x) for x in (video.get("folder_ids") or [])}
                    if old_source:
                        memberships.discard(old_source)
                    if target_id:
                        memberships.add(str(target_id))
                        video["source_folder_id"] = str(target_id)
                    else:
                        video["source_folder_id"] = sorted(memberships)[0] if memberships else ""
                    video["folder_ids"] = sorted(memberships)
                state["done"] += 1
            n = len(entries)
            if status == "done":
                state["ok"] += n
            elif status == "unknown":
                state["unknown"] += n
            elif status == "failed":
                state["failed"] += n
            save_progress()
            emit("progress", "", phase="apply", kind="apply_progress",
                 done=state["done"], total=total)

        def fail_entries(entries, message):
            finish_items(entries, "failed", message)
            emit("warn", f"{message}（{len(entries)} 条）", phase="apply")

        def run_batch(kind, src, target_id, target_name, entries):
            if state["stop"]:
                return False
            aids = [int(video.get("aid", 0) or 0) for _, _, video in entries]
            state["batch_done"] += 1
            bn = state["batch_done"]
            bt = state["batch_total"]
            label = "删除失效" if kind == "delete" else f"移入「{target_name}」"
            emit("info", f"批次 {bn}/{bt}：{label} {len(entries)} 条",
                 phase="apply", done=state["done"], total=total)
            # try 只包远程调用：落库失败不得把已远端成功的批次标成 failed
            try:
                if kind == "delete":
                    session.batch_delete(src, aids, should_stop=lambda: state["stop"])
                    success_msg = f"批量删除失效视频成功（批次 {bn}）"
                else:
                    session.move_batch(src, target_id, aids, mid=owner_mid,
                                       should_stop=lambda: state["stop"])
                    success_msg = f"批量移入「{target_name}」成功（批次 {bn}）"
            except bili_api.WriteUncertainError as e:
                msg = f"批次 {bn} 结果不确定：{e}"
                finish_items(entries, "unknown", msg)
                state["error"] = msg + "。已停止，请人工复核后再继续。"
                state["stop"] = True
                emit("err", state["error"], phase="apply", kind="apply_end",
                     done=state["done"], total=total)
                return False
            except bili_api.RateLimitedError as e:
                state["error"] = ("已手动停止，本批保留待处理。" if state["stop"] else
                                  f"风控触发：{e}。已停止，本批保留待处理。")
                state["stop"] = True
                emit("err", state["error"], phase="apply", kind="apply_end",
                     done=state["done"], total=total)
                return False
            except bili_api.BiliApiError as e:
                # 明确业务失败：记账后继续后续批次。
                fail_entries(entries, f"批次 {bn} 失败：{e}")
                return True
            except (requests.RequestException, OSError) as e:
                # 网络/IO：记账后继续，避免一次抖动卡死整次执行。
                fail_entries(entries, f"批次 {bn} 网络/IO 异常：{e}")
                return True
            # 远端已成功 → 记账；落库失败保持 done 并停，绝不改写成 failed
            try:
                if kind == "delete":
                    finish_items(entries, "done", success_msg, source_id=src)
                    state["deleted"] += len(entries)
                else:
                    finish_items(entries, "done", success_msg,
                                 target_id=target_id, source_id=src)
                emit("ok", f"批次 {bn}/{bt} 完成：{len(entries)} 条",
                     phase="apply", done=state["done"], total=total)
                return True
            except (requests.RequestException, OSError) as e:
                state["error"] = f"批次 {bn} 远端已成功但本地落库失败：{e}"
                state["stop"] = True
                emit("err", state["error"], phase="apply", kind="apply_end",
                     done=state["done"], total=total)
                return False

        try:
            if migrated:
                emit("info", f"已把历史断点前 {migrated} 条补标为已完成")
            emit("info", f"开始批量执行：待操作 {total} 条，单批上限 {batch_size}"
                         f"（已完成 {already_done}，待人工复核 {held_unknown}）",
                 phase="apply", kind="apply_start", done=0, total=total)

            # 每次执行都实时刷新收藏夹映射，不使用过期 folders.json。
            owner_mid = session.get_mid()
            live_folders = session.list_folders()
            store.save_folders(live_folders)
            if needs_profiles:
                readiness_now = _organization_profile_readiness()
                if not readiness_now["ready"]:
                    state["error"] = ("画像状态在执行前发生变化，未发送移动请求：" +
                                       readiness_now["message"])
                    state["stop"] = True
                    emit("err", state["error"], phase="apply", kind="apply_end",
                         done=state["done"], total=total)
                    return
                refresh_analysis_statuses()
                current_statuses = store.analysis_status_map()
                if any(it.get("action") in ("move_to_existing", "create_new") and
                       current_statuses.get(str(b)) != "current" for b, it in pending):
                    state["error"] = "画像版本在执行前发生变化，未发送移动请求；请重新分析并确认方案"
                    state["stop"] = True
                    emit("err", state["error"], phase="apply", kind="apply_end",
                         done=state["done"], total=total)
                    return
                profile_ids = {row["id"] for row in _current_folder_profile_contexts()}
                profile_names = {str(folder.get("title", "")) for folder in live_folders
                                 if str(folder["media_id"]) in profile_ids}
                destination_error = _default_folder_destination_error([it for _, it in pending])
                if destination_error:
                    state["error"] = destination_error + "；未发送移动请求"
                    state["stop"] = True
                    emit("err", state["error"], phase="apply", kind="apply_end",
                         done=state["done"], total=total)
                    return
                if any(it.get("action") == "move_to_existing" and
                       str(it.get("target_folder", "")) not in profile_names
                       for _, it in pending):
                    state["error"] = "执行目标不再是有当前完整画像的收藏夹，未发送移动请求"
                    state["stop"] = True
                    emit("err", state["error"], phase="apply", kind="apply_end",
                         done=state["done"], total=total)
                    return
            name_to_id = {f["title"]: str(f["media_id"]) for f in live_folders}
            forbidden_names = _default_folder_names(live_folders)
            active_ids = {str(f["media_id"]) for f in live_folders}
            latest_videos = {}
            for video in store.load_videos():
                memberships = {str(x) for x in (video.get("folder_ids") or [])}
                if video.get("source_folder_id"):
                    memberships.add(str(video["source_folder_id"]))
                if video.get("bvid") and memberships.intersection(active_ids):
                    latest_videos[video["bvid"]] = video
            videos.clear()
            videos.update(latest_videos)
            executable = []
            archived_only = 0
            for bvid, item in pending:
                if item.get("action") not in ("skip", "") and bvid not in videos:
                    _mark(item, "failed", "该内容已没有 active 收藏夹关系，未执行；请刷新并重新分析")
                    state["done"] += 1
                    state["failed"] += 1
                    archived_only += 1
                else:
                    executable.append((bvid, item))
            pending[:] = executable
            if archived_only:
                store.save_plan(plan)
                emit("warn", f"跳过 {archived_only} 条已归档或无 active 收藏关系的内容，未发送写请求",
                     phase="apply", done=state["done"], total=total)

            delete_groups = {}
            move_groups = {}
            new_groups = {}

            # 先处理无需发请的条目，再分组。
            for bvid, item in pending:
                video = videos.get(bvid, {})
                aid = int(video.get("aid", 0) or 0)
                action = item.get("action", "skip")
                src = str(item.get("source_folder_id") or video.get("source_folder_id") or "")
                entry = (bvid, item, video)
                if action in ("skip", ""):
                    _mark(item, "done", f"{bvid} 已按方案跳过")
                    state["done"] += 1
                    state["skip"] += 1
                elif not aid:
                    fail_entries([entry], f"{bvid} 无有效 aid")
                elif not src:
                    fail_entries([entry], f"{bvid} 来源收藏夹未知")
                elif src not in {str(x) for x in (video.get("folder_ids") or [])}:
                    fail_entries([entry], f"{bvid} 已不在来源收藏夹中，拒绝使用过期关系执行")
                elif action == "delete_invalid":
                    if not _is_invalid(video):
                        fail_entries([entry], f"{bvid} 非失效视频，拒绝删除")
                    else:
                        delete_groups.setdefault(src, []).append(entry)
                elif action == "create_new":
                    name = (item.get("create_new_name") or item.get("target_folder") or "").strip()
                    if name in forbidden_names:
                        fail_entries([entry], "默认收藏夹只能移出，不能作为新建或移入目标")
                    elif not name:
                        fail_entries([entry], f"{bvid} 新收藏夹名为空")
                    else:
                        new_groups.setdefault(name, []).append((src, entry))
                else:
                    target_name = (item.get("target_folder") or "").strip()
                    target_id = name_to_id.get(target_name, "")
                    if target_name in forbidden_names:
                        fail_entries([entry], "默认收藏夹只能移出，不能作为内容整理的移入目标")
                    elif not target_id:
                        fail_entries([entry], f"{bvid} 目标收藏夹「{target_name}」不存在")
                    elif src == target_id:
                        _mark(item, "done", f"{bvid} 已在「{target_name}」，无需移动")
                        state["done"] += 1
                        state["skip"] += 1
                    else:
                        move_groups.setdefault((src, target_id, target_name), []).append(entry)

            save_progress()

            # 实际请求数：删除 -> 移动到现有夹 -> 新建夹并移动。
            def batch_count(groups):
                return sum((len(items) + batch_size - 1) // batch_size
                           for items in groups.values())
            new_batch_total = 0
            for grouped in new_groups.values():
                by_src_count = {}
                for src, entry in grouped:
                    by_src_count[src] = by_src_count.get(src, 0) + 1
                new_batch_total += sum((n + batch_size - 1) // batch_size
                                       for n in by_src_count.values())
            state["batch_total"] = (batch_count(delete_groups) + batch_count(move_groups)
                                    + new_batch_total)

            for src, entries in delete_groups.items():
                for batch in chunks(entries):
                    if not run_batch("delete", src, "", "", batch):
                        return

            for (src, target_id, target_name), entries in move_groups.items():
                for batch in chunks(entries):
                    if not run_batch("move", src, target_id, target_name, batch):
                        return

            for name, grouped in new_groups.items():
                if state["stop"]:
                    return
                target_id = name_to_id.get(name, "")
                entries = [entry for _, entry in grouped]
                if not target_id:
                    try:
                        target_id = session.create_folder(name)
                        if not target_id:
                            raise bili_api.BiliApiError("未返回收藏夹 id")
                        name_to_id[name] = target_id
                        emit("ok", f"已新建收藏夹「{name}」", phase="apply")
                    except bili_api.RateLimitedError as e:
                        state["error"] = f"新建「{name}」时触发风控：{e}。已停止。"
                        state["stop"] = True
                        emit("err", state["error"], phase="apply", kind="apply_end",
                             done=state["done"], total=total)
                        return
                    except (bili_api.BiliApiError, requests.RequestException, OSError) as e:
                        fail_entries(entries, f"新建收藏夹「{name}」失败：{e}")
                        continue
                # 同一新夹的条目仍需按来源夹分组。
                by_src = {}
                for src, entry in grouped:
                    if src == str(target_id):
                        finish_items([entry], "done", f"已在「{name}」，无需移动",
                                     target_id=target_id, source_id=src)
                        state["skip"] += 1
                    else:
                        by_src.setdefault(src, []).append(entry)
                for src, src_entries in by_src.items():
                    for batch in chunks(src_entries):
                        if not run_batch("move", src, target_id, name, batch):
                            return

            level = "ok" if not state["failed"] else "warn"
            emit(level, f"批量执行结束：成功 {state['ok']}"
                        f"（其中删除失效 {state['deleted']}），"
                        f"失败 {state['failed']}，跳过 {state['skip']}",
                 phase="apply", kind="apply_end", done=state["done"], total=total)
        except bili_api.RateLimitedError as e:
            state["error"] = f"风控触发：{e}。已停止。"
            emit("err", state["error"], phase="apply", kind="apply_end",
                 done=state["done"], total=total)
        except Exception as e:
            state["error"] = f"执行失败：{e}"
            emit("err", state["error"], phase="apply", kind="apply_end",
                 done=state["done"], total=total)
        finally:
            try:
                save_progress()
            except (requests.RequestException, OSError) as e:
                emit("err", f"收尾落库失败：{e}", phase="apply")
            state["running"] = False
            # 内容整理结束后自动刷新一次收藏夹目录，避免 remote_count 与本地关系长期不一致
            if state.get("ok") or state.get("deleted"):
                refreshed = _refresh_folder_directory()
                if refreshed is not None:
                    emit("ok", f"已自动刷新收藏夹目录：{len(refreshed)} 个",
                         phase="apply", kind="folders_refreshed")

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "started": True, "total": total, "batch_size": batch_size,
            "held_unknown": held_unknown}


@app.get("/api/apply/status")
def apply_status():
    return APP["apply_run"] or {"running": False, "done": 0, "total": 0,
                                "ok": 0, "failed": 0, "skip": 0, "unknown": 0,
                                "error": None,
                                "log": []}


@app.post("/api/apply/stop")
def apply_stop():
    if APP["apply_run"]:
        APP["apply_run"]["stop"] = True
        return {"ok": True}
    return {"ok": False, "error": "没有运行中的执行"}


# ============ 应用更新 ============
def _latest_release_version() -> str:
    # /releases/latest 会重定向到最新版本的 tag 页面，不消耗 GitHub REST API 配额。
    with requests.get(
        GITHUB_LATEST_RELEASE_URL,
        headers={"User-Agent": "BiliFavOrganizer-update-check"},
        timeout=(10, 25),
        allow_redirects=True,
        stream=True,
    ) as response:
        response.raise_for_status()
        parsed = urlsplit(response.url)

    tag_prefix = f"/{GITHUB_REPOSITORY}/releases/tag/"
    if parsed.scheme != "https" or parsed.hostname != "github.com" or \
            not parsed.path.startswith(tag_prefix):
        raise ValueError("无法从 GitHub 最新 Release 页面读取版本标签")
    tag = parsed.path[len(tag_prefix):].split("/", 1)[0]
    if _version_key(tag) is None:
        raise ValueError(f"最新 Release 版本标签格式不支持：{tag or '空'}")
    return tag


def _fetch_latest_release() -> dict:
    tag = _latest_release_version()
    zip_name = f"BiliFavOrganizer-Windows-x64-{tag}.zip"
    checksum_name = f"{zip_name}.sha256"
    setup_name = f"BiliFavOrganizer-Setup-{tag}.exe"
    setup_checksum_name = f"{setup_name}.sha256"
    download_base = f"https://github.com/{GITHUB_REPOSITORY}/releases/download/{tag}"
    return {
        "version": tag,
        "release_url": f"https://github.com/{GITHUB_REPOSITORY}/releases/tag/{tag}",
        "zip_name": zip_name,
        "zip_url": f"{download_base}/{zip_name}",
        "checksum_url": f"{download_base}/{checksum_name}",
        "setup_name": setup_name,
        "setup_url": f"{download_base}/{setup_name}",
        "setup_checksum_url": f"{download_base}/{setup_checksum_name}",
        "zip_size": 0,
    }


def _set_update_state(**values):
    with _UPDATE_LOCK:
        UPDATE_STATE.update(values)


def _extract_release_zip(zip_path: Path, stage_dir: Path):
    stage_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        members = archive.infolist()
        if not members or len(members) > 10000:
            raise ValueError("Release ZIP 文件数量异常")
        if sum(member.file_size for member in members) > 3 * 1024 * 1024 * 1024:
            raise ValueError("Release ZIP 解压体积超过安全上限")

        stage_root = stage_dir.resolve()
        names = set()
        for member in members:
            name = member.filename
            relative = PurePosixPath(name)
            if (not name or "\\" in name or relative.is_absolute() or
                    ".." in relative.parts or
                    (relative.parts and ":" in relative.parts[0])):
                raise ValueError("Release ZIP 含有不安全的文件路径")
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Release ZIP 不允许包含符号链接")
            target = (stage_dir / Path(*relative.parts)).resolve()
            if not target.is_relative_to(stage_root):
                raise ValueError("Release ZIP 文件路径越界")
            names.add(name.rstrip("/"))

        if "BiliFavOrganizer.exe" not in names:
            raise ValueError("Release ZIP 缺少 BiliFavOrganizer.exe")
        archive.extractall(stage_dir)


_UPDATE_HELPER_SCRIPT = r'''param(
    [int]$ParentPid,
    [string]$WorkDir,
    [string]$StageDir,
    [string]$InstallDir,
    [int]$Port,
    [string]$Version
)
$ErrorActionPreference = "Stop"
$StatusFile = Join-Path $WorkDir "status.json"
$BackupDir = $null
$NewProcess = $null
$BackupMoved = $false

function Write-UpdateState([string]$Status, [string]$Message = "") {
    try {
        @{ status = $Status; error = $Message; version = $Version } |
            ConvertTo-Json -Compress |
            Set-Content -LiteralPath $StatusFile -Encoding UTF8
    } catch { }
}

try {
    Wait-Process -Id $ParentPid -Timeout 20 -ErrorAction SilentlyContinue
    if (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) {
        # 应用会先自己退出（并主动断开 SSE 长连接）；仍未退出就强制结束，
        # 否则替换会一直等一个不会退出的旧进程，表现为"更新卡住"。
        Stop-Process -Id $ParentPid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
    if (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) {
        throw "无法结束正在运行的应用进程，更新已取消。"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $StageDir "BiliFavOrganizer.exe") -PathType Leaf)) {
        throw "更新包中找不到 BiliFavOrganizer.exe。"
    }

    $ParentDir = Split-Path -Parent $InstallDir
    $BackupDir = Join-Path $ParentDir ((Split-Path -Leaf $InstallDir) + ".previous-" + [guid]::NewGuid().ToString("N"))
    Move-Item -LiteralPath $InstallDir -Destination $BackupDir
    $BackupMoved = $true
    Move-Item -LiteralPath $StageDir -Destination $InstallDir

    foreach ($OldItem in (Get-ChildItem -LiteralPath $BackupDir -Force)) {
        $NewItemPath = Join-Path $InstallDir $OldItem.Name
        if (-not (Test-Path -LiteralPath $NewItemPath)) {
            Copy-Item -LiteralPath $OldItem.FullName -Destination $InstallDir -Recurse -Force
        }
    }

    $NewExe = Join-Path $InstallDir "BiliFavOrganizer.exe"
    if (-not (Test-Path -LiteralPath $NewExe -PathType Leaf)) {
        throw "更新后找不到程序文件。"
    }
    $NewProcess = Start-Process -FilePath $NewExe `
        -ArgumentList @("--port", [string]$Port, "--no-browser") `
        -WorkingDirectory $InstallDir -PassThru
    $Ready = $false
    for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
        $NewProcess.Refresh()
        if ($NewProcess.HasExited) { break }
        try {
            $Health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/version" -TimeoutSec 2
            if ($Health.version -eq $Version) { $Ready = $true; break }
        } catch { }
        Start-Sleep -Seconds 1
    }
    if (-not $Ready) {
        throw "新版本未能启动，正在恢复旧版本。"
    }

    Write-UpdateState "complete"
    try { Remove-Item -LiteralPath $BackupDir -Recurse -Force } catch { }
    # 工作目录里是 release.zip 与解压出的 stage：必须整套删（旧代码只删了文件又漏了 -Recurse，
    # 任何一步失败就会留下上百 MB）。先删本脚本自身，再删整个目录。
    try { Remove-Item -LiteralPath $PSCommandPath -Force } catch { }
    try { Remove-Item -LiteralPath $WorkDir -Recurse -Force } catch { }
} catch {
    $Message = $_.Exception.Message
    Write-UpdateState "error" $Message
    if ($NewProcess -and -not $NewProcess.HasExited) {
        try { Stop-Process -Id $NewProcess.Id -Force } catch { }
    }
    if ($BackupMoved -and (Test-Path -LiteralPath $BackupDir)) {
        if (Test-Path -LiteralPath $InstallDir) {
            $FailedDir = $InstallDir + ".failed-" + [guid]::NewGuid().ToString("N")
            try { Move-Item -LiteralPath $InstallDir -Destination $FailedDir } catch { }
        }
        if (-not (Test-Path -LiteralPath $InstallDir)) {
            try { Move-Item -LiteralPath $BackupDir -Destination $InstallDir } catch { }
        }
    }
    $OldExe = Join-Path $InstallDir "BiliFavOrganizer.exe"
    if ((Test-Path -LiteralPath $OldExe -PathType Leaf) -and
            -not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) {
        try {
            Start-Process -FilePath $OldExe `
                -ArgumentList @("--port", [string]$Port, "--no-browser") `
                -WorkingDirectory $InstallDir
        } catch { }
    }
    # 失败原因写到固定位置（工作目录马上要整套删掉，重启后的应用靠它显示原因）
    try {
        $ErrorDir = Join-Path $env:LOCALAPPDATA "BiliFavOrganizer"
        if (-not (Test-Path -LiteralPath $ErrorDir)) {
            New-Item -ItemType Directory -Force -Path $ErrorDir | Out-Null
        }
        @{ status = "error"; error = $Message } | ConvertTo-Json -Compress |
            Set-Content -LiteralPath (Join-Path $ErrorDir "update-error.json") -Encoding UTF8
    } catch { }
    # 收尾：不管回滚成不成，临时目录都要删干净
    try { Remove-Item -LiteralPath $PSCommandPath -Force } catch { }
    try { Remove-Item -LiteralPath $WorkDir -Recurse -Force } catch { }
}
'''


def _sha256_file(path: Path) -> str:
    """分块算文件摘要，避免把几十 MB 一次读进内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_expected_sha256(checksum_url: str, label: str) -> str:
    """下载前先拿到 Release 里 .sha256 文件的期望摘要（续传后要靠它校验整体）。"""
    response = requests.get(checksum_url, timeout=(10, 30))
    response.raise_for_status()
    match = re.search(r"\b([0-9a-fA-F]{64})\b", response.text)
    if not match:
        raise ValueError(f"{label} SHA256 文件格式无效")
    return match.group(1).lower()


def _download_release_asset(url: str, dest: Path, expected_sha256: str, label: str,
                            *, attempts: int = 5, retry_delay_seconds: float = 2.0) -> int:
    """把 Release 资源下载到 dest，连接被掐断时用 Range 续下。

    每轮结束后校验整体 SHA256：通过才算成功；服务器忽略 Range（返回 200）时从头重下；
    连续 attempts 轮都没成功就抛错。取消请求会立刻中断。
    """
    max_bytes = 2 * 1024 * 1024 * 1024
    last_error: object = None
    for attempt in range(1, attempts + 1):
        if _UPDATE_CANCEL.is_set():
            raise _UpdateCancelled()
        have = dest.stat().st_size if dest.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            response = requests.get(url, stream=True, timeout=(15, 60), headers=headers)
            response.raise_for_status()
            try:
                if have and response.status_code == 200:
                    # 服务器不支持断点续传：只能把已下的丢掉重来
                    log.info("%s 不支持断点续传，从头下载", label)
                    have = 0
                    dest.unlink(missing_ok=True)
                content_length = int(response.headers.get("Content-Length") or 0)
                total = have + content_length if content_length else 0
                if total > max_bytes:
                    raise ValueError(f"{label}超过 2 GiB 安全上限")
                _set_update_state(total_bytes=total, downloaded_bytes=have)
                written = have
                with dest.open("ab") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if _UPDATE_CANCEL.is_set():
                            raise _UpdateCancelled()
                        if not chunk:
                            continue
                        output.write(chunk)
                        written += len(chunk)
                        _set_update_state(downloaded_bytes=written)
                if written <= have:
                    raise requests.exceptions.ChunkedEncodingError("本次没有收到新数据")
            finally:
                response.close()
        except _UpdateCancelled:
            raise
        except ValueError:
            raise                      # 尺寸/格式类硬错误不重试
        except requests.exceptions.RequestException as exc:
            last_error = exc
            log.warning("%s 下载中断（第 %d/%d 次）：%s", label, attempt, attempts, exc)

        if dest.exists() and _sha256_file(dest) == expected_sha256:
            return dest.stat().st_size
        if dest.exists() and dest.stat().st_size:
            if attempt >= attempts:
                raise ValueError(f"{label} SHA256 校验失败，已取消更新")
            log.warning("%s 尚未下载完整（第 %d/%d 次），继续续传", label, attempt, attempts)
        elif attempt >= attempts:
            break
        if attempt < attempts:
            time.sleep(retry_delay_seconds * attempt)
    raise RuntimeError(f"{label}下载失败：{last_error or '多次尝试均未完成'}")


def _run_update_worker(release: dict):
    global _UPDATE_STATUS_FILE
    work_dir = None
    try:
        if not getattr(sys, "frozen", False) or os.name != "nt":
            raise RuntimeError("自动覆盖更新仅支持 Windows 便携版")
        _UPDATE_CANCEL.clear()
        install_dir = APP_DIR.resolve()
        if install_dir == install_dir.parent:
            raise RuntimeError("程序不能从磁盘根目录自动更新，请先解压到单独的可写文件夹")
        if not (APP_DIR / "BiliFavOrganizer.exe").is_file():
            raise RuntimeError("当前程序目录中找不到 BiliFavOrganizer.exe")

        work_dir = Path(tempfile.mkdtemp(prefix=".bfo-update-", dir=str(APP_DIR.parent)))
        zip_path = work_dir / "release.zip"
        stage_dir = work_dir / "stage"
        status_file = work_dir / "status.json"
        with _UPDATE_LOCK:
            _UPDATE_STATUS_FILE = status_file
        status_file.write_text(json.dumps({"status": "downloading", "error": ""}),
                               encoding="utf-8")

        expected_sha256 = _fetch_expected_sha256(release["checksum_url"], "更新包")
        downloaded = _download_release_asset(release["zip_url"], zip_path,
                                             expected_sha256, "更新包")
        if _UPDATE_CANCEL.is_set():
            raise _UpdateCancelled()

        if _UPDATE_CANCEL.is_set():
            raise _UpdateCancelled()
        _extract_release_zip(zip_path, stage_dir)
        if not (stage_dir / "_internal").is_dir():
            raise ValueError("Release ZIP 缺少应用运行环境目录")

        helper_path = work_dir / "apply-update.ps1"
        helper_path.write_text("\ufeff" + _UPDATE_HELPER_SCRIPT, encoding="utf-8")
        _set_update_state(status="installing", downloaded_bytes=downloaded,
                          total_bytes=downloaded, error="")
        with _UPDATE_LOCK:
            _UPDATE_STATUS_FILE = status_file

        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            raise RuntimeError("找不到 Windows PowerShell，无法应用更新")
        args = [
            powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(helper_path),
            "-ParentPid", str(os.getpid()),
            "-WorkDir", str(work_dir),
            "-StageDir", str(stage_dir),
            "-InstallDir", str(APP_DIR),
            "-Port", str(_APP_PORT),
            "-Version", str(release["version"]),
        ]
        subprocess.Popen(
            args,
            cwd=str(work_dir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            close_fds=True,
        )
        # 先断开 SSE 长连接，再请求退出；15 秒后仍未退出则硬退，
        # 保证更新助手不会因为"应用仍在运行"而卡住或超时取消。
        _SHUTDOWN.set()
        if _UVICORN_SERVER is not None:
            threading.Timer(2.0, lambda: setattr(_UVICORN_SERVER, "should_exit", True)).start()
            threading.Timer(15.0, _force_exit_if_alive).start()
    except _UpdateCancelled:
        log.info("更新下载已取消")
        _set_update_state(status="cancelled", error="", downloaded_bytes=0, total_bytes=0)
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
    except Exception as exc:
        _set_update_state(status="error", error=str(exc))
        _write_update_error(str(exc))
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


_INSTALLER_UPDATE_HELPER_SCRIPT = r'''param(
    [int]$ParentPid,
    [string]$SetupPath,
    [string]$InstallDir,
    [int]$Port,
    [string]$Version,
    [string]$Scope
)
$ErrorActionPreference = "Stop"
try {
    Wait-Process -Id $ParentPid -Timeout 20 -ErrorAction SilentlyContinue
    if (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) {
        Stop-Process -Id $ParentPid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
    $arguments = @("/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS")
    if ($Scope -eq "all-users") {
        $arguments += "/ALLUSERS"
    } else {
        $arguments += "/CURRENTUSER"
    }
    $startArgs = @{ FilePath = $SetupPath; ArgumentList = $arguments; Wait = $true; PassThru = $true }
    if ($Scope -eq "all-users") {
        # 全机安装需要管理员权限：这里会弹一次 UAC，用户拒绝则安装包返回非零。
        $startArgs["Verb"] = "RunAs"
    }
    $setup = Start-Process @startArgs
    if ($setup.ExitCode -ne 0) {
        throw "安装包返回退出码 $($setup.ExitCode)，更新未完成。"
    }
    $exe = Join-Path $InstallDir "BiliFavOrganizer.exe"
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        throw "更新后找不到程序文件：$exe"
    }
    Start-Process -FilePath $exe -ArgumentList @("--port", [string]$Port, "--no-browser") `
        -WorkingDirectory $InstallDir
    # 安装包已经跑完：把下载下来的安装包与脚本整套删掉（旧代码从不清理，每次留 25 MB）
    try { Remove-Item -LiteralPath $PSCommandPath -Force } catch { }
    try { Remove-Item -LiteralPath $WorkDir -Recurse -Force } catch { }
} catch {
    # 安装失败时把旧程序拉起来，别让用户什么都打不开
    $exe = Join-Path $InstallDir "BiliFavOrganizer.exe"
    if ((Test-Path -LiteralPath $exe -PathType Leaf) -and
            -not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) {
        try {
            Start-Process -FilePath $exe -ArgumentList @("--port", [string]$Port, "--no-browser") `
                -WorkingDirectory $InstallDir
        } catch { }
    }
    # 失败原因写到固定位置（临时目录马上要删掉）
    try {
        $ErrorDir = Join-Path $env:LOCALAPPDATA "BiliFavOrganizer"
        if (-not (Test-Path -LiteralPath $ErrorDir)) {
            New-Item -ItemType Directory -Force -Path $ErrorDir | Out-Null
        }
        @{ status = "error"; error = $_.Exception.Message } | ConvertTo-Json -Compress |
            Set-Content -LiteralPath (Join-Path $ErrorDir "update-error.json") -Encoding UTF8
    } catch { }
    try { Remove-Item -LiteralPath $PSCommandPath -Force } catch { }
    try { Remove-Item -LiteralPath $WorkDir -Recurse -Force } catch { }
}
'''


def _run_installer_update_worker(release: dict, info: dict):
    """安装版更新：下载新安装包 → 校验 SHA256 → 静默运行，由安装程序完成替换与重启。"""
    global _UPDATE_STATUS_FILE
    work_dir = None
    try:
        if not getattr(sys, "frozen", False) or os.name != "nt":
            raise RuntimeError("自动覆盖更新仅支持 Windows 便携版")
        install_dir = str((info or {}).get("install_dir") or "").strip()
        if not install_dir or not Path(install_dir).is_dir():
            raise RuntimeError("注册表记录的安装目录不存在，请重新运行安装包修复安装")
        scope = str((info or {}).get("scope") or "current-user")

        _UPDATE_CANCEL.clear()
        work_dir = Path(tempfile.mkdtemp(prefix=".bfo-setup-"))
        setup_path = work_dir / release["setup_name"]
        status_file = work_dir / "status.json"
        with _UPDATE_LOCK:
            _UPDATE_STATUS_FILE = status_file
        status_file.write_text(json.dumps({"status": "downloading", "error": ""}),
                               encoding="utf-8")

        expected_sha256 = _fetch_expected_sha256(release["setup_checksum_url"], "安装包")
        downloaded = _download_release_asset(release["setup_url"], setup_path,
                                             expected_sha256, "安装包")
        if _UPDATE_CANCEL.is_set():
            raise _UpdateCancelled()

        helper_path = work_dir / "apply-installer-update.ps1"
        helper_path.write_text("\ufeff" + _INSTALLER_UPDATE_HELPER_SCRIPT, encoding="utf-8")
        _set_update_state(status="installing", downloaded_bytes=downloaded,
                          total_bytes=downloaded, error="")
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            raise RuntimeError("找不到 Windows PowerShell，无法应用更新")
        subprocess.Popen(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(helper_path),
             "-ParentPid", str(os.getpid()),
             "-SetupPath", str(setup_path),
             "-InstallDir", install_dir,
             "-Port", str(_APP_PORT),
             "-Version", str(release["version"]),
             "-Scope", scope],
            cwd=str(work_dir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            close_fds=True,
        )
        _SHUTDOWN.set()
        if _UVICORN_SERVER is not None:
            threading.Timer(2.0, lambda: setattr(_UVICORN_SERVER, "should_exit", True)).start()
            threading.Timer(15.0, _force_exit_if_alive).start()
    except _UpdateCancelled:
        log.info("更新下载已取消")
        _set_update_state(status="cancelled", error="", downloaded_bytes=0, total_bytes=0)
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
    except Exception as exc:
        _set_update_state(status="error", error=str(exc))
        _write_update_error(str(exc))
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


@app.get("/api/version")
def app_version_get():
    return {
        "version": _app_version(),
        "commit": BUILD_COMMIT,
        "build_date": BUILD_DATE,
        "author": AUTHOR,
        "homepage": HOMEPAGE,
        "license": LICENSE_NAME,
        "copyright": COPYRIGHT,
        "install_mode": "installed" if _installed_info() else "portable",
        "supports_self_update": bool(getattr(sys, "frozen", False) and os.name == "nt"),
    }


@app.post("/api/update/check")
def update_check():
    global _UPDATE_RELEASE
    current_version = _app_version()
    try:
        release = _fetch_latest_release()
        latest_version = release["version"]
        # 开发构建（dev）等解析不出版本号的，不做版本比较、也不提供应用内更新：
        # 否则会被当成"比任何正式版都旧"，一点检查就开始下载并覆盖当前程序。
        version_comparable = _version_key(current_version) is not None
        update_available = (version_comparable and
                            _is_newer_version(latest_version, current_version))
    except Exception as exc:
        _set_update_state(status="error", current_version=current_version,
                          latest_version="", error=str(exc))
        raise HTTPException(status_code=502, detail=f"检查 GitHub Release 失败：{exc}") from exc

    # 检查成功：上一次留下的失败记录可以清掉了
    try:
        UPDATE_ERROR_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    supports_self_update = bool(getattr(sys, "frozen", False) and os.name == "nt")
    with _UPDATE_LOCK:
        _UPDATE_RELEASE = release if update_available else None
        UPDATE_STATE.update(
            status="available" if update_available else "up_to_date",
            current_version=current_version,
            latest_version=latest_version,
            version_comparable=version_comparable,
            downloaded_bytes=0,
            total_bytes=0,
            error="",
        )
    return {
        "current_version": current_version,
        "latest_version": latest_version,
        "update_available": update_available,
        "version_comparable": version_comparable,
        "supports_self_update": supports_self_update,
        "install_mode": "installed" if _installed_info() else "portable",
        "release_url": release["release_url"],
    }


@app.post("/api/update/install")
def update_install():
    global _UPDATE_RELEASE
    if not getattr(sys, "frozen", False) or os.name != "nt":
        raise HTTPException(status_code=400, detail="自动覆盖更新仅支持 Windows 便携版")
    if _version_key(_app_version()) is None:
        # 与 update_check 一致：开发构建不参与版本比较，也就不能用应用内更新覆盖。
        raise HTTPException(
            status_code=400,
            detail=f"当前是开发构建（{_app_version()}），不提供应用内更新；请手动下载 Release 包")
    active_jobs = ("scan_run", "analyze_run", "apply_run", "folder_merge_run",
                   "folder_merge_ai_run", "folder_profile_run")
    if any(isinstance(APP.get(key), dict) and APP[key].get("running") for key in active_jobs):
        raise HTTPException(status_code=409, detail="有任务正在运行，请任务结束后再检查更新")

    with _UPDATE_LOCK:
        if UPDATE_STATE.get("status") in ("downloading", "installing"):
            return dict(UPDATE_STATE)
        release = _UPDATE_RELEASE
        if not release:
            raise HTTPException(status_code=409, detail="请先检查是否有可用更新")
        UPDATE_STATE.update(status="downloading", error="", downloaded_bytes=0,
                            total_bytes=0)
    # 安装版走"下载安装包并静默运行"，便携版走原有的 ZIP 覆盖。
    installed = _installed_info()
    if installed:
        threading.Thread(target=_run_installer_update_worker,
                         args=(release, installed), daemon=True).start()
    else:
        threading.Thread(target=_run_update_worker, args=(release,), daemon=True).start()
    return {"ok": True, "status": "downloading", "version": release["version"],
            "install_mode": "installed" if installed else "portable"}


@app.post("/api/update/cancel")
def update_cancel():
    """取消正在进行的更新下载；已进入替换阶段后无法中止。"""
    with _UPDATE_LOCK:
        status = UPDATE_STATE.get("status")
        if status == "installing":
            return JSONResponse(
                {"ok": False,
                 "error": "已经开始替换程序文件，无法中止；替换完成后程序会自动重启"},
                status_code=409)
        if status != "downloading":
            return JSONResponse({"ok": False, "error": "当前没有正在进行的更新下载"},
                                status_code=409)
        _UPDATE_CANCEL.set()
        UPDATE_STATE.update(status="cancelled", error="", downloaded_bytes=0, total_bytes=0)
    log.info("已请求取消更新下载")
    return {"ok": True, "status": "cancelled"}


@app.get("/api/update/status")
def update_status_get():
    return _update_state_snapshot()


# ============ 全局状态 ============
@app.get("/api/status")
def status():
    return {
        "stats": store.stats(),
        "scan": APP["scan_run"],
        "analyze": APP["analyze_run"],
        "apply": APP["apply_run"],
    }


# ============ 静态页面 ============
class _NoCacheStatic(StaticFiles):
    """静态资源不走浏览器启发式缓存。

    否则浏览器按「距 Last-Modified 时间的 10%」自算新鲜期，
    在有效期内直接用本地缓存、不回源，导致改完前端刷新后看到的还是旧页面。
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app.mount("/static", _NoCacheStatic(directory=str(STATIC_DIR)), name="static")


def _asset_version() -> str:
    """静态资源版本号：内容变就变，浏览器缓存自动失效。

    index.html 里的 ?v= 在这里按实际内容改写，无需手动维护版本号。
    否则改了 app.js 却忘改 ?v=，浏览器会用「旧 JS + 新 HTML」：
    旧脚本一旦引用已被移除的元素就抛错，整个前端初始化中断。
    """
    parts = []
    for name in ("app.js", "style.css"):
        try:
            st = (STATIC_DIR / name).stat()
            parts.append(f"{name}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(name)
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:10]


_ASSET_VERSION_RE = re.compile(r"\?v=[A-Za-z0-9._-]*")


@app.get("/")
def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = _ASSET_VERSION_RE.sub("?v=" + _asset_version(), html)
    token_script = f"<script>window.__BILI_FAV_TOKEN__={json.dumps(_LOCAL_API_TOKEN)};</script>"
    if "<head>" in html:
        html = html.replace("<head>", "<head>" + token_script, 1)
    else:
        html = token_script + html
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, must-revalidate"})


def main():
    global _UVICORN_SERVER, _APP_PORT
    parser = argparse.ArgumentParser(description="B站收藏夹整理")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--log-file", default="server.log",
                        help="服务日志文件（默认保存在用户数据目录，可指定其它名字）")
    parser.add_argument("--no-browser", action="store_true",
                        help="启动服务后不自动打开浏览器")
    args = parser.parse_args()
    _APP_PORT = args.port

    log_path = Path(args.log_file)
    if not log_path.is_absolute():
        log_path = USER_DATA_DIR / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # 先把 uvicorn 的日志配置建好，再建自己的 handler：
    # uvicorn.Config 内部会 dictConfig，而 dictConfig 会关掉此前创建的所有 handler
    # （stream 被置空、再写入就无效），所以自己的 handler 只能在它之后创建。
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="info",
                            timeout_graceful_shutdown=5)

    # 服务自己的日志（logger 名 server）：dictConfig 之后 root 已没有可用 handler，这里补回控制台
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(console)

    # 控制台保持 uvicorn 原生格式（带配色），文件里另存一份纯文本
    try:
        fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
        fh.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(fh)
        # uvicorn 的日志分成两个 logger，各自的 handler 要单独挂
        for name in ("uvicorn", "uvicorn.access"):
            logging.getLogger(name).addHandler(fh)
        log.info("日志文件：%s", log_path)
    except Exception as e:
        log.warning("无法写入日志文件 %s：%s", log_path, e)

    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    _log_attribution()
    _cleanup_stale_update_leftovers(APP_DIR.parent, Path(tempfile.gettempdir()))
    _persist_api_token()
    restored = _load_persisted_token()
    if restored != _LOCAL_API_TOKEN:
        log.warning("token.blob 解密结果与当前 token 不一致（跨用户或损坏）")
    _app_lock_refresh()
    if _APP_LOCK["enabled"]:
        log.info("应用密码已启用，当前状态：%s",
                 "已解锁（本机自动）" if _APP_LOCK["unlocked"] else "已锁定")
    try:
        VAULT.load_meta()
    except vault_mod.VaultError as exc:
        log.error("金库元数据无法读取：%s", exc)
    _UVICORN_SERVER = uvicorn.Server(config)
    _UVICORN_SERVER.run()


if __name__ == "__main__":
    main()
