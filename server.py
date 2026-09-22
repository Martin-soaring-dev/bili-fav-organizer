# -*- coding: utf-8 -*-
"""B 站收藏夹智能整理 — 本地 Web 应用主程序。

启动:  python server.py [--port 8080]
打开:  http://127.0.0.1:8080/

四个阶段（前端分区操作，可分别独立运行）：
  1) 获取收藏明细    POST /api/scan
  2) LLM 分析        POST /api/analyze/start   + GET /api/analyze/status
  3) 预归类方案      GET  /api/plan  +  POST /api/plan/apply
  4) 确认执行        POST /api/apply/start     + GET /api/apply/status

LLM 配置写入 config.json（也可在前端 /api/config 修改）：
  { "base_url": "https://api.deepseek.com/v1", "api_key": "...", "model": "qwen3-8b" }
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import bili_api
import llm_analyzer
import store

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
SECRETS_FILE = HERE / "secrets.json"      # 凭据单独存放（api_key / cookie_string）
SECRET_KEYS = ("api_key", "cookie_string")
STATIC_DIR = HERE / "static"

LOG_FORMAT = "%(asctime)s [%(name)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("server")

app = FastAPI(title="B站收藏夹整理")

# ---------- 运行状态（跨请求的全局状态） ----------
APP = {
    "browser": "chrome",
    "session": None,          # bili_api.BiliSession
    "folders": [],            # 现有收藏夹清单
    "scan_run": None,
    "analyze_run": None,      # {running, done, total, failed, stop}
    "apply_run": None,
    "folder_merge_run": None,
    "folder_merge_ai_run": None,
    "events": [],             # 事件缓冲(带自增 id)，供 SSE 推送
}
ANALYZE_WAKE = threading.Event()
_evt_lock = threading.Lock()
_evt_seq = [0]


def emit(level: str, text: str, **extra):
    """追加一条事件到缓冲（供 SSE 推送）。level: info/ok/warn/err。"""
    with _evt_lock:
        _evt_seq[0] += 1
        APP["events"].append({"id": _evt_seq[0], "t": time.strftime("%H:%M:%S"),
                              "level": level, "text": text, **extra})
        # 控制缓冲长度
        if len(APP["events"]) > 2000:
            del APP["events"][:500]


# 把事件钩子注入 bili_api，让长等待(412 退避等)提示也能推到前端
bili_api.EVENT_HOOK = lambda level, text: emit(level, text)
bili_api.WRITE_LOG_PATH = HERE / "data" / "write_operations.jsonl"


DEFAULT_CONFIG = {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "qwen3-8b",
    "scan_interval": 10,        # 收藏夹请求最小间隔(秒)
    "analyze_concurrency": 1,   # LLM 分析并发数(仅影响模型请求)，1~4
    "analyze_batch": 20,        # 每次 LLM 请求包含的视频条数，1~100
    "analyze_max_tokens": 32768,  # 单次输出上限(含推理模型的思考)，界面可设，默认 32K
    "scan_scope": "all",        # 扫描范围: all=全部收藏夹 / default=仅默认收藏夹
    "write_interval": 2,        # 执行阶段的写操作基准间隔(秒)，实际再加 0~1.5s 随机抖动
    "folder_merge_interval": 2,
    "apply_batch": 1000,        # 批量 move / batch-del 的单批条数，硬上限 1000
}


def _pick_default_folder(folders: list) -> dict | None:
    """找出"默认收藏夹"：优先按标题，找不到则取列表第一个（B站把默认夹排在最前）。"""
    for f in folders:
        if (f.get("title") or "").strip() == "默认收藏夹":
            return f
    return folders[0] if folders else None


# ============ 配置 ============
def _atomic_write(path: Path, obj):
    """原子写：先写 .tmp 再替换，避免读到半截 JSON。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json_file(path: Path):
    """返回 (数据, 错误信息)；文件不存在返回 ({}, None)。"""
    if not path.exists():
        return {}, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as e:
        return {}, str(e)


def _mask(v: str) -> str:
    """凭据脱敏：只保留尾 4 位。"""
    v = v or ""
    return ("****" + v[-4:]) if len(v) > 4 else ("****" if v else "")


def load_config() -> dict:
    """读取配置（非凭据来自 config.json，凭据来自 secrets.json）。

    解析失败不再静默回退默认值，而是打日志并置 _config_broken 标记。
    """
    cfg = dict(DEFAULT_CONFIG)
    cdata, cerr = _read_json_file(CONFIG_FILE)
    sdata, serr = _read_json_file(SECRETS_FILE)

    # 迁移：老版本把凭据写在 config.json 里
    if not sdata:
        legacy = {k: cdata.get(k) for k in SECRET_KEYS if cdata.get(k)}
        if legacy:
            try:
                _atomic_write(SECRETS_FILE, legacy)
                for k in SECRET_KEYS:
                    cdata.pop(k, None)
                _atomic_write(CONFIG_FILE, cdata)
                sdata = legacy
                log.info("已把凭据从 config.json 迁移到 secrets.json")
            except Exception as e:
                log.warning("凭据迁移失败：%s", e)

    cfg.update({k: v for k, v in cdata.items() if k not in SECRET_KEYS})
    cfg.update({k: v for k, v in sdata.items() if k in SECRET_KEYS})
    if cerr or serr:
        log.error("配置解析失败：config=%s secrets=%s", cerr, serr)
        cfg["_config_broken"] = True
    return cfg


def save_config(cur: dict):
    """拆分保存：非凭据 → config.json；凭据 → secrets.json。均原子写。"""
    pub = {k: v for k, v in cur.items()
           if k not in SECRET_KEYS and not k.startswith("_")}
    sec = {k: v for k, v in cur.items() if k in SECRET_KEYS}
    _atomic_write(CONFIG_FILE, pub)
    if sec:
        prev, _ = _read_json_file(SECRETS_FILE)
        prev.update(sec)
        _atomic_write(SECRETS_FILE, prev)


def get_session_cookie() -> bili_api.CookieInfo:
    """获取 cookie：优先手动粘贴，其次浏览器自动读取。"""
    cfg = load_config()
    return bili_api.load_browser_cookie(APP["browser"],
                                        manual_cookie=cfg.get("cookie_string", ""))


@app.get("/api/cookie")
def get_cookie():
    """返回当前是否已配置 cookie（不回显原值，只给尾 4 位与长度）。"""
    cfg = load_config()
    ck = cfg.get("cookie_string", "") or ""
    return {"configured": bool(ck.strip()), "length": len(ck), "masked": _mask(ck)}


class CookieIn(BaseModel):
    cookie_string: str = ""


@app.post("/api/cookie")
def set_cookie(body: CookieIn):
    """保存手动粘贴的 cookie（留空 / 脱敏值 → 不覆盖）。"""
    v = (body.cookie_string or "").strip()
    cur = load_config()
    if v and "****" not in v:
        cur["cookie_string"] = v
    save_config(cur)
    return {"ok": True}


@app.get("/api/config")
def get_config():
    """返回配置，但凭据只回**尾 4 位**（P0-1）。"""
    cfg = load_config()
    out = dict(cfg)
    out["api_key"] = ""
    out["api_key_masked"] = _mask(cfg.get("api_key", ""))
    out["api_key_set"] = bool(cfg.get("api_key"))
    ck = cfg.get("cookie_string", "") or ""
    out["cookie_string"] = ""
    out["cookie_masked"] = _mask(ck)
    out["cookie_set"] = bool(ck)
    return out


class ConfigIn(BaseModel):
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    browser: Optional[str] = None
    scan_interval: Optional[int] = None
    analyze_concurrency: Optional[int] = None
    analyze_batch: Optional[int] = None
    analyze_max_tokens: Optional[int] = None
    scan_scope: Optional[str] = None
    write_interval: Optional[int] = None
    folder_merge_interval: Optional[int] = None
    apply_batch: Optional[int] = None


@app.post("/api/config")
def set_config(cfg: ConfigIn):
    cur = load_config()
    if cfg.base_url is not None:
        cur["base_url"] = cfg.base_url
    if cfg.api_key is not None:
        v = (cfg.api_key or "").strip()
        # 留空 / 传回脱敏值 → 不覆盖原 key（P0-1 配套）
        if v and "****" not in v:
            cur["api_key"] = v
    if cfg.model is not None:
        cur["model"] = cfg.model
    if cfg.browser is not None:
        APP["browser"] = cfg.browser
    if cfg.scan_interval is not None:
        cur["scan_interval"] = int(cfg.scan_interval)
        if APP.get("session"):
            APP["session"].read_interval = int(cfg.scan_interval)
    if cfg.analyze_concurrency is not None:
        cur["analyze_concurrency"] = max(1, min(4, int(cfg.analyze_concurrency)))
    if cfg.analyze_batch is not None:
        cur["analyze_batch"] = max(1, min(100, int(cfg.analyze_batch)))
    if cfg.analyze_max_tokens is not None:
        cur["analyze_max_tokens"] = max(256, min(131072, int(cfg.analyze_max_tokens)))
    if cfg.scan_scope is not None:
        cur["scan_scope"] = cfg.scan_scope if cfg.scan_scope in ("all", "default") else "all"
    if cfg.write_interval is not None:
        cur["write_interval"] = max(1, min(60, int(cfg.write_interval)))
        if APP.get("session"):
            APP["session"].write_interval = float(cur["write_interval"])
    if cfg.folder_merge_interval is not None:
        cur["folder_merge_interval"] = max(1, min(60, int(cfg.folder_merge_interval)))
    if cfg.apply_batch is not None:
        cur["apply_batch"] = max(1, min(1000, int(cfg.apply_batch)))
    save_config(cur)
    return {"ok": True, "config": get_config()}


# ============ 项目数据：导出 / 导入 / 清除 ============
INVALID_TITLE = "已失效视频"


def _is_invalid(video: dict) -> bool:
    """防火墙：只有标题**恰好**是「已失效视频」才算失效视频。"""
    return (video.get("title") or "").strip() == INVALID_TITLE


def _autobackup_data(tag: str = "autobackup") -> str:
    """把 data/ 下所有 json 复制到 qwencode/<年>/<月>/bili-data-<tag>-<时间>/。返回路径。"""
    import shutil
    try:
        ts = time.strftime("%Y%m%d_%H%M%S")
        dst = (Path.home() / "qwencode" / time.strftime("%Y") / time.strftime("%m")
               / f"bili-data-{tag}-{ts}")
        dst.mkdir(parents=True, exist_ok=True)
        for p in store.DATA_DIR.glob("*.json"):
            shutil.copy2(p, dst / p.name)
        return str(dst)
    except Exception as e:
        log.warning("自动备份失败: %s", e)
        return ""


class DataIn(BaseModel):
    bundle: dict = {}
    scope: str = "all"
    scopes: list[str] = []


@app.get("/api/data/export")
def data_export():
    """导出全部项目数据为一个 JSON 文件下载（不含 cookie / api_key）。"""
    bundle = store.export_all()
    fn = "bili_fav_data_" + time.strftime("%Y%m%d_%H%M%S") + ".json"
    return JSONResponse(content=bundle,
                        headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@app.post("/api/data/import")
def data_import(body: DataIn):
    """导入项目数据（覆盖当前数据）。导入前自动备份当前数据。"""
    if not body.bundle:
        return JSONResponse({"ok": False, "error": "未收到数据"}, status_code=400)
    bk = _autobackup_data("before-import")
    try:
        st = store.import_all(body.bundle)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"导入失败：{e}"}, status_code=400)
    emit("warn", f"已导入项目数据（覆盖原有）：{st}；原数据已备份到 {bk}")
    return {"ok": True, "stats": st, "backup": bk}


@app.post("/api/data/clear")
def data_clear(body: DataIn):
    """清除指定范围的数据。scope: scan / analysis / plan / all。清除前**自动备份**。"""
    valid = {"scan", "analysis", "plan", "folder_merge", "all"}
    scopes = [x for x in body.scopes if x in valid]
    if not scopes:
        scopes = [body.scope if body.scope in valid else "all"]
    if "all" in scopes:
        scopes = ["all"]
    bk = _autobackup_data("before-clear")
    cleared = []
    for scope in scopes:
        cleared.extend(store.clear_scope(scope))
    cleared = list(dict.fromkeys(cleared))
    emit("warn", f"已清除数据（{', '.join(scopes)}）：{', '.join(cleared)}；清除前已自动备份到 {bk}")
    return {"ok": True, "scopes": scopes, "cleared": cleared,
            "backup": bk, "stats": store.stats()}


# ============ 事件流（SSE） ============
@app.get("/api/events/stream")
async def events_stream(since: int = 0):
    """服务器推送事件流；空闲时不发送任何数据（事件驱动，非轮询）。"""
    from fastapi.responses import StreamingResponse

    async def gen():
        last = int(since)
        # 建立连接时先把历史事件(最近 200 条)补发，避免刷新丢日志
        with _evt_lock:
            backlog = [e for e in APP["events"]][-200:]
        for e in backlog:
            if e["id"] > last:
                last = e["id"]
                yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
        while True:
            await asyncio.sleep(0.4)
            with _evt_lock:
                new = [e for e in APP["events"] if e["id"] > last]
            for e in new:
                last = e["id"]
                yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ============ 阶段1: 收藏夹目录与扫描 ============
class ScanIn(BaseModel):
    folder_ids: Optional[list[str]] = None
    mode: str = "resume"       # resume / rebuild


class ScanSelectionIn(BaseModel):
    folder_ids: list[str]


def _folder_view(folders: list) -> dict:
    done = {str(x) for x in store.load_scan_done()}
    selected = set(store.load_scan_selection())
    rows = [{**f, "media_id": str(f["media_id"]),
             "done": str(f["media_id"]) in done,
             "selected": str(f["media_id"]) in selected} for f in folders]
    return {"folders": rows, "selected_ids": list(selected)}


@app.get("/api/folders")
def folders_get():
    return _folder_view(store.load_folders())


@app.post("/api/folders/refresh")
def folders_refresh():
    if APP["scan_run"] and APP["scan_run"].get("running"):
        return JSONResponse({"ok": False, "error": "扫描运行中，暂不刷新目录"}, status_code=409)
    try:
        session = bili_api.BiliSession(get_session_cookie())
        folders = session.list_folders()
        store.save_folders(folders)
        APP["folders"] = folders
        emit("ok", f"已刷新收藏夹目录：{len(folders)} 个")
        return {"ok": True, **_folder_view(folders)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.put("/api/scan/selection")
def scan_selection(body: ScanSelectionIn):
    valid = {str(f["media_id"]) for f in store.load_folders()}
    ids = list(dict.fromkeys(str(x) for x in body.folder_ids))
    unknown = [x for x in ids if x not in valid]
    if unknown:
        return JSONResponse({"ok": False, "error": f"包含不存在的收藏夹：{unknown[:3]}"}, status_code=400)
    store.save_scan_selection(ids)
    return {"ok": True, "folder_ids": ids}


@app.post("/api/scan")
def scan(body: Optional[ScanIn] = None):
    """读取浏览器 cookie → 拉取收藏夹 → 拉取所有视频。返回扫描结果摘要。"""
    if APP["scan_run"] and APP["scan_run"].get("running"):
        return JSONResponse({"ok": False, "error": "扫描已在运行中"}, status_code=400)

    def run():
        app_state = APP["scan_run"] = {"running": True, "step": "init", "done": 0,
                                       "total": 0, "error": None,
                                       "folder_done": 0, "folder_total": 0, "current": ""}
        try:
            cfg = load_config()
            ci = get_session_cookie()
            session = bili_api.BiliSession(ci)
            session.read_interval = int(cfg.get("scan_interval", 10) or 10)
            APP["session"] = session
            emit("info", f"开始扫描（请求间隔 {session.read_interval} 秒）", kind="scan_start")

            app_state["step"] = "folders"
            folders = store.load_folders()
            if not folders:
                folders = session.list_folders()
                store.save_folders(folders)
            APP["folders"] = folders

            requested = body.folder_ids if body and body.folder_ids is not None else store.load_scan_selection()
            requested = {str(x) for x in requested}
            picked = [f for f in folders if not requested or str(f["media_id"]) in requested]
            if not picked:
                raise ValueError("未选择任何收藏夹")
            store.save_scan_selection([str(f["media_id"]) for f in picked])
            app_state["selected_ids"] = [str(f["media_id"]) for f in picked]
            app_state["mode"] = body.mode if body else "resume"
            emit("info", f"扫描范围：{len(picked)} 个收藏夹")

            total = sum(int(f["count"]) for f in picked)
            emit("info", f"共 {len(picked)} 个收藏夹，预计 {total} 条视频")

            # 扫描顺序：按内容量从少到多（小夹先扫，大夹后扫）
            scan_order = sorted(picked, key=lambda x: int(x["count"]))

            app_state["step"] = "videos"
            app_state["total"] = total

            # 按收藏夹断点续扫：跳过已完整扫描的夹
            done_ids = {str(x) for x in store.load_scan_done()}
            if body and body.mode == "rebuild":
                rebuild_ids = {str(f["media_id"]) for f in picked}
                done_ids -= rebuild_ids
                store.save_scan_done(sorted(done_ids))
                old = store.load_videos_raw()
                kept = {}
                for bvid, rec0 in old.items():
                    rec = dict(rec0)
                    memberships = {str(x) for x in rec.get("folder_ids", [])}
                    legacy_source = str(rec.get("source_folder_id", ""))
                    if not memberships and legacy_source:
                        memberships.add(legacy_source)
                    memberships -= rebuild_ids
                    if memberships:
                        rec["folder_ids"] = sorted(memberships)
                        if legacy_source in rebuild_ids:
                            rec["source_folder_id"] = sorted(memberships)[0]
                        kept[bvid] = rec
                store.replace_videos(kept)
            app_state["folder_total"] = len(picked)
            app_state["folder_done"] = len([f for f in picked if str(f["media_id"]) in done_ids])
            if done_ids:
                emit("info", f"断点续扫：已完成 {len(done_ids)} 个收藏夹，将跳过")

            should_stop = lambda: bool(app_state.get("stop"))
            cur_folder = {"title": "", "total_pages": 1}

            def on_event(ev):
                prog = {"done": app_state["done"], "total": app_state["total"],
                        "unique": len(collected),
                        "fdone": app_state["folder_done"], "ftotal": app_state["folder_total"],
                        "current": cur_folder["title"]}
                if ev.get("type") == "req":
                    if ev["pn"] == 1:
                        cur_folder["total_pages"] = ev["total_pages"]
                        emit("info", f"开始读取「{cur_folder['title']}」"
                                     f"（共 {ev['total_pages']} 页）",
                             kind="scan_progress", **prog)
                elif ev.get("type") == "page":
                    emit("info",
                         f"「{cur_folder['title']}」第 {ev['pn']}/{cur_folder['total_pages']} 页 · "
                         f"本页 {ev['got']} 条 · 累计 {app_state['done']}/{app_state['total']}",
                         kind="scan_progress", **prog)

            collected: dict = {}
            existing_videos = store.load_videos_raw()
            done = sum(int(f.get("count", 0) or 0) for f in picked
                       if str(f["media_id"]) in done_ids)
            app_state["done"] = done
            for f in scan_order:
                if app_state.get("stop"):
                    app_state["error"] = "已手动停止（进度已保存）"
                    emit("warn", "已手动停止（进度已保存）", kind="scan_end")
                    break
                if str(f["media_id"]) in done_ids:
                    continue  # 该夹上次已扫完，跳过
                app_state["current"] = f["title"]
                cur_folder["title"] = f["title"]
                for v in session.iter_folder_videos(f["media_id"], int(f["count"]),
                                                    should_stop=should_stop, on_event=on_event):
                    # 全局按 bvid 去重，同时保留跨收藏夹归属。
                    if v["bvid"]:
                        rec = dict(collected.get(v["bvid"]) or existing_videos.get(v["bvid"]) or v)
                        memberships = {str(x) for x in rec.get("folder_ids", [])}
                        memberships.add(str(f["media_id"]))
                        rec["folder_ids"] = sorted(memberships)
                        collected[v["bvid"]] = rec
                    done += 1
                    app_state["done"] = done
                    if done % 20 == 0:
                        store.save_videos(collected)
                        ANALYZE_WAKE.set()
                # 先落盘已抓到的内容
                store.save_videos(collected)
                app_state["current"] = ""
                # 若中途被停止：该夹未扫完，不得标记为完成（否则会被永久跳过）
                if should_stop():
                    app_state["error"] = "已手动停止（进度已保存）"
                    emit("warn", f"已停止；「{f['title']}」未扫完，下次将重新扫描",
                         kind="scan_end")
                    break
                # 正常扫完才记录为已完成
                done_ids.add(str(f["media_id"]))
                store.save_scan_done(sorted(done_ids))
                app_state["folder_done"] = len([x for x in picked if str(x["media_id"]) in done_ids])
                ANALYZE_WAKE.set()
                emit("ok", f"「{f['title']}」完成（累计 {done} 条）", kind="scan_progress")
            store.save_videos(collected)
            app_state["step"] = "done"
            app_state["done"] = done
            if not app_state.get("error"):
                picked_ids = {str(f["media_id"]) for f in picked}
                scoped_unique = len([v for v in store.load_videos()
                                     if str(v.get("source_folder_id", "")) in picked_ids or
                                     picked_ids.intersection(str(x) for x in v.get("folder_ids", []))])
                emit("ok", f"扫描完成：当前范围共 {scoped_unique} 条去重视频"
                            f"（收藏夹标称数合计 {done} 条）",
                     kind="scan_end", unique=scoped_unique, done=done)
                # P1-5：提示还有哪些收藏夹没扫完
                missing = [f["title"] for f in picked if str(f["media_id"]) not in done_ids]
                if missing:
                    emit("warn", f"尚有 {len(missing)} 个收藏夹未完成："
                                 + "、".join(missing[:8])
                                 + ("…" if len(missing) > 8 else ""),
                         kind="scan_end")
        except bili_api.RateLimitedError as e:
            app_state["error"] = f"风控：{e}"
            emit("err", f"风控：{e}", kind="scan_end")
        except bili_api.BiliApiError as e:
            app_state["error"] = str(e)
            emit("err", f"扫描出错：{e}", kind="scan_end")
        except Exception as e:
            app_state["error"] = f"{e}\n{traceback.format_exc()}"
            emit("err", f"扫描异常：{e}", kind="scan_end")
        finally:
            app_state["running"] = False
            ANALYZE_WAKE.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return {"ok": True, "started": True}


@app.get("/api/scan/status")
def scan_status():
    return APP["scan_run"] or {"running": False, "error": None}


@app.post("/api/scan/stop")
def scan_stop():
    if APP["scan_run"]:
        APP["scan_run"]["stop"] = True
        return {"ok": True}
    return {"ok": False, "error": "没有运行中的扫描"}


@app.get("/api/scan/tree")
def scan_tree():
    """返回收藏夹树：每个夹的名称/应有条数/已扫条数/是否完成，供前端弹窗展示。"""
    folders = store.load_folders()
    done_ids = set(store.load_scan_done())
    videos = store.load_videos_raw()
    counts: dict = {}
    for v in videos.values():
        memberships = v.get("folder_ids") or [v.get("source_folder_id", "")]
        for sid in {str(x) for x in memberships}:
            counts[sid] = counts.get(sid, 0) + 1
    tree = []
    for f in sorted(folders, key=lambda x: int(x.get("count", 0))):
        mid = f["media_id"]
        tree.append({
            "media_id": mid,
            "title": f["title"],
            "expected": int(f["count"]),
            "scanned": counts.get(str(mid), 0),
            "done": mid in done_ids,
        })
    return {
        "folders": tree,
        "done_count": len([f for f in folders if f["media_id"] in done_ids]),
        "total_count": len(folders),
    }


# ============ 收藏夹整理：合并方案与执行 ============
class FolderMergeGroupIn(BaseModel):
    target_id: str
    source_ids: list[str]
    final_name: str
    delete_sources: bool = True
    reason: str = ""
    confidence: float = 0.0
    level: str = ""
    merge_type: str = ""
    risk: str = ""


class FolderMergePlanIn(BaseModel):
    groups: list[FolderMergeGroupIn]


@app.get("/api/folder-organize")
def folder_organize_get():
    saved_run = store.load_folder_merge_state()
    if not APP.get("folder_merge_run") and saved_run.get("running"):
        saved_run.update(running=False, status="interrupted")
    return {"folders": store.load_folders(), "draft": store.load_folder_merge_draft(),
            "plan": store.load_folder_merge_plan(),
            "run": APP.get("folder_merge_run") or saved_run,
            "ai_run": APP.get("folder_merge_ai_run") or {"running": False}}


@app.post("/api/folder-organize/suggest")
def folder_organize_suggest():
    if APP.get("folder_merge_ai_run") and APP["folder_merge_ai_run"].get("running"):
        return JSONResponse({"ok": False, "error": "AI 合并分析已在运行"}, status_code=409)
    folders = store.load_folders()
    videos = store.load_videos()
    if not folders:
        return JSONResponse({"ok": False, "error": "请先刷新收藏夹目录"}, status_code=400)
    cfg = load_config()
    llm_cfg = llm_analyzer.LLMConfig(
        base_url=cfg.get("base_url", ""), api_key=cfg.get("api_key", ""),
        model=cfg.get("model", "qwen3-8b"),
        max_tokens=int(cfg.get("analyze_max_tokens", 32768) or 32768))
    if not llm_cfg.configured:
        return JSONResponse({"ok": False, "error": "LLM 未配置"}, status_code=400)
    state = APP["folder_merge_ai_run"] = {"running": True, "error": None, "count": 0}

    def run():
        try:
            by_folder = {str(f["media_id"]): [] for f in folders}
            overlap_counts: dict[tuple[str, str], int] = {}
            for v in videos:
                memberships = {str(x) for x in v.get("folder_ids", [])}
                if not memberships and v.get("source_folder_id") is not None:
                    memberships.add(str(v.get("source_folder_id")))
                mids = sorted(x for x in memberships if x in by_folder)
                for i, left in enumerate(mids):
                    for right in mids[i + 1:]:
                        overlap_counts[(left, right)] = overlap_counts.get((left, right), 0) + 1
                sample = {"title": (v.get("title") or "")[:100]}
                if v.get("upper_name"): sample["upper"] = str(v["upper_name"])[:40]
                for fid in memberships:
                    bucket = by_folder.get(fid)
                    if bucket is not None:
                        bucket.append(sample)

            def spread_samples(items: list, limit: int = 9) -> list:
                if len(items) <= limit:
                    return items
                indexes = sorted({round(i * (len(items) - 1) / (limit - 1)) for i in range(limit)})
                return [items[i] for i in indexes]

            profiles = []
            for f in folders:
                fid = str(f["media_id"])
                overlaps = []
                for (left, right), n in overlap_counts.items():
                    if fid == left: overlaps.append({"folder_id": right, "count": n})
                    elif fid == right: overlaps.append({"folder_id": left, "count": n})
                overlaps.sort(key=lambda x: x["count"], reverse=True)
                profiles.append({"id": fid, "name": f["title"],
                                 "count": int(f.get("count", 0) or 0),
                                 "samples": spread_samples(by_folder.get(fid, [])),
                                 "overlaps": overlaps[:8]})
            raw_groups = llm_analyzer.suggest_folder_merges(llm_cfg, profiles)
            known = {str(f["media_id"]): f for f in folders}
            used = set()
            groups = []
            for raw in raw_groups:
                target = str(raw.get("target_id", ""))
                sources = list(dict.fromkeys(str(x) for x in raw.get("source_ids", [])))
                try: confidence = float(raw.get("confidence", 0) or 0)
                except Exception: confidence = 0.0
                level = str(raw.get("level", "")).lower()
                if level not in ("high", "medium"):
                    level = "high" if confidence >= 0.82 else "medium"
                members = {target, *sources}
                if (target not in known or not sources or target in sources or
                        any(x not in known for x in sources) or used.intersection(members) or
                        known[target]["title"] == "默认收藏夹" or
                        any(known[x]["title"] == "默认收藏夹" for x in sources) or
                        confidence < 0.68):
                    continue
                used.update(members)
                groups.append({"target_id": target, "target_name": known[target]["title"],
                               "source_ids": sources,
                               "source_names": [known[x]["title"] for x in sources],
                               "final_name": str(raw.get("final_name") or known[target]["title"]).strip(),
                               "delete_sources": True, "status": "pending",
                               "reason": str(raw.get("reason", "")).strip(),
                               "risk": str(raw.get("risk", "")).strip(),
                               "merge_type": str(raw.get("merge_type", "")).strip(),
                               "level": level, "confidence": confidence})
            store.save_folder_merge_draft(groups)
            state["count"] = len(groups)
            emit("ok", f"AI 合并分析完成：生成 {len(groups)} 个候选组",
                 phase="folder_merge_ai", kind="folder_merge_ai_end")
        except Exception as e:
            state["error"] = str(e)
            emit("err", f"AI 合并分析失败：{e}", phase="folder_merge_ai",
                 kind="folder_merge_ai_end")
        finally:
            state["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "started": True}


@app.put("/api/folder-organize/draft")
def folder_organize_draft(body: FolderMergePlanIn):
    known = {str(f["media_id"]): f for f in store.load_folders()}
    groups = []
    for raw in body.groups:
        target = str(raw.target_id)
        sources = list(dict.fromkeys(str(x) for x in raw.source_ids if str(x) != target))
        groups.append({"target_id": target,
                       "target_name": (known.get(target) or {}).get("title", ""),
                       "source_ids": sources,
                       "source_names": [(known.get(x) or {}).get("title", x) for x in sources],
                       "final_name": raw.final_name.strip(), "delete_sources": raw.delete_sources,
                       "reason": raw.reason.strip(), "confidence": raw.confidence,
                       "risk": raw.risk.strip(), "merge_type": raw.merge_type.strip(),
                       "level": raw.level.strip(),
                       "status": "pending"})
    store.save_folder_merge_draft(groups)
    return {"ok": True, "groups": groups}


@app.put("/api/folder-organize/plan")
def folder_organize_plan(body: FolderMergePlanIn):
    known = {str(f["media_id"]): f for f in store.load_folders()}
    old_groups = store.load_folder_merge_plan()
    old_by_key = {(str(g.get("target_id")), tuple(sorted(str(x) for x in g.get("source_ids", []))),
                   (g.get("final_name") or "").strip()): g for g in old_groups}
    used_folders = set()
    groups = []
    for i, raw in enumerate(body.groups, 1):
        target = str(raw.target_id)
        sources = list(dict.fromkeys(str(x) for x in raw.source_ids))
        if target not in known:
            return JSONResponse({"ok": False, "error": f"第 {i} 组目标夹不存在"}, status_code=400)
        if not sources or any(x not in known for x in sources):
            return JSONResponse({"ok": False, "error": f"第 {i} 组来源夹无效"}, status_code=400)
        if target in sources:
            return JSONResponse({"ok": False, "error": f"第 {i} 组的目标夹不能同时是来源夹"}, status_code=400)
        members = {target, *sources}
        if used_folders.intersection(members):
            return JSONResponse({"ok": False, "error": f"第 {i} 组包含已被其他组使用的收藏夹"}, status_code=400)
        used_folders.update(members)
        group = {"target_id": target, "target_name": known[target]["title"],
                       "source_ids": sources,
                       "source_names": [known[x]["title"] for x in sources],
                       "final_name": raw.final_name.strip() or known[target]["title"],
                       "delete_sources": raw.delete_sources, "status": "pending",
                       "reason": raw.reason.strip(), "confidence": raw.confidence}
        group.update(risk=raw.risk.strip(), merge_type=raw.merge_type.strip(), level=raw.level.strip())
        key = (target, tuple(sorted(sources)), group["final_name"])
        previous = old_by_key.get(key)
        if previous and previous.get("status") in ("done", "unknown"):
            group.update(status=previous["status"], results=previous.get("results", []))
            if previous.get("error"): group["error"] = previous["error"]
        groups.append(group)
    store.save_folder_merge_plan(groups)
    return {"ok": True, "groups": groups}


@app.post("/api/folder-organize/start")
def folder_organize_start():
    if APP.get("folder_merge_run") and APP["folder_merge_run"].get("running"):
        return JSONResponse({"ok": False, "error": "收藏夹合并已在运行"}, status_code=409)
    if any(APP.get(k) and APP[k].get("running") for k in ("scan_run", "apply_run")):
        return JSONResponse({"ok": False, "error": "扫描或内容执行正在运行，请稍后再合并"}, status_code=409)
    plan = store.load_folder_merge_plan()
    pending = [g for g in plan if g.get("status") not in ("done", "unknown")]
    if not pending:
        return JSONResponse({"ok": False, "error": "没有待执行的收藏夹合并方案"}, status_code=400)
    try:
        session = bili_api.BiliSession(get_session_cookie())
        cfg = load_config()
        session.read_interval = int(cfg.get("scan_interval", 2) or 2)
        session.write_interval = float(cfg.get("folder_merge_interval", 2) or 2)
        owner_mid = session.get_mid()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Cookie 不可用：{e}"}, status_code=400)
    state = APP["folder_merge_run"] = {"running": True, "stop": False, "done": 0,
                                       "total": len(pending), "error": None, "status": "running"}

    def save():
        store.save_folder_merge_plan(plan)
        store.save_folder_merge_state(dict(state))

    def chunks(items, size=1000):
        for i in range(0, len(items), size):
            yield items[i:i + size]

    def run():
        try:
            emit("info", f"开始收藏夹合并：{len(pending)} 组", phase="folder_merge")
            for group in pending:
                if state["stop"]: break
                group["status"] = "running"
                group["results"] = []
                group_ok = True
                live = {str(f["media_id"]): f for f in session.list_folders()}
                target_id = str(group["target_id"])
                if target_id not in live:
                    group.update(status="failed", error="目标收藏夹不存在")
                    state["done"] += 1; save(); continue
                for source_id in group["source_ids"]:
                    if state["stop"]: break
                    live = {str(f["media_id"]): f for f in session.list_folders()}
                    source = live.get(str(source_id))
                    result = {"source_id": str(source_id), "status": "running"}
                    group["results"].append(result)
                    if not source:
                        result["status"] = "already_absent"; save(); continue
                    count = int(source.get("count", 0) or 0)
                    videos = list(session.iter_folder_videos(str(source_id), count,
                                  should_stop=lambda: state["stop"])) if count else []
                    normal = [int(v.get("aid", 0) or 0) for v in videos
                              if int(v.get("aid", 0) or 0) > 0 and not _is_invalid(v)]
                    invalid = [int(v.get("aid", 0) or 0) for v in videos
                               if int(v.get("aid", 0) or 0) > 0 and _is_invalid(v)]
                    hidden_invalid = max(0, count - len(videos))
                    result.update(before=count, movable=len(normal), invalid=len(invalid) + hidden_invalid)
                    try:
                        for batch in chunks(normal):
                            session.move_batch(str(source_id), target_id, batch, mid=owner_mid,
                                               should_stop=lambda: state["stop"])
                        for batch in chunks(invalid):
                            session.batch_delete(str(source_id), batch,
                                                 should_stop=lambda: state["stop"])
                        if hidden_invalid:
                            session.clean_invalid_folder(str(source_id),
                                                         should_stop=lambda: state["stop"])
                        # B 站的收藏夹计数在 move 后可能短暂延迟，等待其收敛再决定是否删夹。
                        after = count
                        for attempt in range(6):
                            live_after = {str(f["media_id"]): f for f in session.list_folders()}
                            after = int((live_after.get(str(source_id)) or {}).get("count", 0) or 0)
                            if after == 0 or state["stop"]:
                                break
                            if attempt < 5:
                                time.sleep(2)
                        result["after"] = after
                        if after:
                            result.update(status="not_empty", error=f"仍有 {after} 条，未删除夹")
                            group_ok = False
                        elif group.get("delete_sources", True):
                            session.delete_folder(str(source_id), should_stop=lambda: state["stop"])
                            result["status"] = "merged_and_deleted"
                        else:
                            result["status"] = "merged_kept"
                    except (bili_api.WriteUncertainError, bili_api.RateLimitedError):
                        raise
                    except bili_api.BiliApiError as e:
                        result.update(status="failed", error=str(e)); group_ok = False
                    save()
                if state["stop"]: break
                if group_ok:
                    final_name = (group.get("final_name") or "").strip()
                    current = {str(f["media_id"]): f for f in session.list_folders()}.get(target_id)
                    if current and final_name and current.get("title") != final_name:
                        session.rename_folder(target_id, final_name, should_stop=lambda: state["stop"])
                    group["status"] = "done"
                else:
                    group["status"] = "partial"
                state["done"] += 1
                emit("ok" if group_ok else "warn",
                     f"合并组完成：{group.get('final_name')}", phase="folder_merge")
                save()
            state["status"] = "stopped" if state["stop"] else "done"
        except bili_api.WriteUncertainError as e:
            state.update(status="unknown", error=str(e), stop=True)
            for g in plan:
                if g.get("status") == "running": g["status"] = "unknown"
            emit("err", f"合并结果不确定，已停止：{e}", phase="folder_merge")
        except bili_api.RateLimitedError as e:
            state.update(status="rate_limited", error=str(e), stop=True)
            emit("err", f"触发风控，已停止：{e}", phase="folder_merge")
        except Exception as e:
            state.update(status="error", error=str(e), stop=True)
            emit("err", f"收藏夹合并异常：{e}", phase="folder_merge")
        finally:
            state["running"] = False
            save()
            try:
                folders = session.list_folders(); store.save_folders(folders); APP["folders"] = folders
            except Exception: pass
            emit("ok" if state["status"] == "done" else "warn", "收藏夹合并任务已结束",
                 phase="folder_merge", kind="folder_merge_end")

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "started": True, "total": len(pending)}


@app.post("/api/folder-organize/stop")
def folder_organize_stop():
    state = APP.get("folder_merge_run")
    if state and state.get("running"):
        state["stop"] = True
        return {"ok": True}
    return {"ok": False, "error": "没有运行中的收藏夹合并"}


# ============ 连接测试 ============
@app.post("/api/test/cookie")
def test_cookie():
    """测试浏览器 cookie 读取 + B站登录态(nav)。"""
    try:
        ci = get_session_cookie()
        s = bili_api.BiliSession(ci)
        mid = s.get_mid()
        if not mid:
            return {"ok": False, "error": "cookie 读到了，但 B站未返回 mid（可能未登录）"}
        return {"ok": True, "mid": mid, "message": f"Cookie 可用，已登录 mid={mid}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/test/llm")
def test_llm():
    """测试 LLM 接口连通（发一条最小请求）。"""
    cfg = load_config()
    if not cfg.get("base_url") or not cfg.get("model"):
        return {"ok": False, "error": "请先填写 base_url 和 模型名"}
    try:
        import requests as _rq
        url = cfg["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if cfg.get("api_key"):
            headers["Authorization"] = "Bearer " + cfg["api_key"]
        payload = {"model": cfg["model"],
                   "messages": [{"role": "user", "content": "ping"}],
                   "max_tokens": 5}
        r = _rq.post(url, json=payload, headers=headers, timeout=30)
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return {"ok": True, "message": f"模型连接成功（{cfg['model']}）"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============ 扫码登录获取 Cookie ============
LOGIN = {"session": None, "qrcode_key": ""}
PASSPORT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


@app.post("/api/login/qr/generate")
def login_qr_generate():
    """生成 B站登录二维码（返回 base64 图片 + qrcode_key）。"""
    try:
        import base64
        import io
        import qrcode

        s = requests.Session()
        s.headers.update({"User-Agent": PASSPORT_UA, "Referer": "https://www.bilibili.com/"})
        r = s.get("https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
                  timeout=15)
        d = r.json()
        if d.get("code") != 0:
            return {"ok": False, "error": f"生成二维码失败：{d.get('message')}"}
        data = d["data"]
        LOGIN["session"] = s
        LOGIN["qrcode_key"] = data["qrcode_key"]

        img = qrcode.make(data["url"])
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        return {"ok": True, "image": "data:image/png;base64," + b64,
                "key": data["qrcode_key"]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/login/qr/poll")
def login_qr_poll():
    """轮询扫码状态；成功后解析并保存 cookie。"""
    s = LOGIN.get("session")
    if not s:
        return {"ok": False, "status": "error", "message": "请先生成二维码"}
    try:
        import urllib.parse
        r = s.get("https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                  params={"qrcode_key": LOGIN.get("qrcode_key", "")}, timeout=15)
        d = r.json()
        data = d.get("data") or {}
        code = data.get("code")
        if code == 0:
            url = data.get("url", "")
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            sessdata = (q.get("SESSDATA") or [""])[0] or s.cookies.get("SESSDATA", "")
            jct = (q.get("bili_jct") or [""])[0] or s.cookies.get("bili_jct", "")
            uid = (q.get("DedeUserID") or [""])[0] or s.cookies.get("DedeUserID", "")
            if sessdata and jct and uid:
                cur = load_config()
                cur["cookie_string"] = f"SESSDATA={sessdata}; bili_jct={jct}; DedeUserID={uid}"
                save_config(cur)
                LOGIN["session"] = None
                return {"ok": True, "status": "ok", "message": "登录成功，Cookie 已保存"}
            return {"ok": False, "status": "error", "message": "登录成功但未解析到 cookie"}
        mapping = {86101: ("waiting", "未扫码"),
                   86090: ("scanned", "已扫码，请在手机上确认"),
                   86038: ("expired", "二维码已过期，请刷新")}
        st, msg = mapping.get(code, ("unknown", f"未知状态 {code}"))
        return {"ok": True, "status": st, "message": msg}
    except Exception as e:
        return {"ok": False, "status": "error", "message": str(e)}


@app.get("/api/login/status")
def login_status():
    cfg = load_config()
    return {"configured": bool((cfg.get("cookie_string") or "").strip())}


# ============ 阶段2: LLM 分析 ============
class AnalyzeIn(BaseModel):
    continuous: bool = False
    folder_ids: Optional[list[str]] = None


class AnalyzeContinuousIn(BaseModel):
    enabled: bool


def _make_analyzer(folders: list[str]) -> llm_analyzer.LLMAnalyzer:
    cfg = load_config()
    return llm_analyzer.LLMAnalyzer(llm_analyzer.LLMConfig(
        base_url=cfg.get("base_url", ""), api_key=cfg.get("api_key", ""),
        model=cfg.get("model", "qwen3-8b"),
        max_tokens=int(cfg.get("analyze_max_tokens", 8192) or 8192)), folders=folders)


@app.post("/api/analyze/start")
def analyze_start(body: Optional[AnalyzeIn] = None):
    if APP["analyze_run"] and APP["analyze_run"].get("running"):
        return JSONResponse({"ok": False, "error": "分析已在运行中"}, status_code=400)
    body = body or AnalyzeIn()
    selected = {str(x) for x in (body.folder_ids or store.load_scan_selection())}
    def candidates():
        rows = store.load_videos()
        return [v for v in rows if not selected or
                str(v.get("source_folder_id", "")) in selected or
                bool(selected.intersection(str(x) for x in v.get("folder_ids", [])))]
    videos = candidates()
    scan_running_at_start = bool(APP.get("scan_run") and APP["scan_run"].get("running"))
    if not videos and not (body.continuous and scan_running_at_start):
        return JSONResponse({"ok": False, "error": "尚无已扫描数据，请先执行扫描"}, status_code=400)

    folders_titles = [f["title"] for f in store.load_folders()]
    analyzer = _make_analyzer(folders_titles)
    if not analyzer.config.configured:
        return JSONResponse({"ok": False, "error": "LLM 未配置，请先填写 base_url 和 model"}, status_code=400)

    cfg = load_config()
    concurrency = max(1, min(4, int(cfg.get("analyze_concurrency", 1) or 1)))
    batch_size = int(cfg.get("analyze_batch", 20) or 20)
    if batch_size < 1:
        batch_size = 1
    state = APP["analyze_run"] = {"running": True, "done": 0, "total": len(videos),
                                  "failed": 0, "stop": False, "error": None,
                                  "concurrency": concurrency, "batch": batch_size,
                                  "continuous": bool(body.continuous), "waiting": False,
                                  "round": 0, "selected_ids": sorted(selected),
                                  "inflight": 0}

    def run():
        from concurrent.futures import ThreadPoolExecutor
        lock = threading.Lock()
        fail_streak = [0]
        failed_session: set[str] = set()

        def work(batch, ctx):
            """处理一个批次（一次请求多条）。"""
            if state["stop"]:
                return
            with lock:
                state["inflight"] += len(batch)
                inflight = state["inflight"]
                current_done = state["done"]
                current_total = state["total"]
                current_failed = state["failed"]
            # 请求已交给模型：立即推送浅色「等待响应」进度。
            emit("progress", "", phase="analyze", kind="analyze_progress",
                 done=current_done, total=current_total, failed=current_failed,
                 inflight=inflight)
            err = ""
            try:
                got = analyzer.analyze_batch(batch)
            except Exception as e:
                got = {}
                err = f"{type(e).__name__}: {e}"
            okn = 0
            with lock:
                for bvid, res in (got or {}).items():
                    if res:
                        store.save_analysis({bvid: res})
                        okn += 1
                state["failed"] += (len(batch) - okn)
                state["inflight"] = max(0, state["inflight"] - len(batch))
                ctx["done"] += len(batch)
                state["done"] = ctx["done"]
                d, f, inflight = ctx["done"], state["failed"], state["inflight"]
                if len(batch) and okn == 0:
                    fail_streak[0] += 1
                else:
                    fail_streak[0] = 0
                streak = fail_streak[0]
                if err:
                    state["error"] = state.get("error") or err
            # 静默进度事件：前端只推进度条，不写日志
            emit("progress", "", phase="analyze", kind="analyze_progress",
                 done=d, total=state["total"], failed=f, inflight=inflight)
            emit("ok" if okn == len(batch) else "warn",
                 f"批完成 +{okn}/{len(batch)} · 累计 {d}/{len(videos)}（失败 {f}）",
                 phase="analyze", done=d, total=state["total"], inflight=inflight)
            if err:
                emit("err", f"本批异常（{len(batch)} 条均失败）：{err}", phase="analyze")
            # 连续多批全失败 → 疑似配置/额度问题，自动停止
            if streak >= 5 and not state["stop"]:
                state["stop"] = True
                emit("err", f"连续 {streak} 批全部失败，已自动停止分析；"
                            f"请检查模型配置、key 或额度", phase="analyze", kind="analyze_end")

        try:
            ctx = {"done": 0}
            emit("info", f"开始分析，批大小 {batch_size} × 并发 {concurrency}"
                 + ("，已启用连续分析" if state["continuous"] else ""),
                 phase="analyze", kind="analyze_start", done=0, total=len(videos), failed=0,
                 inflight=0)
            while not state["stop"]:
                current = candidates()
                existing = store.load_analysis_raw()
                pending = [v for v in current if v.get("bvid")
                           and v["bvid"] not in existing
                           and v["bvid"] not in failed_session
                           and not _is_invalid(v)]
                state["total"] = len(current)
                state["done"] = len([v for v in current if _is_invalid(v) or v.get("bvid") in existing])
                ctx["done"] = state["done"]
                if pending:
                    state["waiting"] = False
                    state["round"] += 1
                    batches = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
                    before = set(existing)
                    if concurrency <= 1:
                        for b in batches:
                            if state["stop"]: break
                            work(b, ctx)
                    else:
                        with ThreadPoolExecutor(max_workers=concurrency) as ex:
                            list(ex.map(lambda b: work(b, ctx), batches))
                    after = store.load_analysis_raw()
                    failed_session.update(v["bvid"] for v in pending if v["bvid"] not in after and v["bvid"] not in before)
                    continue
                scan_running = bool(APP.get("scan_run") and APP["scan_run"].get("running"))
                if not state["continuous"] or not scan_running:
                    break
                state["waiting"] = True
                emit("progress", "", phase="analyze", kind="analyze_progress",
                     done=state["done"], total=state["total"], failed=state["failed"],
                     inflight=state["inflight"], waiting=True)
                ANALYZE_WAKE.clear()
                ANALYZE_WAKE.wait(3)

            if state["stop"]:
                emit("warn", "已停止分析（进度已保存）", phase="analyze",
                     kind="analyze_end", done=ctx["done"], total=state["total"])
            else:
                emit("ok" if not state["failed"] else "warn",
                     f"分析结束：完成 {ctx['done']}，失败 {state['failed']}",
                     phase="analyze", kind="analyze_end",
                     done=ctx["done"], total=state["total"])
        except Exception as e:
            state["error"] = str(e)
            emit("err", f"分析出错：{e}", phase="analyze", kind="analyze_end")
        finally:
            state["running"] = False
            state["stop"] = False
            state["continuous"] = False
            state["waiting"] = False

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "started": True, "total": len(videos)}


@app.get("/api/analyze/status")
def analyze_status():
    return APP["analyze_run"] or {"running": False, "done": 0, "total": 0,
                                  "failed": 0, "stop": False, "error": None}


@app.post("/api/analyze/stop")
def analyze_stop():
    if APP["analyze_run"]:
        APP["analyze_run"]["stop"] = True
        ANALYZE_WAKE.set()
        return {"ok": True}
    return {"ok": False, "error": "没有运行中的分析"}


@app.post("/api/analyze/continuous")
def analyze_continuous(body: AnalyzeContinuousIn):
    state = APP.get("analyze_run")
    if not state or not state.get("running"):
        return {"ok": True, "enabled": False}
    state["continuous"] = bool(body.enabled)
    ANALYZE_WAKE.set()
    return {"ok": True, "enabled": state["continuous"]}


# ============ 阶段3: 预归类方案 ============
@app.get("/api/plan")
def get_plan():
    """返回分析结果 + 现有收藏夹，供前端展示归类方案。"""
    analysis = store.load_analysis()
    folders = store.load_folders()
    videos = {v["bvid"]: v for v in store.load_videos()}
    invalid = [v["bvid"] for v in videos.values() if _is_invalid(v)]
    return {
        "analysis": analysis,
        "existing_folders": folders,
        "videos_by_bvid": videos,
        "invalid_count": len(invalid),
        "invalid_bvids": invalid,
        "plan": store.load_plan_raw(),      # 含 status: pending/done/failed
    }


@app.post("/api/plan/mark_invalid")
def plan_mark_invalid():
    """把所有标题恰为「已失效视频」的视频标记为删除（写入 plan.json，等执行）。

    防火墙：只挑选标题**恰好**等于「已失效视频」的条目。
    """
    videos = store.load_videos()
    inv = [v for v in videos if _is_invalid(v)]
    if not inv:
        return {"ok": True, "count": 0, "message": "没有发现失效视频"}
    plan = {}
    for v in inv:
        plan[v["bvid"]] = {"bvid": v["bvid"], "action": "delete_invalid",
                           "target_folder": "", "create_new_name": ""}
    store.save_plan(plan)
    emit("warn", f"已把 {len(inv)} 条失效视频标记为删除（到阶段❹点「开始执行」生效）")
    return {"ok": True, "count": len(inv)}


class PlanReview(BaseModel):
    apply_list: list  # [{bvid, action, target_folder, create_new_name?}]


@app.post("/api/plan/apply")
def apply_plan(body: PlanReview):
    """把前端确认后的归类方案存入 plan.json，作为执行阶段的输入。

    关键：**保留已存在的 done 状态**，否则已执行过的条目会被重置为待办 → 重复执行。
    """
    old = store.load_plan_raw()
    plan = {}
    kept_done = 0
    for item in body.apply_list:
        bvid = item.get("bvid", "")
        if not bvid:
            continue
        entry = {
            "bvid": bvid,
            "action": item.get("action", "skip"),
            "target_folder": item.get("target_folder", ""),
            "create_new_name": item.get("create_new_name", ""),
            "status": "pending",
        }
        prev = old.get(bvid) or {}
        if prev.get("status") == "done":
            entry["status"] = "done"
            entry["result"] = prev.get("result", "")
            entry["at"] = prev.get("at", "")
            kept_done += 1
        plan[bvid] = entry
    # 保留 plan 中原本存在、但本次未提交的条目（例如"标记失效视频"）
    for bvid, prev in old.items():
        plan.setdefault(bvid, prev)
    store.save_plan(plan)
    msg = f"已写入方案 {len(plan)} 条"
    if kept_done:
        msg += f"（其中 {kept_done} 条已完成状态已保留，不会重复执行）"
    emit("info", msg)
    return {"ok": True, "planned": len(plan), "kept_done": kept_done}


class PlanStatusIn(BaseModel):
    bvids: list = []          # 要处理的所有 bvid
    status: str = "pending"   # pending / done
    all: bool = False         # True 表示对 plan 中全部条目生效


@app.post("/api/plan/set_status")
def plan_set_status(body: PlanStatusIn):
    """把条目在「待操作 / 已完成」之间移动（例如把已完成的移回待操作）。"""
    st = body.status if body.status in ("pending", "done") else "pending"
    plan = store.load_plan_raw()
    targets = set(plan.keys()) if body.all else set(body.bvids or [])
    n = 0
    for bvid, item in plan.items():
        if bvid in targets:
            item["status"] = st
            if st == "pending":
                item["result"] = ""
            n += 1
    store.save_plan(plan)
    emit("info", f"已把 {n} 条改为「{'待操作' if st == 'pending' else '已完成'}」")
    return {"ok": True, "count": n, "status": st}


class PlanRemoveIn(BaseModel):
    bvids: list = []


@app.post("/api/plan/remove")
def plan_remove(body: PlanRemoveIn):
    """把条目从方案中彻底移除（只删本地方案，不动 B站）。"""
    plan = store.load_plan_raw()
    targets = set(body.bvids or [])
    n = 0
    for b in list(plan.keys()):
        if b in targets:
            plan.pop(b, None)
            n += 1
    store.replace_plan(plan)
    emit("warn", f"已从方案中移除 {n} 条")
    return {"ok": True, "count": n}


# ============ 阶段4: 执行 ============
@app.post("/api/apply/start")
def apply_start():
    """批量执行 plan：失效视频 batch-del，其余视频 move。

    明确业务失败记录后继续；风控停止；超时/无响应标记 unknown 并停止，
    交由用户人工复核。
    """
    if APP["apply_run"] and APP["apply_run"].get("running"):
        return JSONResponse({"ok": False, "error": "执行已在运行中"}, status_code=400)
    plan = store.load_plan_raw()
    if not plan:
        return JSONResponse({"ok": False, "error": "没有可执行的方案，请先在阶段3确认"}, status_code=400)
    # 执行阶段按需自建会话（不依赖是否先扫描过）
    if APP["session"] is None:
        try:
            APP["session"] = bili_api.BiliSession(get_session_cookie())
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"Cookie 不可用：{e}"}, status_code=400)
    session = APP["session"]
    cfg = load_config()
    session.write_interval = float(cfg.get("write_interval", 2) or 2)
    batch_size = max(1, min(1000, int(cfg.get("apply_batch", 1000) or 1000)))
    videos = {v["bvid"]: v for v in store.load_videos()}

    # 迁移：把"上次执行断点"之前的条目补上已完成状态（早期版本没记录结果）
    prev = store.load_apply_state()
    done_idx = int(prev.get("last_ok", 0) or 0)
    migrated = 0
    if done_idx:
        for i, (_b, it) in enumerate(plan.items()):
            if i < done_idx and it.get("status") != "done":
                it["status"] = "done"
                it["result"] = it.get("result") or "（历史执行，按断点视为已完成）"
                it["at"] = it.get("at") or ""
                migrated += 1
        if migrated:
            store.save_plan(plan)
        store.save_apply_state({})   # 迁移只做一次；之后一律以 status 为准

    # unknown 必须人工复核后移回 pending，不自动重发。
    pending = [(b, it) for b, it in plan.items()
               if it.get("status") not in ("done", "unknown")]
    total = len(pending)
    already_done = len([1 for it in plan.values() if it.get("status") == "done"])
    held_unknown = len([1 for it in plan.values() if it.get("status") == "unknown"])

    state = APP["apply_run"] = {"running": True, "done": 0, "total": total,
                                "ok": 0, "failed": 0, "skip": 0, "deleted": 0,
                                "unknown": 0, "stop": False, "error": None, "log": [],
                                "batch_size": batch_size, "batch_done": 0, "batch_total": 0}

    def _mark(item, status: str, result: str):
        """写入一条方案的执行结果。"""
        item["status"] = status
        item["result"] = result
        item["at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def chunks(items):
        for i in range(0, len(items), batch_size):
            yield items[i:i + batch_size]

    def run():
        def save_progress():
            store.save_plan(plan)
            store.save_videos(videos)

        def finish_items(entries, status, message, *, target_id=""):
            for bvid, item, video in entries:
                _mark(item, status, message)
                if status == "done" and target_id:
                    video["source_folder_id"] = str(target_id)
                state["done"] += 1
            n = len(entries)
            if status == "done":
                state["ok"] += n
            elif status == "unknown":
                state["unknown"] += n
            elif status == "failed":
                state["failed"] += n
            save_progress()
            emit("progress", "", phase="apply", kind="apply_progress",
                 done=state["done"], total=total)

        def fail_entries(entries, message):
            finish_items(entries, "failed", message)
            emit("warn", f"{message}（{len(entries)} 条）", phase="apply")

        def run_batch(kind, src, target_id, target_name, entries):
            if state["stop"]:
                return False
            aids = [int(video.get("aid", 0) or 0) for _, _, video in entries]
            state["batch_done"] += 1
            bn = state["batch_done"]
            bt = state["batch_total"]
            label = "删除失效" if kind == "delete" else f"移入「{target_name}」"
            emit("info", f"批次 {bn}/{bt}：{label} {len(entries)} 条",
                 phase="apply", done=state["done"], total=total)
            try:
                if kind == "delete":
                    session.batch_delete(src, aids, should_stop=lambda: state["stop"])
                    msg = f"批量删除失效视频成功（批次 {bn}）"
                    finish_items(entries, "done", msg)
                    state["deleted"] += len(entries)
                else:
                    session.move_batch(src, target_id, aids, mid=owner_mid,
                                       should_stop=lambda: state["stop"])
                    msg = f"批量移入「{target_name}」成功（批次 {bn}）"
                    finish_items(entries, "done", msg, target_id=target_id)
                emit("ok", f"批次 {bn}/{bt} 完成：{len(entries)} 条",
                     phase="apply", done=state["done"], total=total)
                return True
            except bili_api.WriteUncertainError as e:
                msg = f"批次 {bn} 结果不确定：{e}"
                finish_items(entries, "unknown", msg)
                state["error"] = msg + "。已停止，请人工复核后再继续。"
                state["stop"] = True
                emit("err", state["error"], phase="apply", kind="apply_end",
                     done=state["done"], total=total)
                return False
            except bili_api.RateLimitedError as e:
                state["error"] = ("已手动停止，本批保留待处理。" if state["stop"] else
                                  f"风控触发：{e}。已停止，本批保留待处理。")
                state["stop"] = True
                emit("err", state["error"], phase="apply", kind="apply_end",
                     done=state["done"], total=total)
                return False
            except bili_api.BiliApiError as e:
                # 明确业务失败：记账后继续后续批次。
                fail_entries(entries, f"批次 {bn} 失败：{e}")
                return True
            except Exception as e:
                fail_entries(entries, f"批次 {bn} 异常：{e}")
                return True

        try:
            if migrated:
                emit("info", f"已把历史断点前 {migrated} 条补标为已完成")
            emit("info", f"开始批量执行：待操作 {total} 条，单批上限 {batch_size}"
                         f"（已完成 {already_done}，待人工复核 {held_unknown}）",
                 phase="apply", kind="apply_start", done=0, total=total)

            # 每次执行都实时刷新收藏夹映射，不使用过期 folders.json。
            owner_mid = session.get_mid()
            live_folders = session.list_folders()
            name_to_id = {f["title"]: str(f["media_id"]) for f in live_folders}

            delete_groups = {}
            move_groups = {}
            new_groups = {}

            # 先处理无需发请的条目，再分组。
            for bvid, item in pending:
                video = videos.get(bvid, {})
                aid = int(video.get("aid", 0) or 0)
                action = item.get("action", "skip")
                src = str(item.get("source_folder_id") or video.get("source_folder_id") or "")
                entry = (bvid, item, video)
                if action in ("skip", ""):
                    _mark(item, "done", f"{bvid} 已按方案跳过")
                    state["done"] += 1
                    state["skip"] += 1
                elif not aid:
                    fail_entries([entry], f"{bvid} 无有效 aid")
                elif not src:
                    fail_entries([entry], f"{bvid} 来源收藏夹未知")
                elif action == "delete_invalid":
                    if not _is_invalid(video):
                        fail_entries([entry], f"{bvid} 非失效视频，拒绝删除")
                    else:
                        delete_groups.setdefault(src, []).append(entry)
                elif action == "create_new":
                    name = (item.get("create_new_name") or item.get("target_folder") or "").strip()
                    if not name:
                        fail_entries([entry], f"{bvid} 新收藏夹名为空")
                    else:
                        new_groups.setdefault(name, []).append((src, entry))
                else:
                    target_name = (item.get("target_folder") or "").strip()
                    target_id = name_to_id.get(target_name, "")
                    if not target_id:
                        fail_entries([entry], f"{bvid} 目标收藏夹「{target_name}」不存在")
                    elif src == target_id:
                        _mark(item, "done", f"{bvid} 已在「{target_name}」，无需移动")
                        state["done"] += 1
                        state["skip"] += 1
                    else:
                        move_groups.setdefault((src, target_id, target_name), []).append(entry)

            save_progress()

            # 实际请求数：删除 -> 移动到现有夹 -> 新建夹并移动。
            def batch_count(groups):
                return sum((len(items) + batch_size - 1) // batch_size
                           for items in groups.values())
            new_batch_total = 0
            for grouped in new_groups.values():
                by_src_count = {}
                for src, entry in grouped:
                    by_src_count[src] = by_src_count.get(src, 0) + 1
                new_batch_total += sum((n + batch_size - 1) // batch_size
                                       for n in by_src_count.values())
            state["batch_total"] = (batch_count(delete_groups) + batch_count(move_groups)
                                    + new_batch_total)

            for src, entries in delete_groups.items():
                for batch in chunks(entries):
                    if not run_batch("delete", src, "", "", batch):
                        return

            for (src, target_id, target_name), entries in move_groups.items():
                for batch in chunks(entries):
                    if not run_batch("move", src, target_id, target_name, batch):
                        return

            for name, grouped in new_groups.items():
                if state["stop"]:
                    return
                target_id = name_to_id.get(name, "")
                entries = [entry for _, entry in grouped]
                if not target_id:
                    try:
                        target_id = session.create_folder(name)
                        if not target_id:
                            raise bili_api.BiliApiError("未返回收藏夹 id")
                        name_to_id[name] = target_id
                        emit("ok", f"已新建收藏夹「{name}」", phase="apply")
                    except bili_api.RateLimitedError as e:
                        state["error"] = f"新建「{name}」时触发风控：{e}。已停止。"
                        state["stop"] = True
                        emit("err", state["error"], phase="apply", kind="apply_end",
                             done=state["done"], total=total)
                        return
                    except Exception as e:
                        fail_entries(entries, f"新建收藏夹「{name}」失败：{e}")
                        continue
                # 同一新夹的条目仍需按来源夹分组。
                by_src = {}
                for src, entry in grouped:
                    if src == str(target_id):
                        finish_items([entry], "done", f"已在「{name}」，无需移动",
                                     target_id=target_id)
                        state["skip"] += 1
                    else:
                        by_src.setdefault(src, []).append(entry)
                for src, src_entries in by_src.items():
                    for batch in chunks(src_entries):
                        if not run_batch("move", src, target_id, name, batch):
                            return

            level = "ok" if not state["failed"] else "warn"
            emit(level, f"批量执行结束：成功 {state['ok']}"
                        f"（其中删除失效 {state['deleted']}），"
                        f"失败 {state['failed']}，跳过 {state['skip']}",
                 phase="apply", kind="apply_end", done=state["done"], total=total)
        except bili_api.RateLimitedError as e:
            state["error"] = f"风控触发：{e}。已停止。"
            emit("err", state["error"], phase="apply", kind="apply_end",
                 done=state["done"], total=total)
        except Exception as e:
            state["error"] = f"执行初始化失败：{e}"
            emit("err", state["error"], phase="apply", kind="apply_end",
                 done=state["done"], total=total)
        finally:
            save_progress()
            state["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "started": True, "total": total, "batch_size": batch_size,
            "held_unknown": held_unknown}


@app.get("/api/apply/status")
def apply_status():
    return APP["apply_run"] or {"running": False, "done": 0, "total": 0,
                                "ok": 0, "failed": 0, "skip": 0, "unknown": 0,
                                "error": None,
                                "log": []}


@app.post("/api/apply/stop")
def apply_stop():
    if APP["apply_run"]:
        APP["apply_run"]["stop"] = True
        return {"ok": True}
    return {"ok": False, "error": "没有运行中的执行"}


# ============ 全局状态 ============
@app.get("/api/status")
def status():
    return {
        "stats": store.stats(),
        "scan": APP["scan_run"],
        "analyze": APP["analyze_run"],
        "apply": APP["apply_run"],
    }


# ============ 静态页面 ============
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


def main():
    parser = argparse.ArgumentParser(description="B站收藏夹整理")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--log-file", default="server.log",
                        help="服务日志文件（默认 server.log，可指定其它名字）")
    args = parser.parse_args()

    log_path = Path(args.log_file)
    if not log_path.is_absolute():
        log_path = HERE / log_path

    # 先把 uvicorn 的日志配置建好，再建自己的 handler：
    # uvicorn.Config 内部会 dictConfig，而 dictConfig 会关掉此前创建的所有 handler
    # （stream 被置空、再写入就无效），所以自己的 handler 只能在它之后创建。
    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="info")

    # 服务自己的日志（logger 名 server）：dictConfig 之后 root 已没有可用 handler，这里补回控制台
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(console)

    # 控制台保持 uvicorn 原生格式（带配色），文件里另存一份纯文本
    try:
        fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
        fh.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(fh)
        # uvicorn 的日志分成两个 logger，各自的 handler 要单独挂
        for name in ("uvicorn", "uvicorn.access"):
            logging.getLogger(name).addHandler(fh)
        log.info("日志文件：%s", log_path)
    except Exception as e:
        log.warning("无法写入日志文件 %s：%s", log_path, e)

    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
