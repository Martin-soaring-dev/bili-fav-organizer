# -*- coding: utf-8 -*-
"""B 站收藏夹 API 封装。

职责：
  - 从浏览器自动读取登录 cookie（browsercookie）
  - 获取当前用户信息、收藏夹列表、收藏夹内的视频列表
  - 创建收藏夹、把视频添加到收藏夹、从收藏夹删除
  - 内置限速、失败重试、风控检测

设计原则：
  - 读操作(cookie/列表)高频重试，写操作(加/删)严格限速+断点由调用方控制
  - 所有返回统一成 dict，含 ok/code/msg/data，便于上层判断
"""
from __future__ import annotations

import json
import hashlib
import logging
import random
import time
import urllib.parse
from dataclasses import dataclass, field, asdict
from pathlib import Path

import requests

log = logging.getLogger("bili_api")

REQUEST_TIMEOUT = 15          # 单次请求超时(秒)
BATCH_WRITE_TIMEOUT = 30      # 批量写请求超时；超时后远端结果不确定
BATCH_LIMIT = 1000            # 实测硬上限：1000 成功，1001 返回 -400
PAGE_SIZE = 20                # 收藏夹视频每页条数
FAV_BULK_CANDIDATE_LIMIT = 1000  # resource/ids 历史实测可能在 1000 条截断，仅作为尝试阈值
MAX_SCAN_PAGES = 50000        # 防止异常 has_more 响应导致无限请求
READ_INTERVAL = 10            # 相邻两次收藏夹请求的最小间隔(秒)，全局生效
PAGE_INTERVAL = READ_INTERVAL  # 兼容旧名
HTTP_412_BACKOFF = 300        # 遇 HTTP 412 的退避等待(秒)
RETRY_READ = 4                # 读操作重试次数
MIN_INTERVAL_WRITE = 1.0      # 写操作最小间隔(秒) —— 旧常量，保留兜底
MAX_INTERVAL_WRITE = 3.0      # 写操作最大间隔(秒) —— 旧常量，保留兜底
WRITE_INTERVAL = 2.0          # 写操作基准间隔(秒)：距上次写操作至少这么久，可由 server 调整
WRITE_JITTER = 1.5            # 写操作随机抖动上限(秒)：避免完全等间隔被识别为脚本
WRITE_LOG_PATH = None         # 由 server 设置，用于写操作断点记录
EVENT_HOOK = None             # 由 server 注入：fn(level, text)，用于把长等待提示推到前端


def _notify(level: str, text: str):
    """把一条提示通过注入的钩子发给前端事件流（未注入时静默）。"""
    try:
        if EVENT_HOOK:
            EVENT_HOOK(level, text)
    except Exception:
        pass

# B 站风控/错误的常见 code
CODE_OK = 0
CODE_NOT_LOGIN = -101         # 未登录
CODE_TOKEN_EXPIRED = -658     # 登录过期
CODE_FAV_LIMITED = -403       # 访问权限/风控（可能是"需要登录"或"触发风控"）


class BiliApiError(RuntimeError):
    """B 站 API 返回非零 code 或被判定为风控时抛出。"""


class RateLimitedError(BiliApiError):
    """被风控/限流。调用方应暂停并提示用户。"""


class WriteUncertainError(BiliApiError):
    """写请求已发出，但未拿到可确认的响应；不应自动重试。"""


@dataclass
class CookieInfo:
    """从浏览器提取的登录 cookie。仅保存必要字段。"""
    sessdata: str
    bili_jct: str
    userid: str
    dedeuserid: str = ""
    csrf_from = "cookie"       # 固定

    @property
    def has_essential(self) -> bool:
        return bool(self.sessdata and self.bili_jct and self.userid)


def _contains_unsafe(s: str) -> bool:
    """判断字符串是否含 WBI 签名需过滤的字符。"""
    for ch in "!'()*":
        if ch in s:
            return True
    return False


def _find_cookie(browser_cookiejar, name: str) -> str:
    """从 cookiejar 里按名字取 cookie 值。"""
    for c in browser_cookiejar:
        if c.name == name:
            return c.value
    return ""


def parse_cookie_string(cookie_str: str) -> CookieInfo:
    """解析手动粘贴的 cookie 字符串（形如 'k1=v1; k2=v2'），提取登录字段。"""
    fields = {}
    for part in (cookie_str or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        fields[k.strip()] = v.strip()
    ci = CookieInfo(
        sessdata=fields.get("SESSDATA", ""),
        bili_jct=fields.get("bili_jct", ""),
        userid=fields.get("DedeUserID", ""),
        dedeuserid=fields.get("DedeUserID", ""),
    )
    return ci


def load_cookie_from_string(cookie_str: str) -> CookieInfo:
    """从手动粘贴的 cookie 字符串加载登录信息，校验必需字段。"""
    ci = parse_cookie_string(cookie_str)
    if not ci.has_essential:
        missing = []
        if not ci.sessdata:
            missing.append("SESSDATA")
        if not ci.bili_jct:
            missing.append("bili_jct")
        if not ci.userid:
            missing.append("DedeUserID")
        raise BiliApiError(
            "粘贴的 Cookie 缺少必需字段：" + "、".join(missing) + "。\n"
            "请从浏览器 F12 → Application/应用 → Cookies → https://www.bilibili.com 复制完整 Cookie。"
        )
    return ci


def make_cookie_header(ci: CookieInfo) -> dict:
    """构造请求头里的 Cookie 字符串。"""
    parts = []
    if ci.sessdata:
        parts.append(f"SESSDATA={ci.sessdata}")
    if ci.bili_jct:
        parts.append(f"bili_jct={ci.bili_jct}")
    if ci.dedeuserid:
        parts.append(f"DedeUserID={ci.dedeuserid}")
    return {"Cookie": "; ".join(parts)}


def _browser_running(browser: str) -> bool:
    """检测指定浏览器进程是否在运行（简单按进程名判断）。"""
    import subprocess
    proc_map = {
        "chrome": "chrome.exe",
        "edge": "msedge.exe",
        "firefox": "firefox.exe",
    }
    exe = proc_map.get(browser)
    if not exe:
        return False
    try:
        out = subprocess.run(["tasklist", "/fi", f"imagename eq {exe}"],
                             capture_output=True, text=True, timeout=10).stdout
        return exe.lower() in out.lower()
    except Exception:
        return False


def load_browser_cookie(browser: str = "chrome", manual_cookie: str = "") -> CookieInfo:
    """获取 B 站登录信息。

    优先使用手动粘贴的 cookie（manual_cookie），为空时才尝试从浏览器自动读取。
    注意：新版 Chrome(153+) 启用 App-Bound 加密(v20)，cookie 无法被脚本解密，
    此时必须使用手动粘贴的 cookie。
    """
    # 优先：手动粘贴的 cookie
    if manual_cookie and manual_cookie.strip():
        return load_cookie_from_string(manual_cookie)

    try:
        import browsercookie
    except Exception as e:  # pragma: no cover
        raise BiliApiError(f"无法导入 browsercookie 库：{e}。请先 pip install browsercookie") from e

    loader = None
    if browser == "chrome":
        loader = lambda: browsercookie.chrome()
    elif browser == "edge":
        loader = lambda: browsercookie.edge()
    elif browser == "firefox":
        loader = lambda: browsercookie.firefox()
    else:
        loader = lambda: browsercookie.chrome()

    # 先检测浏览器是否在运行；运行中一定读不了 cookie 文件
    if _browser_running(browser):
        raise BiliApiError(
            f"检测到 {browser} 浏览器正在运行，无法读取其登录 cookie（Windows 会锁定 cookie 文件）。\n"
            "请先【完全关闭】该浏览器（包括后台进程），再重新扫描。\n"
            "扫描完成后可以重新打开浏览器查看结果。"
        )

    try:
        jar = loader()
    except Exception as e:  # pragma: no cover
        raise BiliApiError(
            f"读取 {browser} 的 cookie 失败：{e}\n"
            "【常见原因】新版 Chrome(153+) 启用了 App-Bound 加密(v20)，脚本无法解密其 cookie。\n"
            "【解决办法】请在配置区手动粘贴 B 站 Cookie（见界面指引），即可绕开浏览器加密。"
        ) from e

    ci = CookieInfo(
        sessdata=_find_cookie(jar, "SESSDATA"),
        bili_jct=_find_cookie(jar, "bili_jct"),
        userid=_find_cookie(jar, "DedeUserID"),
        dedeuserid=_find_cookie(jar, "DedeUserID"),
    )
    if not ci.has_essential:
        raise BiliApiError(
            "从浏览器 cookie 中未提取到完整的 B 站登录信息（缺 SESSDATA / bili_jct）。\n"
            "请确认该浏览器已登录 bilibili.com（登录后需保持一段时间让 cookie 写入），然后重试。"
        )
    return ci


class BiliSession:
    """持有 cookie 的 B 站会话，封装常用收藏 API。"""

    def __init__(self, cookie: CookieInfo):
        self.cookie = cookie
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
        })
        self.session.headers.update(make_cookie_header(cookie))
        self._wapi = "https://api.bilibili.com"
        self._last_write_at = 0.0
        self.write_interval = WRITE_INTERVAL   # 写操作基准间隔(秒)，可由 server 调整
        self._last_read_at = 0.0    # 上次收藏夹读请求时间(全局节流)
        self.read_interval = READ_INTERVAL  # 读请求最小间隔(秒)，可由 server 调整
        self._mixin_key = ""        # WBI 签名 mixin_key
        self._mixin_key_ts = 0.0    # 获取时间
        self._nav_data = None       # 缓存的 nav 返回，供 WBI 复用
        self._mid = ""              # 当前用户 mid，避免执行初始化重复请求 nav
        self._risk_got_once = set()  # 已做过"等30s重试"的错误码集合(第二次遇再停止)

    # ---------- 基础请求 ----------
    def _request(self, method: str, url: str, *, params=None, data=None,
                 allow_retry=True, headers=None, should_stop=None,
                 timeout=REQUEST_TIMEOUT, uncertain_on_transport=False):
        """带重试的请求。非 0 code 视为业务错误。"""
        last_exc = None
        attempts = RETRY_READ if allow_retry else 1
        for i in range(attempts):
            try:
                if method.upper() == "GET":
                    self._throttle_read(should_stop=should_stop)
                    if should_stop and should_stop():
                        raise RateLimitedError("已手动停止（请求间隔等待期间）")
                resp = self.session.request(
                    method, url, params=params, headers=headers,
                    data=data, timeout=timeout,
                )
                # HTTP 412：B站限流/风控。分片退避（期间可被停止）后重试
                if resp.status_code == 412:
                    if i < attempts - 1:
                        _notify("warn", f"遭遇 HTTP 412（限流），退避等待 {HTTP_412_BACKOFF} 秒"
                                        f"（可随时点停止）")
                        remain = HTTP_412_BACKOFF
                        while remain > 0:
                            if should_stop and should_stop():
                                raise RateLimitedError("已手动停止（412 退避期间）")
                            time.sleep(1)
                            remain -= 1
                            if remain and remain % 30 == 0:
                                _notify("info", f"412 退避中，剩余约 {remain} 秒")
                        continue
                    raise RateLimitedError(
                        f"B站接口限流(HTTP 412)：{url}\n"
                        "已退避重试仍被拒，账号/IP 可能处于冷却期。\n"
                        "建议：等待较长时间(如 30 分钟~数小时)后再扫描；已抓取的数据不会丢失。"
                    )
                resp.raise_for_status()
                body = resp.json()
                code = body.get("code", -1)
                if code == CODE_OK:
                    return body.get("data", {})
                if code in (CODE_NOT_LOGIN, CODE_TOKEN_EXPIRED):
                    raise BiliApiError(
                        f"B 站登录态失效(code={code})：{body.get('message','')}。请重新登录浏览器。"
                    )
                # 风控/权限类
                if code in (CODE_FAV_LIMITED, -403):
                    raise RateLimitedError(
                        f"疑似触发风控或权限不足(code={code})：{body.get('message','')}"
                    )
                # 频率/风控校验错误码：-352 需验证码，-503 频率过高
                if code in (-352, -503, -509):
                    if attempts <= 1:
                        raise RateLimitedError(
                            f"B 站风控/频率限制(code={code})：{body.get('message','')}"
                        )
                    if code in self._risk_got_once:
                        raise RateLimitedError(
                            f"B 站风控校验失败(code={code})：{body.get('message','')}。\n"
                            "已自动等待 30 秒重试过一次仍失败，可能需要人工处理或过验证码，扫描已停止。"
                        )
                    self._risk_got_once.add(code)
                    _notify("warn", f"遇到风控码 {code}，等待 30 秒后重试")
                    for _ in range(30):
                        if should_stop and should_stop():
                            raise RateLimitedError("已手动停止（风控等待期间）")
                        time.sleep(1)
                    continue  # 重新尝试本次请求
                # 其它业务错误，重试几次再抛
                raise BiliApiError(f"B站返回 code={code}：{body.get('message','')}")
            except (RateLimitedError, BiliApiError):
                raise
            except Exception as e:  # 网络/JSON 异常，允许重试
                last_exc = e
                if i < attempts - 1:
                    time.sleep(0.5 * (i + 1))
        if uncertain_on_transport:
            raise WriteUncertainError(
                f"批量写请求未收到可确认响应：{url} ({last_exc})"
            )
        raise BiliApiError(f"请求失败：{url} ({last_exc})")

    def _throttle_write(self, should_stop=None):
        """写操作限速：距上次写操作至少 write_interval 秒（另加随机抖动）。"""
        gap = self.write_interval + random.uniform(0, WRITE_JITTER)
        while True:
            wait = self._last_write_at + gap - time.monotonic()
            if wait <= 0:
                break
            if should_stop and should_stop():
                raise RateLimitedError("已手动停止（写操作等待期间）")
            time.sleep(min(0.5, wait))
        self._last_write_at = time.monotonic()

    def _throttle_read(self, wait_slice=0.5, should_stop=None):
        """全局读节流：保证两次收藏夹请求间隔 >= read_interval 秒（跨收藏夹也生效）。

        分片睡眠，便于 should_stop 时快速退出。
        """
        while True:
            wait = self._last_read_at + self.read_interval - time.monotonic()
            if wait <= 0:
                break
            if should_stop and should_stop():
                return
            time.sleep(min(wait_slice, wait))
        self._last_read_at = time.monotonic()

    def _write_log(self, action: str, payload: dict):
        """把一次写操作追加到断点日志，便于中断后重建进度。"""
        if not WRITE_LOG_PATH:
            return
        try:
            record = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action, **payload}
            with open(WRITE_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ---------- WBI 签名 ----------
    def _extract_wbi_key(self, url: str) -> str:
        """从 wbi_img 图片 URL 提取 key（URL 文件名去掉扩展名）。"""
        if not url:
            return ""
        name = urllib.parse.urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        return name.split(".")[0]

    def _mixin_from_nav(self, nav: dict) -> bool:
        """从 nav 数据提取 img_key/sub_key 合成 mixin_key；成功返回 True。"""
        wbi = (nav or {}).get("wbi_img", {}) or {}
        img_key = self._extract_wbi_key(wbi.get("img_url", ""))
        sub_key = self._extract_wbi_key(wbi.get("sub_url", ""))
        if len(img_key) != 32 or len(sub_key) != 32:
            return False
        self._mixin_key = img_key + sub_key
        self._mixin_key_ts = time.time()
        return True

    def _refresh_wbi_keys(self):
        """获取 WBI 签名密钥：优先复用已取到的 nav 数据，避免重复请求（P2-5）。"""
        if self._mixin_key:
            return self._mixin_key
        if getattr(self, "_nav_data", None) and self._mixin_from_nav(self._nav_data):
            return self._mixin_key
        nav = self._request("GET", f"{self._wapi}/x/web-interface/nav")
        self._nav_data = nav
        if not self._mixin_from_nav(nav):
            wbi = (nav or {}).get("wbi_img", {}) or {}
            raise BiliApiError(
                "无法获取 WBI 签名密钥(keys="
                f"{len(self._extract_wbi_key(wbi.get('img_url','')))},"
                f"{len(self._extract_wbi_key(wbi.get('sub_url','')))})")
        return self._mixin_key

    def _wbi_sign(self, params: dict) -> dict:
        """对参数做 WBI 签名，返回带 wts / w_rid 的副本。"""
        out = dict(params)
        out["wts"] = str(int(time.time()))
        # 过滤掉值含特殊字符的项，排序拼接
        items = sorted(
            (k, str(v)) for k, v in out.items()
            if k not in ("w_rid", "wts") and not _contains_unsafe(str(v))
        )
        query = urllib.parse.urlencode(items, safe="'()*!-._~")
        mixin = self._refresh_wbi_keys()
        out["w_rid"] = hashlib.md5(f"{query}{mixin}".encode()).hexdigest()
        return out

    # ---------- 用户 & 收藏夹 ----------
    def get_mid(self) -> str:
        """获取当前登录用户 mid，同时缓存 nav 数据供 WBI 签名复用（只发一次请求）。"""
        if self._mid:
            return self._mid
        self._throttle_read()
        data = self._request("GET", f"{self._wapi}/x/web-interface/nav")
        self._nav_data = data
        try:
            self._mixin_from_nav(data)
        except Exception:
            pass
        self._mid = str(data.get("mid", ""))
        return self._mid

    def list_folders(self) -> list[dict]:
        """获取收藏夹列表，返回 [{media_id, title, count, attr, ...}]。

        attr 是属性位域：bit0=是否私有，bit1=0 表示默认收藏夹。
        保留它供服务端可靠识别默认收藏夹（不依赖标题文字）。
        """
        mid = self.get_mid()
        self._throttle_read()
        data = self._request("GET", f"{self._wapi}/x/v3/fav/folder/created/list-all",
                             params={"up_mid": mid})
        folders = []
        for f in data.get("list", []) or []:
            folders.append({
                "media_id": str(f.get("id", "")),
                "title": f.get("title", ""),
                "count": f.get("media_count", 0) or 0,
                "cover": f.get("cover", ""),
                "attr": f.get("attr"),
            })
        return folders

    def get_folder_info(self, media_id: str) -> dict:
        """读取单个收藏夹元数据（包括当前简介、标题、封面和属性位）。"""
        self._throttle_read()
        data = self._request("GET", f"{self._wapi}/x/v3/fav/folder/info",
                             params={"media_id": str(media_id)})
        if not isinstance(data, dict):
            raise BiliApiError("B站没有返回有效的收藏夹信息")
        return data

    def iter_folder_videos(self, media_id: str, total: int,
                           should_stop=None, on_event=None):
        """逐页产出收藏夹 video 记录。每页 PAGE_SIZE 条。带 WBI 签名。

        should_stop: 可选回调，返回 True 则立即停止
        on_event:    可选回调，接收事件 dict（req / page）
        """
        pn = 1
        total_pages = max(1, (int(total) + PAGE_SIZE - 1) // PAGE_SIZE)
        while True:
            if pn > MAX_SCAN_PAGES:
                raise BiliApiError(f"收藏夹分页超过安全上限 {MAX_SCAN_PAGES}")
            if should_stop and should_stop():
                return
            # 全局节流：与上次收藏夹请求间隔 >= read_interval 秒（跨收藏夹也生效）
            self._throttle_read(should_stop=should_stop)
            if should_stop and should_stop():
                return
            if on_event:
                on_event({"type": "req", "media_id": media_id,
                          "pn": pn, "total_pages": total_pages})
            params = {
                "media_id": media_id,
                "pn": pn,
                "ps": PAGE_SIZE,
                "order": "mtime",     # 按收藏时间
                "type": "0",          # 视频
                "platform": "web",
            }
            params = self._wbi_sign(params)
            data = self._request(
                "GET", f"{self._wapi}/x/v3/fav/resource/list",
                params=params, should_stop=should_stop,
            )
            medias = data.get("medias", []) or []
            if not medias:
                break
            for m in medias:
                yield self._normalize_video(m, media_id)
            if on_event:
                on_event({"type": "page", "media_id": media_id,
                          "pn": pn, "got": len(medias)})
            # 翻页判断：以 has_more 为准。
            # 注意：收藏夹含失效视频时，中间页可能不足 PAGE_SIZE 条，
            # 此时 has_more 仍为 True —— 不能按"短页"判定结束（曾导致大量漏抓）。
            has_more = data.get("has_more")
            if has_more is False:
                break
            pn += 1

    def get_folder_resource_ids(self, media_id: str, total: int,
                                should_stop=None, on_event=None) -> list[dict]:
        """获取小收藏夹的资源 ID 清单；超过实测候选边界时拒绝，交由分页扫描。"""
        total = int(total or 0)
        if total < 0 or total > FAV_BULK_CANDIDATE_LIMIT:
            raise BiliApiError(f"收藏夹数量 {total} 超过 resource/ids 候选边界")
        if total == 0:
            return []
        if should_stop and should_stop():
            return []
        self._throttle_read(should_stop=should_stop)
        if should_stop and should_stop():
            return []
        if on_event:
            on_event({"type": "bulk_req", "media_id": media_id, "step": "ids"})
        ids = self._request("GET", f"{self._wapi}/x/v3/fav/resource/ids",
                            params={"media_id": media_id, "platform": "web"},
                            should_stop=should_stop) or []
        if not isinstance(ids, list) or len(ids) != total:
            raise BiliApiError(f"ID 清单数量不匹配：目录 {total}，接口返回 {len(ids) if isinstance(ids, list) else '非列表'}")
        normalized = []
        keys = set()
        for item in ids:
            if not isinstance(item, dict) or item.get("id") is None or item.get("type") is None:
                raise BiliApiError("ID 清单包含缺少 id/type 的条目")
            try:
                resource_type = int(item["type"])
            except (TypeError, ValueError):
                raise BiliApiError("ID 清单包含无效资源类型")
            key = (resource_type, str(item["id"]))
            if key in keys:
                raise BiliApiError("ID 清单包含重复资源")
            keys.add(key)
            normalized.append({"id": str(item["id"]), "type": resource_type})
        return normalized

    def get_resource_infos_bulk(self, media_id: str, ids: list[dict],
                                should_stop=None, on_event=None) -> list[dict]:
        """批量取得指定资源的元数据，不重新读取完整收藏夹 ID 清单。"""
        if not ids:
            return []
        if len(ids) > FAV_BULK_CANDIDATE_LIMIT:
            raise BiliApiError(f"单次元数据请求超过 {FAV_BULK_CANDIDATE_LIMIT} 条候选边界")
        resources = []
        requested = []
        for item in ids:
            if not isinstance(item, dict) or item.get("id") is None or item.get("type") is None:
                raise BiliApiError("待查资源 ID 清单格式无效")
            key = (str(item["type"]), str(item["id"]))
            requested.append(key)
            resources.append(f"{item['id']}:{item['type']}")
        if len(set(requested)) != len(requested):
            raise BiliApiError("待查资源 ID 清单包含重复项")
        self._throttle_read(should_stop=should_stop)
        if should_stop and should_stop():
            return []
        if on_event:
            on_event({"type": "bulk_req", "media_id": media_id, "step": "infos", "count": len(ids)})
        infos = self._request("GET", f"{self._wapi}/x/v3/fav/resource/infos",
                              params={"resources": ",".join(resources), "platform": "web"},
                              should_stop=should_stop) or []
        if not isinstance(infos, list):
            raise BiliApiError("批量元数据接口返回格式不是列表")
        info_by_key = {}
        for item in infos:
            if not isinstance(item, dict) or item.get("id") is None or item.get("type") is None:
                continue
            key = (str(item["type"]), str(item["id"]))
            if key in info_by_key:
                raise BiliApiError("批量元数据包含重复资源")
            info_by_key[key] = item
        missing = [key for key in requested if key not in info_by_key]
        if missing or len(info_by_key) != len(requested):
            raise BiliApiError(f"批量元数据不完整：缺少 {len(missing)} 条")
        result = []
        for item, key in zip(ids, requested):
            raw = info_by_key[key]
            video = self._normalize_video(raw, media_id)
            video["id"] = str(item["id"])
            video["type"] = int(item["type"])
            video["attr"] = raw.get("attr", 0)
            video["resource_key"] = f"{item['type']}:{item['id']}"
            result.append(video)
        return result

    def get_folder_videos_bulk(self, media_id: str, total: int,
                               should_stop=None, on_event=None) -> list[dict]:
        """尝试用 resource/ids + resource/infos 一次取得小收藏夹的全部元数据。

        只接受数量和资源键均可核对的完整结果。调用者应在失败后回退 resource/list。
        该接口的 1000 项边界未被项目验证；此处限制尝试范围，不作为完整性保证。
        """
        total = int(total or 0)
        if total < 0 or total > FAV_BULK_CANDIDATE_LIMIT:
            raise BiliApiError(f"收藏夹数量 {total} 超过批量扫描候选范围")
        if total == 0:
            return []
        if should_stop and should_stop():
            return []

        self._throttle_read(should_stop=should_stop)
        if should_stop and should_stop():
            return []
        if on_event:
            on_event({"type": "bulk_req", "media_id": media_id, "step": "ids"})
        ids = self._request("GET", f"{self._wapi}/x/v3/fav/resource/ids",
                            params={"media_id": media_id, "platform": "web"},
                            should_stop=should_stop) or []
        if not isinstance(ids, list) or len(ids) != total:
            raise BiliApiError(f"ID 清单数量不匹配：目录 {total}，接口返回 {len(ids) if isinstance(ids, list) else '非列表'}")

        keys = []
        resources = []
        for item in ids:
            if not isinstance(item, dict) or item.get("id") is None or item.get("type") is None:
                raise BiliApiError("ID 清单包含缺少 id/type 的条目")
            key = (str(item["type"]), str(item["id"]))
            keys.append(key)
            resources.append(f"{item['id']}:{item['type']}")
        if len(set(keys)) != len(keys):
            raise BiliApiError("ID 清单包含重复资源")

        self._throttle_read(should_stop=should_stop)
        if should_stop and should_stop():
            return []
        if on_event:
            on_event({"type": "bulk_req", "media_id": media_id, "step": "infos", "count": total})
        infos = self._request(
            "GET", f"{self._wapi}/x/v3/fav/resource/infos",
            params={"resources": ",".join(resources), "platform": "web"},
            should_stop=should_stop) or []
        if not isinstance(infos, list):
            raise BiliApiError("批量元数据接口返回格式不是列表")

        info_by_key = {}
        for item in infos:
            if not isinstance(item, dict) or item.get("id") is None or item.get("type") is None:
                continue
            key = (str(item["type"]), str(item["id"]))
            if key in info_by_key:
                raise BiliApiError("批量元数据包含重复资源")
            info_by_key[key] = item
        missing = [key for key in keys if key not in info_by_key]
        if missing or len(info_by_key) != len(keys):
            raise BiliApiError(f"批量元数据不完整：缺少 {len(missing)} 条")

        result = []
        for item, key in zip(ids, keys):
            raw = info_by_key[key]
            video = self._normalize_video(raw, media_id)
            video["id"] = str(item["id"])
            video["type"] = int(item["type"])
            video["attr"] = raw.get("attr", 0)
            video["resource_key"] = f"{item['type']}:{item['id']}"
            result.append(video)
        if len(result) != total:
            raise BiliApiError(f"批量元数据条数校验失败：目录 {total}，获得 {len(result)}")
        return result

    @staticmethod
    def _normalize_video(m: dict, media_id: str) -> dict:
        """把收藏 API 返回的 medias 项规整成统一字段。"""
        bvid = m.get("bvid", "") or ""
        aid = m.get("id", 0) or m.get("aid", 0)
        upper = m.get("upper", {}) or {}
        try:
            aid = int(aid or 0)
        except (TypeError, ValueError):
            aid = 0
        return {
            "bvid": str(bvid),
            "aid": aid,
            "id": str(m.get("id") or aid or ""),
            "title": (m.get("title") or "").strip(),
            "desc": (m.get("intro") or m.get("desc") or "").strip(),
            "type": m.get("type", 0),
            "upper_name": upper.get("name", ""),
            "upper_mid": str(upper.get("mid", "")),
            "duration": m.get("duration", 0),
            "pubtime": m.get("pubtime", 0),
            "fav_time": m.get("fav_time", 0) or m.get("ctime", 0),
            "source_folder_id": str(media_id),
            "tags": m.get("tags", []) or [],   # 部分接口返回
            "attr": m.get("attr", 0),
        }

    # ---------- 写操作 ----------
    def create_folder(self, title: str) -> str:
        """创建收藏夹，返回新 media_id。"""
        self._throttle_write()
        url = f"{self._wapi}/x/v3/fav/folder/add"
        data = {"title": title, "csrf": self.cookie.bili_jct}
        # Content-Type 用请求级传入，不污染会话（P1-3）
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        payload = {"action": "create_folder", "title": title}
        try:
            d = self._request("POST", url, data=data, allow_retry=False, headers=headers)
            self._write_log("create_folder", payload)
            return str(d.get("id", ""))
        except (RateLimitedError,) as e:
            self._write_log("create_folder_failed", payload)
            raise

    def rename_folder(self, media_id: str, title: str, *, should_stop=None) -> None:
        """重命名收藏夹。无确认响应时抛 WriteUncertainError。"""
        title = (title or "").strip()
        if not title:
            raise BiliApiError("收藏夹名不能为空")
        payload = {"action": "rename_folder", "media_id": str(media_id), "title": title}
        self._write_log("rename_folder_sending", payload)
        self._throttle_write(should_stop=should_stop)
        try:
            self._request(
                "POST", f"{self._wapi}/x/v3/fav/folder/edit",
                data={"media_id": str(media_id), "title": title,
                      "csrf": self.cookie.bili_jct},
                allow_retry=False, timeout=BATCH_WRITE_TIMEOUT,
                uncertain_on_transport=True, should_stop=should_stop,
            )
            self._write_log("rename_folder_done", payload)
        except Exception as e:
            tag = "rename_folder_unknown" if isinstance(e, WriteUncertainError) else "rename_folder_failed"
            self._write_log(tag, {**payload, "error": str(e)})
            raise

    def update_folder_intro(self, media_id: str, folder_info: dict, intro: str,
                            *, should_stop=None) -> dict:
        """仅更新收藏夹简介，同时保留远端标题、公开状态和已有封面。"""
        title = str(folder_info.get("title") or "").strip()
        if not title:
            raise BiliApiError("无法读取当前收藏夹名称，拒绝提交简介以免意外改名")
        if "attr" not in folder_info:
            raise BiliApiError("无法读取收藏夹公开状态，拒绝提交简介以免意外更改隐私设置")
        try:
            privacy = int(folder_info.get("attr") or 0) & 1
        except (TypeError, ValueError) as exc:
            raise BiliApiError("收藏夹公开状态无效，未提交简介") from exc
        intro = str(intro or "")
        if len(intro) > 200:
            raise BiliApiError("收藏夹简介不能超过 200 字")
        payload = {"action": "update_folder_intro", "media_id": str(media_id),
                   "title": title, "intro_length": len(intro)}
        self._write_log("update_folder_intro_sending", payload)
        self._throttle_write(should_stop=should_stop)
        data = {"media_id": str(media_id), "title": title, "intro": intro,
                "privacy": privacy, "csrf": self.cookie.bili_jct}
        cover = str(folder_info.get("cover") or "").strip()
        if cover:
            data["cover"] = cover
        try:
            result = self._request(
                "POST", f"{self._wapi}/x/v3/fav/folder/edit",
                data=data, allow_retry=False, timeout=BATCH_WRITE_TIMEOUT,
                uncertain_on_transport=True, should_stop=should_stop,
            )
            self._write_log("update_folder_intro_done", payload)
            return result if isinstance(result, dict) else {}
        except Exception as exc:
            tag = "update_folder_intro_unknown" if isinstance(exc, WriteUncertainError) else "update_folder_intro_failed"
            self._write_log(tag, {**payload, "error": str(exc)})
            raise

    def delete_folder(self, media_id: str, *, should_stop=None) -> None:
        """删除收藏夹。调用方必须先确认收藏夹已空。"""
        payload = {"action": "delete_folder", "media_id": str(media_id)}
        self._write_log("delete_folder_sending", payload)
        self._throttle_write(should_stop=should_stop)
        try:
            self._request(
                "POST", f"{self._wapi}/x/v3/fav/folder/del",
                data={"media_ids": str(media_id), "csrf": self.cookie.bili_jct},
                allow_retry=False, timeout=BATCH_WRITE_TIMEOUT,
                uncertain_on_transport=True, should_stop=should_stop,
            )
            self._write_log("delete_folder_done", payload)
        except Exception as e:
            tag = "delete_folder_unknown" if isinstance(e, WriteUncertainError) else "delete_folder_failed"
            self._write_log(tag, {**payload, "error": str(e)})
            raise

    def clean_invalid_folder(self, media_id: str, *, should_stop=None) -> None:
        """清理指定收藏夹中 B 站判定为失效的内容。"""
        payload = {"action": "clean_invalid_folder", "media_id": str(media_id)}
        self._write_log("clean_invalid_folder_sending", payload)
        self._throttle_write(should_stop=should_stop)
        try:
            self._request(
                "POST", f"{self._wapi}/x/v3/fav/resource/clean",
                data={"media_id": str(media_id), "csrf": self.cookie.bili_jct},
                allow_retry=False, timeout=BATCH_WRITE_TIMEOUT,
                uncertain_on_transport=True, should_stop=should_stop,
            )
            self._write_log("clean_invalid_folder_done", payload)
        except Exception as e:
            tag = ("clean_invalid_folder_unknown" if isinstance(e, WriteUncertainError)
                   else "clean_invalid_folder_failed")
            self._write_log(tag, {**payload, "error": str(e)})
            raise

    def add_to_folder(self, media_id: str, resources: list[str], type_: int = 2) -> bool:
        """把若干 bvid（或 aid）添加到收藏夹。resources 支持 aid_list。

        act=1 表示添加。type: 2=视频(用aid)。这里统一处理：
          内部先把传入的 bvid 列表按需转 aid 太繁琐，故约定顶层只传 aid。
        返回 True 表示成功。
        """
        # 简化：B站 deal 接口用 rid（aid）而非 bvid。这里我们提供 add_with_aid 供 server 调用。
        raise NotImplementedError("请使用 add_with_aid / remove_from_folder")

    def add_with_aid(self, media_id: str, aid: int) -> bool:
        """把单个 aid 添加进收藏夹(act=1)。"""
        self._throttle_write()
        url = f"{self._wapi}/x/v3/fav/resource/deal"
        data = {
            "rid": str(aid), "type": "2",
            "add_media_ids": str(media_id),
            "csrf": self.cookie.bili_jct,
        }
        payload = {"action": "add", "media_id": media_id, "aid": aid}
        try:
            self._request("POST", url, data=data, allow_retry=False)
            self._write_log("add", payload)
            return True
        except (RateLimitedError, BiliApiError) as e:
            self._write_log("add_failed", {**payload, "error": str(e)})
            return False

    def remove_from_folder(self, media_id: str, aid: int) -> bool:
        """把单个 aid 从收藏夹移出(act=2)。"""
        self._throttle_write()
        url = f"{self._wapi}/x/v3/fav/resource/deal"
        data = {
            "rid": str(aid), "type": "2",
            "del_media_ids": str(media_id),
            "csrf": self.cookie.bili_jct,
        }
        payload = {"action": "remove", "media_id": media_id, "aid": aid}
        try:
            self._request("POST", url, data=data, allow_retry=False)
            self._write_log("remove", payload)
            return True
        except (RateLimitedError, BiliApiError) as e:
            self._write_log("remove_failed", {**payload, "error": str(e)})
            return False

    @staticmethod
    def _batch_resources(aids: list[int]) -> tuple[list[int], str]:
        """去重并组装批量 resources；严格限制为 1~1000 条。"""
        clean = list(dict.fromkeys(int(a) for a in aids if int(a) > 0))
        if not clean:
            raise BiliApiError("批量操作没有有效 aid")
        if len(clean) > BATCH_LIMIT:
            raise BiliApiError(f"批量操作超过上限：{len(clean)} > {BATCH_LIMIT}")
        return clean, ",".join(f"{aid}:2" for aid in clean)

    def move_batch(self, src_media_id: str, tar_media_id: str, aids: list[int],
                   *, mid: str = "", should_stop=None) -> int:
        """批量移动视频；返回实际提交的去重 aid 数。

        code=0 按整批成功处理。运输层无确认响应时抛 WriteUncertainError，
        由上层标记 unknown 并交给人工复核。
        """
        clean, resources = self._batch_resources(aids)
        if str(src_media_id) == str(tar_media_id):
            raise BiliApiError("来源收藏夹与目标收藏夹相同")
        owner_mid = str(mid or self.get_mid())
        payload = {
            "action": "batch_move", "src_media_id": str(src_media_id),
            "tar_media_id": str(tar_media_id), "aids": clean,
        }
        self._write_log("batch_move_sending", payload)
        self._throttle_write(should_stop=should_stop)
        try:
            self._request(
                "POST", f"{self._wapi}/x/v3/fav/resource/move",
                data={
                    "src_media_id": str(src_media_id),
                    "tar_media_id": str(tar_media_id),
                    "mid": owner_mid,
                    "resources": resources,
                    "platform": "web",
                    "csrf": self.cookie.bili_jct,
                },
                allow_retry=False,
                should_stop=should_stop,
                timeout=BATCH_WRITE_TIMEOUT,
                uncertain_on_transport=True,
            )
            self._write_log("batch_move_done", payload)
            return len(clean)
        except Exception as e:
            tag = "batch_move_unknown" if isinstance(e, WriteUncertainError) else "batch_move_failed"
            self._write_log(tag, {**payload, "error": str(e)})
            raise

    def batch_delete(self, media_id: str, aids: list[int], *, should_stop=None) -> int:
        """从一个收藏夹批量删除视频；返回实际提交的去重 aid 数。"""
        clean, resources = self._batch_resources(aids)
        payload = {"action": "batch_delete", "media_id": str(media_id), "aids": clean}
        self._write_log("batch_delete_sending", payload)
        self._throttle_write(should_stop=should_stop)
        try:
            self._request(
                "POST", f"{self._wapi}/x/v3/fav/resource/batch-del",
                data={
                    "media_id": str(media_id),
                    "resources": resources,
                    "platform": "web",
                    "csrf": self.cookie.bili_jct,
                },
                allow_retry=False,
                should_stop=should_stop,
                timeout=BATCH_WRITE_TIMEOUT,
                uncertain_on_transport=True,
            )
            self._write_log("batch_delete_done", payload)
            return len(clean)
        except Exception as e:
            tag = "batch_delete_unknown" if isinstance(e, WriteUncertainError) else "batch_delete_failed"
            self._write_log(tag, {**payload, "error": str(e)})
            raise

    # ---------- 视频标签(可选) ----------
    def get_tags(self, bvid: str) -> list[str]:
        """获取视频标签，用于增强内容判断。失败返回空列表。"""
        try:
            d = self._request("GET", f"{self._wapi}/x/tag/archive/tags", params={"bvid": bvid})
            return [t.get("tag_name", "") for t in d if t.get("tag_name")]
        except BiliApiError:
            return []
