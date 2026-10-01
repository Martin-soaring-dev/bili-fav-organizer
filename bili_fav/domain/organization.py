"""Collection organization invariants, without FastAPI or persistence concerns."""

DEFAULT_FAVORITE_NAME = "默认收藏夹"
INVALID_VIDEO_TITLE = "已失效视频"


def folder_attr_is_default(folder: dict) -> bool | None:
    """Bilibili's attr bit 1 is clear for the inbox/default collection."""
    attr = folder.get("attr") if isinstance(folder, dict) else None
    if attr is None:
        return None
    try:
        return (int(attr) & 2) == 0
    except (TypeError, ValueError):
        return None


def default_folder(folders: list | None) -> dict | None:
    rows = folders or []
    for folder in rows:
        if str(folder.get("title") or "").strip() == DEFAULT_FAVORITE_NAME:
            return folder
    flagged = [folder for folder in rows if folder_attr_is_default(folder) is True]
    return flagged[0] if len(flagged) == 1 else None


def default_folder_ids(folders: list | None) -> set[str]:
    folder = default_folder(folders)
    return {str(folder["media_id"])} if folder and folder.get("media_id") else set()


def default_folder_names(folders: list | None) -> set[str]:
    folder = default_folder(folders)
    title = str((folder or {}).get("title") or "").strip()
    return {title} if title else set()


def destination_error(items: list[dict], folders: list | None) -> str:
    forbidden = default_folder_names(folders)
    for item in items:
        action = str(item.get("action", "skip"))
        target = str(item.get("target_folder", "")).strip()
        new_name = str(item.get("create_new_name") or target).strip()
        if action == "move_to_existing" and target in forbidden:
            return "默认收藏夹只能移出，不能作为内容整理的移入目标；请修改该条方案"
        if action == "create_new" and new_name in forbidden:
            return "默认收藏夹只能移出，不能作为新建或移入目标；请修改该条方案"
    return ""


def is_invalid_video(video: dict) -> bool:
    """Only the exact invalid-video placeholder title is treated as removable."""
    return (video.get("title") or "").strip() == INVALID_VIDEO_TITLE
