# -*- coding: utf-8 -*-
"""LLM 分析模块 — 批量判断视频应归属的收藏夹。

调用 OpenAI 兼容格式的模型接口（可配置 base_url / api_key / model）。

设计要点：
  - **批处理**：一次请求发送多条视频，收藏夹清单只在 system 提示词里写一次
  - **结构化输入/输出**：输入 {"videos":[...]}，输出 {"results":[...]}，按整数 id 对齐
  - **容错**：
      * 正文为空（推理模型把 token 花在 reasoning 上）→ 重试无用，直接二分拆分
      * 解析失败 / 网络错误 → 重试；仍失败则二分拆分
      * 单条仍失败 → 放弃并计入失败
  - **多线程安全**：使用线程本地 requests.Session
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

import requests

log = logging.getLogger("llm_analyzer")

DEFAULT_MODEL = "qwen3-8b"
DEFAULT_TIMEOUT = 180         # 推理模型较慢，给足超时
DEFAULT_MAX_TOKENS = 32768    # 单次输出上限（含 reasoning），界面可设，默认 32K
MAX_RETRY = 2                 # 解析失败/网络错误的重试次数
MAX_SPLIT_DEPTH = 3           # 二分拆分递归深度上限
ACTIONS = ("move_to_existing", "create_new", "skip")
TITLE_MAX = 120
DESC_MAX = 200


def _extract_json(text: str):
    """从模型回复中提取 JSON 对象。优先找 ```json 围栏，否则整体尝试解析。"""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    m = re.search(r"(\{.*\})", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return None


class LLMConfig:
    """模型连接配置。可从 dict 或 config.json 构建。"""

    def __init__(self, base_url: str = "", api_key: str = "",
                 model: str = DEFAULT_MODEL, params: dict | None = None,
                 max_tokens: int = DEFAULT_MAX_TOKENS):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model or DEFAULT_MODEL
        self.params = params or {}
        self.max_tokens = int(max_tokens or DEFAULT_MAX_TOKENS)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)


class LLMAnalyzer:
    def __init__(self, config: LLMConfig, folders: list[str] | None = None):
        self.config = config
        self.folders = list(folders or [])
        self._local = threading.local()
        self._sys = None

    # ---------- 基础设施 ----------
    def _session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            self._local.s = s
        return s

    def _system_prompt(self) -> str:
        if self._sys is None:
            fl = "、".join(self.folders) if self.folders else "（暂无）"
            self._sys = (
                "你是 B 站收藏夹整理助手。任务是判断一批视频各自**归属于哪个收藏夹**。\n"
                "\n"
                f"【用户现有收藏夹】（共 {len(self.folders)} 个）\n{fl}\n"
                "\n"
                "【输入】一个 JSON 对象：\n"
                '{"videos":[{"id":1,"title":"...","desc":"...","upper":"..."}, ...]}\n'
                "\n"
                "【输出】必须是**纯 JSON**，不要任何额外文字：\n"
                '{"results":[{"id":1,"action":"move_to_existing|create_new|skip",'
                '"recommended":"收藏夹名","reason":"一句话理由","confidence":0.9}, ...]}\n'
                "\n"
                "【规则】\n"
                "1. 每个输入 id 必须有一条对应结果，id 原样返回，不得遗漏、不得新增。\n"
                "2. 优先归入现有收藏夹 → action=move_to_existing，recommended 必须原样照抄现有夹名。\n"
                "3. 现有夹都不贴合、而视频主题清晰 → action=create_new，recommended 给 2~6 字新夹名。\n"
                "4. 仅当标题/简介/UP主 全空或完全无法归类 → action=skip。\n"
                "5. confidence 为 0~1 的把握程度。\n"
                "6. 理由务必简短（不超过 20 字），不要展开分析，避免浪费输出长度。\n"
                "只返回 JSON。"
            )
        return self._sys

    @staticmethod
    def _pack(videos: list[dict]) -> list[dict]:
        """把批量视频压成请求用的紧凑结构（局部 id = 1..N）。"""
        out = []
        for i, v in enumerate(videos, start=1):
            item = {"id": i, "title": (v.get("title") or "")[:TITLE_MAX]}
            d = (v.get("desc") or "").strip()
            if d:
                item["desc"] = d[:DESC_MAX]
            if v.get("upper_name"):
                item["upper"] = v["upper_name"]
            out.append(item)
        return out

    # ---------- 单次调用 ----------
    def _call(self, videos: list[dict]):
        """调用一次模型。

        返回 (结果字典 bvid->result, 缺失视频列表, 原因)
        原因: "ok" | "partial" | "empty" | "parse" | "error"
        """
        n = len(videos)
        if n == 0:
            return {}, [], "ok"
        # 输出预算：推理模型的思考也占 token，必须给足（否则正文为空）
        max_tokens = min(self.config.max_tokens, 3000 + 500 * n)
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": json.dumps(
                    {"videos": self._pack(videos)}, ensure_ascii=False)},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            **self.config.params,
        }
        url = f"{self.config.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        reason = "error"
        for _ in range(MAX_RETRY):
            try:
                resp = self._session().post(
                    url, json=payload, headers=headers, timeout=DEFAULT_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"].get("content") or ""
                if not content.strip():
                    # 推理模型把预算耗在 reasoning 上 → 重试无用，交由上层拆分
                    log.warning("空正文(batch=%d, max_tokens=%d)，将拆分", n, max_tokens)
                    return {}, list(videos), "empty"
                obj = _extract_json(content)
                if not obj:
                    reason = "parse"
                    continue
                results = obj.get("results")
                if not isinstance(results, list):
                    if isinstance(obj, dict) and "id" in obj:
                        results = [obj]           # 兼容单条返回
                    else:
                        reason = "parse"
                        continue
                out = {}
                for r in results:
                    if not isinstance(r, dict):
                        continue
                    try:
                        rid = int(r.get("id"))
                    except Exception:
                        continue
                    if rid < 1 or rid > n:
                        continue
                    bvid = videos[rid - 1].get("bvid", "")
                    if not bvid:
                        continue
                    rec = str(r.get("recommended", "")).strip()
                    action = str(r.get("action", "")).strip()
                    if action not in ACTIONS:
                        action = ("move_to_existing" if rec in self.folders
                                  else ("create_new" if rec else "skip"))
                    try:
                        conf = float(r.get("confidence", 0) or 0)
                    except Exception:
                        conf = 0.0
                    out[bvid] = {
                        "bvid": bvid,
                        "recommended": rec,
                        "action": action,
                        "reason": str(r.get("reason", "")).strip(),
                        "confidence": conf,
                    }
                missing = [v for v in videos if v.get("bvid") not in out]
                return out, missing, ("ok" if not missing else "partial")
            except Exception as e:
                reason = "error"
                log.warning("调用失败(batch=%d): %s", n, e)
                time.sleep(0.5)
        return {}, list(videos), reason

    # ---------- 批处理 + 容错 ----------
    def analyze_batch(self, videos: list[dict], _depth: int = 0) -> dict:
        """分析一批视频，返回 {bvid: result}。"""
        if not videos or not self.config.configured:
            return {}
        got, missing, reason = self._call(videos)
        if not missing:
            return got
        if len(missing) == 1:
            # 解析失败/网络错误可再试一次；"空正文"重试无用，直接放弃
            if reason in ("parse", "error"):
                got2, _, _ = self._call(missing)
                got.update(got2)
            return got
        if _depth >= MAX_SPLIT_DEPTH:
            return got
        mid = len(missing) // 2
        got.update(self.analyze_batch(missing[:mid], _depth + 1))
        got.update(self.analyze_batch(missing[mid:], _depth + 1))
        return got

    # ---------- 单条包装（兼容旧调用） ----------
    def analyze(self, video: dict) -> dict | None:
        r = self.analyze_batch([video])
        return r.get(video.get("bvid", ""))


def suggest_folder_merges(config: LLMConfig, folder_profiles: list[dict]) -> list[dict]:
    """根据收藏夹名称和代表内容生成保守的合并建议。"""
    system = (
        "你是 B 站收藏夹信息架构整理助手。目标是在保留有用主题边界的前提下，"
        "减少过细、零散、用途高度接近的收藏夹，使目录更容易浏览和维护。"
        "不要为合并而合并，也不要把「完全重复」当成唯一条件。\n"
        "请综合判断：名称和主题、代表视频、收藏数量、跨夹重复数 overlaps，以及用户查找这些内容时是否会自然地在同一个夹中查找。\n"
        "可推荐的情形：\n"
        "1. synonym：名称近义且内容方向一致。\n"
        "2. content_overlap：代表内容、关键对象或实际收藏明显交叉。\n"
        "3. natural_parent：多个较小子主题能形成自然、清晰、不过度宽泛的上位分类，且分开保留没有稳定的检索价值。\n"
        "不得推荐：仅同属一个大领域；合并后只能使用空泛名称；各夹内容较多且边界清晰；"
        "专业主题会被普通大类吞没；只是受众相同或处于同一应用链条。默认收藏夹不参与。\n"
        "建议分级：high 表示合并价值明确（confidence>=0.82）；medium 表示结构上合理但需人工判断（0.68~0.82）；"
        "低于 0.68 不输出。样本不足时不得给 high，但可以给 medium 候选。\n"
        "每个收藏夹最多出现在一组。目标夹选内容更完整、名称更具代表性或数量更多的夹。"
        "final_name 应准确覆盖全组，一般为 2~10 个字。reason 写具体依据，risk 写可能损失的分类边界。\n"
        "只返回纯 JSON："
        '{"groups":[{"final_name":"...","target_id":"...","source_ids":["..."],'
        '"level":"high|medium","merge_type":"synonym|content_overlap|natural_parent",'
        '"reason":"...","risk":"...","confidence":0.82}]}\n'
        "target_id 和 source_ids 必须原样使用输入 id，两者不得重复。没有合适建议时返回 {\"groups\":[]}。"
    )
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps({"folders": folder_profiles}, ensure_ascii=False)},
        ],
        "temperature": 0.15,
        "max_tokens": min(config.max_tokens, 16000),
        **config.params,
    }
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    resp = requests.post(f"{config.base_url}/chat/completions", json=payload,
                         headers=headers, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"].get("content") or ""
    obj = _extract_json(content)
    if not obj or not isinstance(obj.get("groups"), list):
        raise ValueError("模型未返回可解析的合并组 JSON")
    return obj["groups"]
