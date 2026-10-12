"""Credential display rules; never persist secrets in task journals."""
from __future__ import annotations


def mask_secret(value: str) -> str:
    value = value or ""
    return ("****" + value[-4:]) if len(value) > 4 else ("****" if value else "")
