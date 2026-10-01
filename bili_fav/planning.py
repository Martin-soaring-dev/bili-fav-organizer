"""Plan versioning and review helpers, independent of HTTP handlers."""

DECISION_FIELDS = (
    "action", "target_folder", "create_new_name", "source_folder_id",
    "target_id", "source_ids", "final_name", "delete_sources", "reason",
    "confidence", "profile_basis_versions", "organization_profile_versions",
)
REVIEW_ACTIONS = {"skip", "delete_invalid", "move_to_existing", "create_new"}


def validate_review_items(items: list) -> str:
    """Validate the shape of human-reviewed content decisions before persistence."""
    if not isinstance(items, list):
        return "方案格式无效"
    seen = set()
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            return f"第 {index} 条方案格式无效"
        bvid = str(item.get("bvid") or "").strip()
        if not bvid:
            return f"第 {index} 条方案缺少视频标识"
        if bvid in seen:
            return f"方案中重复包含视频 {bvid}"
        seen.add(bvid)
        action = str(item.get("action") or "skip").strip()
        if action not in REVIEW_ACTIONS:
            return f"视频 {bvid} 的操作类型无效"
        if action == "move_to_existing" and not str(item.get("target_folder") or "").strip():
            return f"视频 {bvid} 尚未选择目标收藏夹"
        if action == "create_new" and not str(
                item.get("create_new_name") or item.get("target_folder") or "").strip():
            return f"视频 {bvid} 的新收藏夹名称为空"
    return ""


def build_reviewed_plan(items: list, previous: dict,
                        profile_versions: dict | None = None) -> tuple[dict, int]:
    """Build persisted plan rows and retain completion state for unchanged BVIDs."""
    old = previous or {}
    versions = dict(profile_versions or {})
    classification_actions = {"move_to_existing", "create_new"}
    plan = {}
    kept_done = 0
    for item in items:
        bvid = str(item.get("bvid") or "").strip()
        action = str(item.get("action") or "skip").strip() or "skip"
        entry = {
            "bvid": bvid,
            "action": action,
            "target_folder": str(item.get("target_folder") or "").strip(),
            "create_new_name": str(item.get("create_new_name") or "").strip(),
            "status": "pending",
        }
        if action in classification_actions:
            entry["organization_profile_versions"] = versions
        prev = old.get(bvid) or {}
        if prev.get("status") == "done":
            entry["status"] = "done"
            entry["result"] = prev.get("result", "")
            entry["at"] = prev.get("at", "")
            kept_done += 1
        plan[bvid] = entry
    # Retain entries not included in this review (for example, manually marked invalid videos).
    for bvid, item in old.items():
        plan.setdefault(bvid, item)
    return plan, kept_done


def diff_plans(previous: dict, current: dict) -> dict:
    """Describe decision changes without treating execution status as a plan edit."""
    before = previous or {}
    after = current or {}
    changes = []
    for key in sorted(set(before) | set(after)):
        old = before.get(key)
        new = after.get(key)
        if old is None:
            changes.append({"bvid": str(key), "change": "added", "after": new})
            continue
        if new is None:
            changes.append({"bvid": str(key), "change": "removed", "before": old})
            continue
        old_decision = {field: old.get(field, "") for field in DECISION_FIELDS}
        new_decision = {field: new.get(field, "") for field in DECISION_FIELDS}
        if old_decision != new_decision:
            changes.append({"bvid": str(key), "change": "changed",
                            "before": old_decision, "after": new_decision})
    counts = {kind: sum(change["change"] == kind for change in changes)
              for kind in ("added", "removed", "changed")}
    return {"counts": counts, "total": len(changes), "changes": changes}


def is_snapshot_current(approved: dict, observed: dict) -> bool:
    return bool(approved and observed and
                approved.get("fingerprint") == observed.get("fingerprint"))
