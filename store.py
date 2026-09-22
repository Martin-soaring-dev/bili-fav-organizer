# -*- coding: utf-8 -*-
"""本地数据存取层。

职责：
  - 把抓取到的收藏夹/视频数据缓存成 JSON，实现扫描断点续跑
  - 存 LLM 分析结果（每个 bvid 一条），实现分析断点续跑
  - 存执行阶段的进度（哪些已处理），支持中断后继续
  - 提供读写接口给 server 和各阶段使用

所有数据放在 data/ 子目录，按阶段分文件，便于查看和清理。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
DATA_DIR.mkdir(exist_ok=True)

FOLDERS_FILE = DATA_DIR / "folders.json"
VIDEOS_FILE = DATA_DIR / "videos.json"          # {bvid: video_record}
ANALYSIS_FILE = DATA_DIR / "analysis.json"      # {bvid: {recommended, reason, confidence, ...}}
PLAN_FILE = DATA_DIR / "plan.json"              # {bvid: plan_record}
APPLY_STATE_FILE = DATA_DIR / "apply_state.json"  # 执行进度
SCAN_DONE_FILE = DATA_DIR / "scan_done.json"    # 已完整扫描的收藏夹 media_id 列表
SCAN_SELECTION_FILE = DATA_DIR / "scan_selection.json"
FOLDER_MERGE_PLAN_FILE = DATA_DIR / "folder_merge_plan.json"
FOLDER_MERGE_STATE_FILE = DATA_DIR / "folder_merge_state.json"
FOLDER_MERGE_DRAFT_FILE = DATA_DIR / "folder_merge_draft.json"

_lock = threading.Lock()


def _read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _write_json(path: Path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------- 收藏夹 ----------
def save_folders(folders: list):
    with _lock:
        _write_json(FOLDERS_FILE, folders)


def load_folders() -> list:
    with _lock:
        return _read_json(FOLDERS_FILE, [])


# ---------- 视频 ----------
def save_videos(videos: dict):
    """videos: {bvid: record}。增量合并保存。"""
    with _lock:
        cur = _read_json(VIDEOS_FILE, {})
        cur.update(videos)
        _write_json(VIDEOS_FILE, cur)


def load_videos_raw() -> dict:
    with _lock:
        return _read_json(VIDEOS_FILE, {})


def load_videos() -> list:
    with _lock:
        return list(_read_json(VIDEOS_FILE, {}).values())


def clear_videos():
    with _lock:
        _write_json(VIDEOS_FILE, {})


def replace_videos(videos: dict):
    """整体替换视频索引（用于重新扫描时移除旧的收藏夹归属）。"""
    with _lock:
        _write_json(VIDEOS_FILE, videos or {})


# ---------- LLM 分析 ----------
def save_analysis(items: dict):
    with _lock:
        cur = _read_json(ANALYSIS_FILE, {})
        cur.update(items)
        _write_json(ANALYSIS_FILE, cur)


def load_analysis_raw() -> dict:
    with _lock:
        return _read_json(ANALYSIS_FILE, {})


def load_analysis() -> list:
    with _lock:
        return list(_read_json(ANALYSIS_FILE, {}).values())


def clear_analysis():
    with _lock:
        _write_json(ANALYSIS_FILE, {})


# ---------- 归类方案 ----------
def save_plan(items: dict):
    with _lock:
        cur = _read_json(PLAN_FILE, {})
        cur.update(items)
        _write_json(PLAN_FILE, cur)


def load_plan_raw() -> dict:
    with _lock:
        return _read_json(PLAN_FILE, {})


def load_plan() -> list:
    with _lock:
        return list(_read_json(PLAN_FILE, {}).values())


def clear_plan():
    with _lock:
        _write_json(PLAN_FILE, {})


def replace_plan(items: dict):
    """整体替换 plan（用于删除条目——save_plan 是合并，删不掉）。"""
    with _lock:
        _write_json(PLAN_FILE, items or {})


# ---------- 执行进度 ----------
def save_apply_state(state: dict):
    with _lock:
        _write_json(APPLY_STATE_FILE, state)


def load_apply_state() -> dict:
    with _lock:
        return _read_json(APPLY_STATE_FILE, {})


def clear_apply_state():
    with _lock:
        _write_json(APPLY_STATE_FILE, {})


# ---------- 扫描进度(按收藏夹断点续扫) ----------
def save_scan_done(done_ids: list):
    with _lock:
        _write_json(SCAN_DONE_FILE, done_ids)


def load_scan_done() -> list:
    with _lock:
        return _read_json(SCAN_DONE_FILE, [])


def clear_scan_done():
    with _lock:
        _write_json(SCAN_DONE_FILE, [])


def save_scan_selection(folder_ids: list):
    """保存用户明确选择的扫描范围。"""
    with _lock:
        _write_json(SCAN_SELECTION_FILE, [str(x) for x in folder_ids])


def load_scan_selection() -> list:
    with _lock:
        return _read_json(SCAN_SELECTION_FILE, [])


# ---------- 收藏夹合并 ----------
def save_folder_merge_plan(groups: list):
    with _lock:
        _write_json(FOLDER_MERGE_PLAN_FILE, groups or [])


def load_folder_merge_plan() -> list:
    with _lock:
        return _read_json(FOLDER_MERGE_PLAN_FILE, [])


def save_folder_merge_draft(groups: list):
    with _lock:
        _write_json(FOLDER_MERGE_DRAFT_FILE, groups or [])


def load_folder_merge_draft() -> list:
    with _lock:
        return _read_json(FOLDER_MERGE_DRAFT_FILE, [])


def save_folder_merge_state(state: dict):
    with _lock:
        _write_json(FOLDER_MERGE_STATE_FILE, state or {})


def load_folder_merge_state() -> dict:
    with _lock:
        return _read_json(FOLDER_MERGE_STATE_FILE, {})


# ---------- 项目数据：导出 / 导入 / 清除 ----------
def export_all() -> dict:
    """把全部项目数据打包成可序列化的 dict（不含 cookie / api_key）。"""
    return {
        "version": 1,
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "folders": load_folders(),
        "videos": load_videos_raw(),
        "analysis": load_analysis_raw(),
        "plan": load_plan_raw(),
        "scan_done": load_scan_done(),
        "scan_selection": load_scan_selection(),
        "folder_merge_plan": load_folder_merge_plan(),
        "folder_merge_draft": load_folder_merge_draft(),
        "folder_merge_state": load_folder_merge_state(),
    }


def import_all(bundle: dict) -> dict:
    """用导入的 bundle 覆盖项目数据。返回各数据集条数。"""
    with _lock:
        if "folders" in bundle:
            _write_json(FOLDERS_FILE, bundle.get("folders") or [])
        if "videos" in bundle:
            _write_json(VIDEOS_FILE, bundle.get("videos") or {})
        if "analysis" in bundle:
            _write_json(ANALYSIS_FILE, bundle.get("analysis") or {})
        if "plan" in bundle:
            _write_json(PLAN_FILE, bundle.get("plan") or {})
        if "scan_done" in bundle:
            _write_json(SCAN_DONE_FILE, bundle.get("scan_done") or [])
        if "scan_selection" in bundle:
            _write_json(SCAN_SELECTION_FILE, bundle.get("scan_selection") or [])
        if "folder_merge_plan" in bundle:
            _write_json(FOLDER_MERGE_PLAN_FILE, bundle.get("folder_merge_plan") or [])
        if "folder_merge_draft" in bundle:
            _write_json(FOLDER_MERGE_DRAFT_FILE, bundle.get("folder_merge_draft") or [])
        if "folder_merge_state" in bundle:
            _write_json(FOLDER_MERGE_STATE_FILE, bundle.get("folder_merge_state") or {})
    return stats()


def clear_scope(scope: str) -> list:
    """清除指定范围的数据。scope: scan / analysis / plan / all。返回被清除的数据集。"""
    cleared = []
    with _lock:
        if scope in ("scan", "all"):
            _write_json(FOLDERS_FILE, [])
            _write_json(VIDEOS_FILE, {})
            _write_json(SCAN_DONE_FILE, [])
            _write_json(SCAN_SELECTION_FILE, [])
            cleared += ["folders", "videos", "scan_done", "scan_selection"]
        if scope in ("analysis", "all"):
            _write_json(ANALYSIS_FILE, {})
            cleared.append("analysis")
        if scope in ("plan", "all"):
            _write_json(PLAN_FILE, {})
            _write_json(APPLY_STATE_FILE, {})
            cleared += ["plan", "apply_state"]
        if scope in ("folder_merge", "all"):
            _write_json(FOLDER_MERGE_PLAN_FILE, [])
            _write_json(FOLDER_MERGE_DRAFT_FILE, [])
            _write_json(FOLDER_MERGE_STATE_FILE, {})
            cleared += ["folder_merge_plan", "folder_merge_draft", "folder_merge_state"]
    return cleared


# ---------- 统计辅助 ----------
def stats() -> dict:
    return {
        "folders": len(load_folders()),
        "videos": len(load_videos_raw()),
        "analyzed": len(load_analysis_raw()),
        "planned": len(load_plan_raw()),
    }
