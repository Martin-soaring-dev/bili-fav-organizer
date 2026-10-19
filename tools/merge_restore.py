# -*- coding: utf-8 -*-
"""把 9/23 JSON 备份 + 9/24 项目库合并进当前应用数据库。

合并规则：
  · 字段值「更新的来源优先、非空优先」：现库(10/09) > 项目库(9/24) > JSON备份(9/23)
  · 例外 1：fav_time 优先取 JSON 备份 —— 批量元数据接口不返回收藏时间，
    bili_api.py:695 用 ctime 兜底，写进去的值是错的
  · 例外 2：占位标题「已失效视频」不覆盖备份里的真实标题
  · 收藏归属 folder_ids 取最新一次扫描
用法：python tools/merge_restore.py            # dry-run
      python tools/merge_restore.py --apply    # 写入（先自动备份现库）
"""
import json
import os
import shutil
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from core.database.profile_lock import ProfileFileLock

BK = HERE / "data" / "backups" / "bili-data-before-clear-20260923_212602"
OLD = HERE / "data" / "library.sqlite3"
LIVE = Path(os.environ["LOCALAPPDATA"]) / "BiliFavOrganizer" / "data" / "library.sqlite3"
INVALID_TITLE = "已失效视频"
LAYERS = ("s1", "s2", "s3")          # 由旧到新
APPLY = "--apply" in sys.argv


def load_json(p, default):
    p = Path(p)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def empty(v):
    return v is None or v == "" or v == [] or v == {} or v == 0


def read_db(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        videos = {str(r["resource_key"]): json.loads(r["record_json"])
                  for r in c.execute("SELECT resource_key,record_json FROM videos")}
        items = {}
        for r in c.execute("SELECT resource_key,media_id FROM folder_items"):
            items.setdefault(str(r["resource_key"]), set()).add(str(r["media_id"]))
        # 旧库（9/24）还没有 status / directory_order 这些列，按实际列取
        fcols = {r[1] for r in c.execute("PRAGMA table_info(folders)")}
        scols = {r[1] for r in c.execute("PRAGMA table_info(folder_scan_state)")}
        fwant = [x for x in ("media_id", "title", "remote_count", "record_json",
                             "status", "archived_at", "directory_order") if x in fcols]
        swant = [x for x in ("media_id", "status", "strategy", "expected_count", "fetched_count",
                             "snapshot_started_at", "snapshot_completed_at", "last_error")
                 if x in scols]
        folders = [dict(r) for r in c.execute(f"SELECT {','.join(fwant)} FROM folders")]
        scan = [dict(r) for r in c.execute(f"SELECT {','.join(swant)} FROM folder_scan_state")]
        return videos, items, folders, scan
    finally:
        c.close()


def resource_key(rec):
    bvid = str(rec.get("bvid") or "").strip()
    if bvid.startswith("BV"):
        return bvid
    rid = rec.get("id") or rec.get("aid") or ""
    rtype = rec.get("type") or 2
    return f"{rtype}:{rid}" if rid else bvid


def pick_value(old, new, key):
    """新值优先，但空值和占位标题不覆盖。"""
    if new is None or new == []:
        return old
    if empty(new):
        return old
    if key == "title" and new == INVALID_TITLE and old not in (None, "", INVALID_TITLE):
        return old
    return new


def main():
    lock = ProfileFileLock(LIVE.parent / ".bfo-profile.lock")
    # Cooperating current builds use this same profile lock for every normal DB
    # connection and maintenance operation. Older builds do not; close them
    # before running this one-off recovery tool.
    with lock.maintenance(timeout=30.0):
        _merge_under_profile_lock()


def _merge_under_profile_lock():
    s1 = load_json(BK / "videos.json", {})
    s1_an = load_json(BK / "analysis.json", {})
    s1_pl = load_json(BK / "plan.json", {})
    s2_v, s2_fi, s2_folders, s2_scan = read_db(OLD)
    s3_v, s3_fi, s3_folders, s3_scan = read_db(LIVE)

    # 空 bvid 的失效视频 → 找回原始 BV 号
    aid2bvid = {}
    for src in (s2_v, s3_v, s1):
        for rec in src.values():
            b = str(rec.get("bvid") or "").strip()
            if b:
                aid2bvid[str(rec.get("aid") or rec.get("id") or "")] = b

    def ident(rec):
        b = str(rec.get("bvid") or "").strip()
        if b:
            return b
        return aid2bvid.get(str(rec.get("aid") or rec.get("id") or "")) or ""

    # 收集三层：identity -> {layer: (rec, folders)}
    data = {}
    stats = Counter()

    def overlay(layer, key, rec, folders):
        i = ident(rec)
        if not i:
            i = str(key)
        data.setdefault(i, {})[layer] = (dict(rec), set(str(x) for x in folders if str(x)))
        stats[layer] += 1

    for key, rec in s1.items():
        overlay("s1", key, rec, rec.get("folder_ids") or s1.get(key, {}).get("folder_ids") or [])
    for key, rec in s2_v.items():
        overlay("s2", key, rec, s2_fi.get(key) or rec.get("folder_ids") or [])
    for key, rec in s3_v.items():
        overlay("s3", key, rec, s3_fi.get(key) or rec.get("folder_ids") or [])

    # 逐字段合并
    final = {}
    fav_patched = 0
    for i, layers in data.items():
        merged, folders = {}, set()
        for layer in LAYERS:                      # 旧 -> 新
            if layer not in layers:
                continue
            rec, fids = layers[layer]
            for k, v in rec.items():
                merged[k] = pick_value(merged.get(k), v, k) if k in merged else v
            folders = fids or folders             # 新层有归属就用新层
        if "s1" in layers and layers["s1"][0].get("fav_time"):
            s1fav = layers["s1"][0]["fav_time"]
            if s1fav != merged.get("fav_time"):
                merged["fav_time"] = s1fav
                fav_patched += 1
        if not folders:
            for layer in reversed(LAYERS):
                if layer in layers and layers[layer][1]:
                    folders = layers[layer][1]
                    break
        merged["folder_ids"] = sorted(folders)
        if folders and str(merged.get("source_folder_id") or "") not in folders:
            merged["source_folder_id"] = merged["folder_ids"][0]
        elif not merged.get("source_folder_id") and folders:
            merged["source_folder_id"] = merged["folder_ids"][0]
        rk = resource_key(merged)
        final[rk] = (merged, merged["folder_ids"])

    # 分析 / 方案：只保留仍在库里的视频
    bvids = {k for k in final if k.startswith("BV")}
    analysis = {k: v for k, v in s1_an.items() if k in bvids}
    plans = {k: v for k, v in s1_pl.items() if k in bvids}

    # 扫描状态：现库 9 个夹原样保留，其余按 9/24 快照补一行并标记 stale
    live_scan = {r["media_id"]: r for r in s3_scan}
    old_scan = {r["media_id"]: r for r in s2_scan}
    remote = {str(f["media_id"]): int(f["remote_count"] or 0) for f in s3_folders}
    new_scan = []
    for mid in {str(f["media_id"]) for f in s2_folders}:
        if mid in live_scan:
            continue
        st = old_scan.get(mid) or {}
        new_scan.append({
            "media_id": mid, "status": "stale",
            "strategy": st.get("strategy") or "bulk_ids_infos",
            "expected_count": remote.get(mid, st.get("expected_count") or 0),
            "fetched_count": sum(1 for _, fids in final.values() if mid in fids),
            "snapshot_started_at": st.get("snapshot_started_at"),
            "snapshot_completed_at": st.get("snapshot_completed_at"),
            "last_error": "数据恢复自 2026-09-24 扫描；建议重扫校验",
        })

    # ---- 报告 ----
    print("三层覆盖：JSON备份 %d | 项目库 %d | 现库 %d" % (stats["s1"], stats["s2"], stats["s3"]))
    print("合并后视频：%d 条（现库原有 %d 条）" % (len(final), len(s3_v)))
    print("收藏时间修正：%d 条" % fav_patched)
    print("分析 %d / %d 条，方案 %d / %d 条" % (len(analysis), len(s1_an), len(plans), len(s1_pl)))
    print("folder_scan_state：保留现库 %d 条 + 新增 stale %d 条" % (len(live_scan), len(new_scan)))
    print("folder_items：%d 行" % sum(len(f) for _, f in final.values()))
    bad = sum(1 for rec, _ in final.values()
              if rec.get("fav_time") and rec.get("pubtime") and rec["fav_time"] < rec["pubtime"])
    print("合并后 fav_time < pubtime（本应为 0）：%d" % bad)
    ph = sum(1 for rec, _ in final.values() if rec.get("title") == INVALID_TITLE)
    print("仍为占位标题的失效视频：%d" % ph)
    only_live = [k for k in final if k in {resource_key(r) for r in s3_v.values()}]
    new_only = set(final) - set(s2_v) - set(s1)
    print("现库独有（备份中没有）：%s" % sorted(new_only)[:10])

    # 抽样核对
    samples = ["BV1hVbkeKEni", "BV11y4y177tc"]
    if new_only:
        samples.append(sorted(new_only)[0])
    print("\n抽样：")
    for s in samples:
        rec, fids = final.get(s, (None, None))
        if not rec:
            print("  %s 不在合并结果里" % s)
            continue
        print("  %s | %s | fav=%s pub=%s | type=%s attr=%s | folders=%s"
              % (s, (rec.get("title") or "")[:28], rec.get("fav_time"), rec.get("pubtime"),
                 rec.get("type"), rec.get("attr"), fids))

    if not APPLY:
        print("\n[dry-run] 未写入。加 --apply 执行。")
        return

    bak_dir = LIVE.parent / "backups" / ("manual-merge-restore-" + time.strftime("%Y%m%d_%H%M%S"))
    bak_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LIVE, timeout=60)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        shutil.copy2(LIVE, bak_dir / LIVE.name)
        print("已备份现库 ->", bak_dir)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM videos")
        conn.execute("DELETE FROM folder_items")
        for rk, (rec, fids) in final.items():
            bvid = str(rec.get("bvid") or "")
            rid = str(rec.get("id") or rec.get("aid") or "")
            try:
                rtype = int(rec.get("type") or 2)
            except (TypeError, ValueError):
                rtype = 2
            conn.execute(
                "INSERT INTO videos(resource_key,bvid,resource_id,resource_type,record_json,updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (rk, bvid, rid, rtype,
                 json.dumps(rec, ensure_ascii=False, separators=(",", ":")), now))
            mids = set(fids) | {str(rec.get("source_folder_id") or "")} - {""}
            for mid in mids:
                conn.execute("INSERT OR REPLACE INTO folder_items(media_id,resource_key,fav_time)"
                             " VALUES(?,?,?)", (mid, rk, rec.get("fav_time")))
        conn.execute("DELETE FROM analyses")
        for k, v in analysis.items():
            conn.execute("INSERT INTO analyses(resource_key,record_json,status,dependent_ids)"
                         " VALUES(?,?,?,?)",
                         (k, json.dumps(v, ensure_ascii=False, separators=(",", ":")),
                          "stale", "[]"))
        conn.execute("DELETE FROM plans")
        for k, v in plans.items():
            conn.execute("INSERT INTO plans(resource_key,record_json) VALUES(?,?)",
                         (k, json.dumps(v, ensure_ascii=False, separators=(",", ":"))))
        for row in new_scan:
            conn.execute(
                "INSERT OR REPLACE INTO folder_scan_state(media_id,status,strategy,expected_count,"
                "fetched_count,cursor_page,snapshot_started_at,snapshot_completed_at,last_error)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (row["media_id"], row["status"], row["strategy"], row["expected_count"],
                 row["fetched_count"], None, row["snapshot_started_at"],
                 row["snapshot_completed_at"], row["last_error"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    c2 = sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True)
    for t in ("videos", "folder_items", "analyses", "plans", "folder_scan_state", "folders"):
        print("  写入后 %s = %d" % (t, c2.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]))
    c2.close()
    print("完成。备份目录：", bak_dir)


if __name__ == "__main__":
    main()
