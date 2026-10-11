# -*- coding: utf-8 -*-
"""合并后校验：对比写入前备份与写入后的库。"""
import json
import os
import sqlite3
import sys
from pathlib import Path

LIVE = Path(os.environ["LOCALAPPDATA"]) / "BiliFavOrganizer" / "data" / "library.sqlite3"
BAKS = sorted((LIVE.parent / "backups").glob("manual-merge-restore-*"))
BEFORE = BAKS[-1] / LIVE.name if BAKS else None
BK = Path(__file__).resolve().parent.parent / "data" / "backups" / \
    "bili-data-before-clear-20260923_212602"


def load(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    v = {str(r["resource_key"]): json.loads(r["record_json"])
         for r in c.execute("SELECT resource_key,record_json FROM videos")}
    fi = {}
    for r in c.execute("SELECT resource_key,media_id FROM folder_items"):
        fi.setdefault(str(r["resource_key"]), set()).add(str(r["media_id"]))
    meta = {t: c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            for t in ("videos", "folder_items", "analyses", "plans",
                      "folders", "folder_scan_state", "app_state", "providers", "models")}
    integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
    c.close()
    return v, fi, meta, integrity


after_v, after_fi, after_m, integ = load(LIVE)
before_v, before_fi, before_m, _ = load(BEFORE) if BEFORE else ({}, {}, {}, "")

print("integrity_check:", integ)
print("写入前:", before_m)
print("写入后:", after_m)

# 1. 写入前的每条视频必须还在
missing = [k for k in before_v if k not in after_v]
print("\n[1] 写入前的视频丢失数:", len(missing), missing[:5])

# 2. 写入前的收藏归属不能丢
lost_folder = 0
for k, mids in before_fi.items():
    now = after_fi.get(k, set())
    if mids - now:
        lost_folder += 1
print("[2] 归属被删掉的视频数:", lost_folder)

# 3. 现库原有 204 条：title/归属/analysis 不受影响
changed_title = [k for k in before_v if k in after_v
                 and before_v[k].get("title") != after_v[k].get("title")]
print("[3] 写入前视频中标题变化:", len(changed_title))
for k in changed_title[:5]:
    print("    ", k, repr(before_v[k].get("title"))[:30], "->", repr(after_v[k].get("title"))[:40])

# 4. 备份里的收藏时间全部落地
s1 = json.loads((BK / "videos.json").read_text(encoding="utf-8"))
bad_fav = [b for b, rec in s1.items()
          if b in after_v and after_v[b].get("fav_time") != rec.get("fav_time")]
print("[4] 与 9/23 备份收藏时间不一致:", len(bad_fav), bad_fav[:5])

# 5. 备份里的标题全部落地（含 23 条失效视频）
bad_title = [b for b, rec in s1.items()
             if b in after_v and rec.get("title")
             and after_v[b].get("title") != rec.get("title")]
print("[5] 与 9/23 备份标题不一致:", len(bad_title), bad_title[:5])

# 6. 现库独有的 3 条还在
for b in ("BV11Lhm6xECp", "BV1rraT6gE7e", "BV1wx5y6NEDv"):
    print("[6]", b, "在库中" if b in after_v else "缺失")

# 7. 失效视频：真实标题 + attr=1
dead = [k for k, r in after_v.items() if r.get("attr") == 1]
ph = [k for k, r in after_v.items() if r.get("title") == "已失效视频"]
print("[7] attr=1 的视频:", len(dead), "| 仍是占位标题:", len(ph))

# 8. 分析状态
c = sqlite3.connect(f"file:{LIVE}?mode=ro", uri=True)
print("[8] analyses 状态分布:", [tuple(r) for r in c.execute(
    "SELECT status,COUNT(*) FROM analyses GROUP BY status")])
print("[9] folder_scan_state 分布:", [tuple(r) for r in c.execute(
    "SELECT status,COUNT(*) FROM folder_scan_state GROUP BY status")])
orphan = c.execute("SELECT COUNT(*) FROM analyses a LEFT JOIN videos v "
                   "ON a.resource_key=v.resource_key WHERE v.resource_key IS NULL").fetchone()[0]
print("[10] 孤儿分析（视频不存在）:", orphan)
c.close()
