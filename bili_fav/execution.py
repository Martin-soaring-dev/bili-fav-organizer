"""Remote post-condition checks for Bilibili collection writes."""
import time

def _folder_video_aids(session, media_id: str, count: int, should_stop=None) -> set[int]:
    count = max(0, int(count or 0))
    if count == 0:
        return set()
    if count <= 1000:
        resources = session.get_folder_resource_ids(media_id, count, should_stop=should_stop)
        return {int(item["id"]) for item in resources
                if int(item.get("type", 0)) == 2 and str(item.get("id", "")).isdigit()}
    videos = session.iter_folder_videos(media_id, count, should_stop=should_stop)
    aids = set()
    for video in videos:
        value = video.get("aid") or video.get("id")
        try:
            if int(value) > 0:
                aids.add(int(value))
        except (TypeError, ValueError):
            continue
    return aids


def reconcile_batch(session, kind: str, source_id: str, target_id: str,
                    aids: list[int], *, should_stop=None, attempts: int = 3) -> dict:
    """Read remote memberships back and verify a move or delete post-condition.

    The API's resource/ids endpoint is used only within its known safe size. Larger
    folders fall back to resource/list pagination, whose documented page size is 20.
    """
    expected = {int(aid) for aid in aids if int(aid) > 0}
    last = {"source_present": [], "target_present": [], "source_count": None,
            "target_count": None}
    last_error = None
    for attempt in range(max(1, attempts)):
        if should_stop and should_stop():
            break
        try:
            folders = {str(row.get("media_id")): row for row in session.list_folders()}
            source = folders.get(str(source_id))
            target = folders.get(str(target_id)) if target_id else None
            source_count = int((source or {}).get("count", 0) or 0)
            target_count = int((target or {}).get("count", 0) or 0)
            source_aids = _folder_video_aids(session, source_id, source_count, should_stop)
            target_aids = (_folder_video_aids(session, target_id, target_count, should_stop)
                           if target_id else set())
            source_present = sorted(expected & source_aids)
            target_present = sorted(expected & target_aids)
            last = {"source_present": source_present, "target_present": target_present,
                    "source_count": source_count, "target_count": target_count,
                    "attempt": attempt + 1}
            if kind == "delete":
                verified = not source_present
            else:
                verified = not source_present and expected.issubset(target_aids)
            if verified:
                return {**last, "verified": True}
            last_error = None
        except Exception as exc:
            last_error = str(exc)
        if attempt + 1 < attempts:
            time.sleep(0.25)
    result = {**last, "verified": False}
    if last_error:
        result["verification_error"] = last_error
    return result


def reconcile_created_folder(session, media_id: str, title: str) -> dict:
    folders = session.list_folders()
    matches = [row for row in folders
               if str(row.get("media_id", "")) == str(media_id)
               and str(row.get("title", "")) == str(title)]
    return {"verified": len(matches) == 1,
            "media_id": str(media_id), "title": str(title),
            "match_count": len(matches)}


def reconcile_folder_deleted(session, media_id: str) -> dict:
    folders = session.list_folders()
    matches = [row for row in folders if str(row.get("media_id", "")) == str(media_id)]
    return {"verified": not matches, "media_id": str(media_id), "match_count": len(matches)}


def reconcile_folder_renamed(session, media_id: str, title: str) -> dict:
    folders = [row for row in session.list_folders()
               if str(row.get("media_id", "")) == str(media_id)]
    return {"verified": len(folders) == 1 and str(folders[0].get("title", "")) == str(title),
            "media_id": str(media_id), "title": str(title), "match_count": len(folders)}


def reconcile_invalid_cleanup(session, media_id: str, invalid_predicate, *, should_stop=None) -> dict:
    folders = {str(row.get("media_id")): row for row in session.list_folders()}
    folder = folders.get(str(media_id))
    count = int((folder or {}).get("count", 0) or 0)
    records = list(session.iter_folder_videos(media_id, count, should_stop=should_stop)) if count else []
    remaining = sum(bool(invalid_predicate(record)) for record in records)
    return {"verified": remaining == 0, "media_id": str(media_id),
            "count": count, "records_checked": len(records), "invalid_remaining": remaining}
