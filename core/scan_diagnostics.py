"""Read-only, in-memory diagnostic of folder list pagination versus resource ID catalog.

Never log or persist the user's identifiers or login session. Results use an
ephemeral per-request salt and truncated digests, useful only within one report.
"""
from __future__ import annotations

import hashlib
import secrets


def diagnose_folder(session, media_id: str, expected: int) -> dict:
    if not 0 <= expected <= 1000:
        raise ValueError("仅诊断目录计数在 0–1000 之间的收藏夹")
    nonce = secrets.token_bytes(16)

    def digest(key):
        return hashlib.sha256(nonce + str(key).encode("utf-8")).hexdigest()[:20]

    pages = []
    page_keys = []
    # Use existing pagination and signing so the diagnosis follows the actual scan path.
    def page_event(ev):
        typ = ev.get("type")
        if typ == "req":
            pages.append({"page": int(ev["pn"]), "returned": 0, "empty": False})
        elif typ == "page" and pages:
            pages[-1]["returned"] = int(ev["got"])
        elif typ == "empty_page" and pages:
            pages[-1]["empty"] = True

    for item in session.iter_folder_videos(str(media_id), expected, on_event=page_event):
        key = f"{int(item.get('type') or 2)}:{str(item.get('id') or item.get('aid') or '')}"
        page_keys.append(key)

    id_keys = []
    ids_error = None
    try:
        # Intentionally read raw IDs to preserve evidence if the API returns
        # a partial list: get_folder_resource_ids rejects that list immediately.
        session._throttle_read()
        raw = session._request("GET", f"{session._wapi}/x/v3/fav/resource/ids",
                               params={"media_id": str(media_id), "platform": "web"})
        if not isinstance(raw, list):
            ids_error = "resource/ids 返回非列表"
        else:
            for item in raw:
                if not isinstance(item, dict) or item.get("id") is None or item.get("type") is None:
                    ids_error = "resource/ids 包含无效条目"
                    continue
                id_keys.append(f"{int(item['type'])}:{str(item['id'])}")
    except Exception as exc:
        # No API URL, response body, or cookie is exposed to UI.
        ids_error = type(exc).__name__

    pset, iset = set(page_keys), set(id_keys)
    return {
        "read_only": True,
        "directory_count": expected,
        "paged_count": len(page_keys),
        "paged_unique_count": len(pset),
        "ids_count": len(id_keys),
        "ids_unique_count": len(iset),
        "ids_error": ids_error,
        "pages": pages,
        "ids_not_in_pages": sorted(digest(key) for key in iset - pset) if ids_error is None else [],
        "pages_not_in_ids": sorted(digest(key) for key in pset - iset) if ids_error is None else [],
        "digest_note": "临时随机盐哈希仅在本次结果内用于定位，不可跨次对照；不包含视频名称或登录凭据",
    }
