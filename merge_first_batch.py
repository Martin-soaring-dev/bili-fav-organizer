# -*- coding: utf-8 -*-
"""第一批收藏夹合并。

规则：来源夹批量 move 到目标夹；确认来源夹 media_count=0 后才删除；
整组成功后重命名目标夹。结果不确定或风控时立即停止。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import bili_api
import server


HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "data" / "merge_first_batch_state.json"

MERGES = [
    ("网络与安全", "网络安全", ["网络", "安全"]),
    ("育儿教育", "育儿", ["亲子教育", "教育"]),
    ("编程开发", "编程", ["计算机", "网站开发", "python"]),
    ("算法与数据结构", "数据结构", ["算法"]),
    ("科研学术", "论文写作", ["进入科研状态", "科研人生", "学术研究", "联合培养"]),
    ("个人成长", "个人管理", ["职场", "社交", "处世智慧"]),
    ("软件与工具", "软件&工具", ["Office", "windows"]),
    ("机械制造", "机械", ["制造", "数控", "结构&原理"]),
    ("掌机游戏", "3DS", ["GBA"]),
    ("公益正能量", "正能量", ["公益"]),
    ("娱乐搞笑", "有趣搞笑", ["娱乐", "喜剧脱口秀"]),
]


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def chunks(items: list[int], size: int = 1000):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def folder_map(session: bili_api.BiliSession) -> dict[str, dict]:
    return {f["title"]: f for f in session.list_folders()}


def wait_folder_count(session: bili_api.BiliSession, title: str,
                      *, attempts: int = 6, interval: float = 2.0) -> int:
    """list-all 的 media_count 写后可能短暂延迟，等待其收敛。"""
    last = 0
    for i in range(attempts):
        live = folder_map(session)
        last = int((live.get(title) or {}).get("count", 0) or 0)
        if last == 0:
            return 0
        if i < attempts - 1:
            time.sleep(interval)
    return last


def main() -> int:
    cfg = server.load_config()
    session = bili_api.BiliSession(server.get_session_cookie())
    session.read_interval = int(cfg.get("scan_interval", 2) or 2)
    session.write_interval = float(cfg.get("write_interval", 2) or 2)
    mid = session.get_mid()
    state = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "running",
        "groups": [],
    }
    save_state(state)

    try:
        for final_name, target_name, source_names in MERGES:
            live = folder_map(session)
            target = live.get(final_name) or live.get(target_name)
            group = {"final": final_name, "target": target_name, "sources": [], "status": "running"}
            state["groups"].append(group)
            save_state(state)
            if not target:
                group.update(status="failed", error=f"目标夹不存在：{target_name}")
                save_state(state)
                print(f"[FAIL] {final_name}: 目标夹不存在 {target_name}")
                continue
            target_id = str(target["media_id"])
            group_ok = True

            for source_name in source_names:
                live = folder_map(session)
                source = live.get(source_name)
                result = {"name": source_name, "status": "running"}
                group["sources"].append(result)
                save_state(state)
                if not source:
                    result["status"] = "already_absent"
                    save_state(state)
                    continue
                source_id = str(source["media_id"])
                count = int(source.get("count", 0) or 0)
                videos = list(session.iter_folder_videos(source_id, count)) if count else []
                hidden_invalid = count if count and not videos else 0
                normal_aids = [int(v["aid"]) for v in videos
                               if int(v.get("aid", 0) or 0) > 0
                               and (v.get("title") or "").strip() != "已失效视频"]
                invalid_aids = [int(v["aid"]) for v in videos
                                if int(v.get("aid", 0) or 0) > 0
                                and (v.get("title") or "").strip() == "已失效视频"]
                result.update(before=count, normal=len(normal_aids),
                              invalid=len(invalid_aids) + hidden_invalid)
                try:
                    for batch in chunks(normal_aids):
                        session.move_batch(source_id, target_id, batch, mid=mid)
                    for batch in chunks(invalid_aids):
                        session.batch_delete(source_id, batch)
                    # list-all 有计数、resource/list 却无法枚举：只剩 B 站判定的失效残留。
                    if hidden_invalid:
                        session.clean_invalid_folder(source_id)
                except (bili_api.WriteUncertainError, bili_api.RateLimitedError):
                    raise
                except bili_api.BiliApiError as e:
                    result.update(status="failed", error=str(e))
                    group_ok = False
                    save_state(state)
                    print(f"[FAIL] {source_name} -> {final_name}: {e}")
                    continue

                after = wait_folder_count(session, source_name)
                result["after"] = after
                if after != 0:
                    result.update(status="not_empty", error=f"移动后仍有 {after} 条")
                    group_ok = False
                    save_state(state)
                    print(f"[KEEP] {source_name}: 仍有 {after} 条，未删除收藏夹")
                    continue

                session.delete_folder(source_id)
                result["status"] = "merged_and_deleted"
                save_state(state)
                print(f"[OK] {source_name} -> {final_name}: {count} 条，来源夹已删除")

            if group_ok:
                live = folder_map(session)
                if final_name not in live:
                    session.rename_folder(target_id, final_name)
                group["status"] = "done"
                print(f"[DONE] {target_name} -> {final_name}")
            else:
                group["status"] = "partial"
                print(f"[PARTIAL] {final_name}: 有来源夹未完成，目标夹未重命名")
            save_state(state)

        state["status"] = "done"
        state["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
        return 0
    except bili_api.WriteUncertainError as e:
        state.update(status="unknown", error=str(e), finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        save_state(state)
        print(f"[STOP/UNKNOWN] {e}", file=sys.stderr)
        return 2
    except bili_api.RateLimitedError as e:
        state.update(status="rate_limited", error=str(e), finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        save_state(state)
        print(f"[STOP/RISK] {e}", file=sys.stderr)
        return 3
    except Exception as e:
        state.update(status="error", error=str(e), finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        save_state(state)
        print(f"[STOP/ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
