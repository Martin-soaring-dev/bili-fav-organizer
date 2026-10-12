"""Pure version comparison, independent of FastAPI and filesystem."""
from __future__ import annotations

import re


def version_key(version: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"[vV]?(\d+(?:\.\d+)*)", str(version or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer_version(latest: str, current: str) -> bool:
    latest_key, current_key = version_key(latest), version_key(current)
    if latest_key is None:
        raise ValueError(f"Release 版本标签格式不支持：{latest}")
    if current_key is None:
        return True
    width = max(len(latest_key), len(current_key))
    return latest_key + (0,) * (width - len(latest_key)) > (
        current_key + (0,) * (width - len(current_key))
    )
