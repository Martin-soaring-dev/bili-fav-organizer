# -*- coding: utf-8 -*-
"""Secure Vault：DEK 多 KEK 包装（密码 / 设备 / 账号 / 恢复码）。

- DEK：随机 32 字节，用于 AES-GCM 加密数据或整库文件。
- KEK：由密码/恢复码等派生，只用于「包装」DEK，磁盘上不存 DEK 明文。
- 恢复码：高熵字符串，用户离线保管；本模块永不负责展示/落盘明文。
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from Cryptodome.Cipher import AES

PBKDF2_ITERATIONS = 200_000
DEK_LEN = 32
NONCE_LEN = 12
RECOVERY_GROUPS = 5
RECOVERY_GROUP_LEN = 4

_RECOVERY_RE = re.compile(
    r"^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}(?:-[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{4}){4}$"
)
# Crockford-like base32 without confusing 0/1/O/I
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class VaultError(ValueError):
    """验证或解包失败（密码/恢复码错误、数据损坏等）。"""


def _derive_kek(secret: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", secret.encode("utf-8"), salt, iterations, dklen=DEK_LEN
    )


def generate_dek() -> bytes:
    return secrets.token_bytes(DEK_LEN)


def generate_recovery_code() -> str:
    """生成形如 XXXX-XXXX-XXXX-XXXX-XXXX 的高熵恢复码（约 80 bit）。"""
    groups = []
    for _ in range(RECOVERY_GROUPS):
        groups.append("".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(RECOVERY_GROUP_LEN)))
    return "-".join(groups)


def normalize_recovery_code(code: str) -> str:
    cleaned = re.sub(r"[\s-]+", "", (code or "").strip()).upper()
    if len(cleaned) != RECOVERY_GROUPS * RECOVERY_GROUP_LEN:
        raise VaultError("恢复码格式不正确")
    groups = [cleaned[i:i + RECOVERY_GROUP_LEN] for i in range(0, len(cleaned), RECOVERY_GROUP_LEN)]
    normalized = "-".join(groups)
    if not _RECOVERY_RE.match(normalized):
        raise VaultError("恢复码包含无效字符")
    return normalized


def wrap_dek(dek: bytes, secret: str, *, iterations: int = PBKDF2_ITERATIONS) -> dict:
    """用 secret 派生 KEK 并包装 DEK。返回可 JSON 序列化的 wrap。"""
    if len(dek) != DEK_LEN:
        raise VaultError("DEK 长度非法")
    salt = secrets.token_bytes(16)
    kek = _derive_kek(secret, salt, iterations)
    nonce = secrets.token_bytes(NONCE_LEN)
    ct = AES.new(kek, AES.MODE_GCM, nonce=nonce).encrypt_and_digest(dek)
    return {
        "kdf": "pbkdf2-sha256",
        "iterations": int(iterations),
        "salt": salt.hex(),
        "nonce": nonce.hex(),
        "ct": ct[0].hex(),
        "tag": ct[1].hex(),
    }


def unwrap_dek(wrap: dict, secret: str) -> bytes:
    try:
        salt = bytes.fromhex(wrap["salt"])
        nonce = bytes.fromhex(wrap["nonce"])
        ct = bytes.fromhex(wrap["ct"])
        tag = bytes.fromhex(wrap["tag"])
        iterations = int(wrap.get("iterations") or PBKDF2_ITERATIONS)
    except (KeyError, ValueError, TypeError) as exc:
        raise VaultError("包装数据损坏") from exc
    kek = _derive_kek(secret, salt, iterations)
    try:
        dek = AES.new(kek, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ct, tag)
    except ValueError as exc:
        raise VaultError("密码或恢复码不正确") from exc
    if len(dek) != DEK_LEN:
        raise VaultError("DEK 长度非法")
    return dek


def encrypt_blob(dek: bytes, data: bytes) -> bytes:
    """nonce || tag || ct"""
    nonce = secrets.token_bytes(NONCE_LEN)
    ct, tag = AES.new(dek, AES.MODE_GCM, nonce=nonce).encrypt_and_digest(data)
    return nonce + tag + ct


def decrypt_blob(dek: bytes, blob: bytes) -> bytes:
    if len(blob) < NONCE_LEN + 16:
        raise VaultError("密文过短")
    nonce, tag, ct = blob[:NONCE_LEN], blob[NONCE_LEN:NONCE_LEN + 16], blob[NONCE_LEN + 16:]
    try:
        return AES.new(dek, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ct, tag)
    except ValueError as exc:
        raise VaultError("解密失败：数据损坏或 DEK 不正确") from exc


@dataclass
class VaultState:
    password_set: bool = False
    unlocked: bool = False
    onboarding_complete: bool = False
    bound_mid: str = ""
    has_device_wrap: bool = False
    has_account_wrap: bool = False
    has_recovery_wrap: bool = False


class Vault:
    """进程内金库：管理 wraps 元数据与内存中的 DEK。"""

    def __init__(self, meta_path: Path):
        self.meta_path = Path(meta_path)
        self._dek: bytes | None = None
        self._meta = {
            "version": 1,
            "bound_mid": "",
            "onboarding_complete": False,
            "wraps": {},
        }

    # ---------- 元数据 ----------
    def load_meta(self) -> None:
        if not self.meta_path.is_file():
            return
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise VaultError("金库元数据损坏") from exc
        if not isinstance(data, dict) or "wraps" not in data:
            raise VaultError("金库元数据损坏")
        self._meta = data

    def save_meta(self) -> None:
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_suffix(self.meta_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.meta_path)

    @property
    def state(self) -> VaultState:
        wraps = self._meta.get("wraps") or {}
        return VaultState(
            password_set="password" in wraps,
            unlocked=self._dek is not None,
            onboarding_complete=bool(self._meta.get("onboarding_complete")),
            bound_mid=str(self._meta.get("bound_mid") or ""),
            has_device_wrap="device" in wraps,
            has_account_wrap="account" in wraps,
            has_recovery_wrap="recovery" in wraps,
        )

    def is_unlocked(self) -> bool:
        return self._dek is not None

    def lock(self) -> None:
        self._dek = None

    def _require_dek(self) -> bytes:
        if self._dek is None:
            raise VaultError("金库未解锁")
        return self._dek

    def _set_wrap(self, name: str, wrap: dict) -> None:
        self._meta.setdefault("wraps", {})[name] = wrap

    def _pop_wrap(self, name: str) -> None:
        (self._meta.get("wraps") or {}).pop(name, None)

    # ---------- 生成 / 包装 ----------
    def initialize(self, *, bound_mid: str, password: str, recovery_code: str | None = None) -> str:
        """首次启用：生成 DEK 与 password/recovery 包装。返回规范化恢复码明文（仅此一次）。"""
        dek = generate_dek()
        self._dek = dek
        code = normalize_recovery_code(recovery_code or generate_recovery_code())
        self._meta = {
            "version": 1,
            "bound_mid": str(bound_mid or ""),
            "onboarding_complete": False,
            "wraps": {
                "password": wrap_dek(dek, password),
                "recovery": wrap_dek(dek, code),
            },
        }
        self.save_meta()
        return code

    def set_password(self, password: str, *, current_password: str | None = None) -> None:
        dek = self._dek
        if dek is None:
            wraps = self._meta.get("wraps") or {}
            if "password" not in wraps or not current_password:
                raise VaultError("金库未解锁且无法验证当前密码")
            dek = unwrap_dek(wraps["password"], current_password)
            self._dek = dek
        self._set_wrap("password", wrap_dek(dek, password))
        self.save_meta()

    def bind_device_blob(self, device_secret: str) -> dict:
        """用设备 secret（如 DPAPI 明文材料）包装 DEK，返回 wrap。"""
        dek = self._require_dek()
        wrap = wrap_dek(dek, device_secret)
        self._set_wrap("device", wrap)
        self.save_meta()
        return wrap

    def bind_account(self, account_secret: str) -> None:
        """account_secret 由调用方用 mid‖cookie 派生；不在此落盘 secret。"""
        dek = self._require_dek()
        self._set_wrap("account", wrap_dek(dek, account_secret))
        self.save_meta()

    def complete_onboarding(self) -> None:
        self._meta["onboarding_complete"] = True
        self.save_meta()

    def clear_password(self, current_password: str) -> None:
        wraps = self._meta.get("wraps") or {}
        if "password" not in wraps:
            return
        if self._dek is None:
            self._dek = unwrap_dek(wraps["password"], current_password)
        self._pop_wrap("password")
        self.save_meta()

    # ---------- 解锁 ----------
    def unlock_with_password(self, password: str) -> None:
        wraps = self._meta.get("wraps") or {}
        wrap = wraps.get("password")
        if not wrap:
            raise VaultError("尚未设置应用密码")
        self._dek = unwrap_dek(wrap, password)

    def unlock_with_recovery(self, recovery_code: str) -> None:
        wraps = self._meta.get("wraps") or {}
        wrap = wraps.get("recovery")
        if not wrap:
            raise VaultError("没有恢复码包装")
        self._dek = unwrap_dek(wrap, normalize_recovery_code(recovery_code))

    def unlock_with_secret(self, name: str, secret: str) -> None:
        wraps = self._meta.get("wraps") or {}
        wrap = wraps.get(name)
        if not wrap:
            raise VaultError(f"没有 {name} 包装")
        self._dek = unwrap_dek(wrap, secret)

    def try_any_unlock(self, *, password: str | None = None, recovery_code: str | None = None,
                       device_secret: str | None = None, account_secret: str | None = None) -> bool:
        errors = []
        for name, secret in (
            ("password", password), ("recovery", recovery_code),
            ("device", device_secret), ("account", account_secret),
        ):
            if not secret:
                continue
            try:
                self.unlock_with_secret(name, secret if name != "recovery"
                                        else normalize_recovery_code(secret))
                return True
            except VaultError as exc:
                errors.append(f"{name}: {exc}")
        return False

    # ---------- 数据加解密 ----------
    def encrypt(self, data: bytes) -> bytes:
        return encrypt_blob(self._require_dek(), data)

    def decrypt(self, blob: bytes) -> bytes:
        return decrypt_blob(self._require_dek(), blob)

    def encrypt_file(self, src: Path, dest: Path) -> None:
        raw = Path(src).read_bytes()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.encrypt(raw))

    def decrypt_file(self, src: Path, dest: Path) -> None:
        raw = Path(src).read_bytes()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.decrypt(raw))
