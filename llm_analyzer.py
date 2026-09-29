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
from collections import deque
from datetime import datetime
from email.utils import parsedate_to_datetime

import requests

log = logging.getLogger("llm_analyzer")
_profile_request_lock = threading.Lock()
_profile_last_request_at = 0.0
_profile_token_windows: dict[str, deque] = {}

DEFAULT_MODEL = "qwen3-8b"
DEFAULT_TIMEOUT = 180         # 推理模型较慢，给足超时
DEFAULT_MAX_TOKENS = 32768    # 单次输出上限；部分模型将 reasoning 计入此限制
MAX_RETRY = 2                 # 解析失败/网络错误的重试次数
MAX_SPLIT_DEPTH = 3           # 二分拆分递归深度上限
ACTIONS = ("move_to_existing", "create_new", "skip")
TITLE_MAX = 120
DESC_MAX = 200


def _extract_json(text: str):
    """Extract the first complete JSON object, including nested objects and fenced replies."""
    if not text:
        return None
    candidates = [match.group(1).strip() for match in
                  re.finditer(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.I)]
    candidates.append(text.strip())
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        for start, char in enumerate(candidate):
            if char != "{":
                continue
            depth = 0
            in_string = False
            escaped = False
            for end in range(start, len(candidate)):
                current = candidate[end]
                if in_string:
                    if escaped:
                        escaped = False
                    elif current == "\\":
                        escaped = True
                    elif current == '"':
                        in_string = False
                    continue
                if current == '"':
                    in_string = True
                elif current == "{":
                    depth += 1
                elif current == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            parsed = json.loads(candidate[start:end + 1])
                            if isinstance(parsed, dict):
                                return parsed
                        except Exception:
                            break
    return None


class LLMConfig:
    """模型连接配置。可从 dict 或 config.json 构建。"""

    def __init__(self, base_url: str = "", api_key: str = "",
                 model: str = DEFAULT_MODEL, params: dict | None = None,
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 context_window_tokens: int = 32768):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model or DEFAULT_MODEL
        self.params = params or {}
        requested_max_tokens = int(max_tokens or DEFAULT_MAX_TOKENS)
        self.max_tokens = cap_completion_tokens(
            self.base_url, self.model, requested_max_tokens)
        self.context_window_tokens = max(4096, int(context_window_tokens or 32768))

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)


def build_chat_completion_payload(config: LLMConfig, messages: list[dict],
                                  max_tokens: int, temperature: float | None = None,
                                  extra: dict | None = None) -> dict:
    """Build a chat payload and adapt Xiaomi MiMo's documented field names."""
    effective_max_tokens = cap_completion_tokens(
        config.base_url, config.model, min(int(max_tokens), config.max_tokens))
    payload = {
        "model": config.model,
        "messages": messages,
        **({"temperature": temperature} if temperature is not None else {}),
        **config.params,
        "max_tokens": effective_max_tokens,
        **(extra or {}),
    }
    if "xiaomimimo.com" in config.base_url.lower():
        # MiMo documents max_completion_tokens and thinking.type instead of the
        # generic max_tokens / enable_thinking / reasoning_effort parameters.
        disabled = payload.pop("enable_thinking", None) is False
        payload.pop("reasoning_effort", None)
        payload.pop("max_tokens", None)
        payload["max_completion_tokens"] = effective_max_tokens
        payload["thinking"] = {"type": "disabled" if disabled else "enabled"}
        if not disabled:
            payload.pop("temperature", None)
            payload.pop("top_p", None)
    return payload


def _provider_completion_limit(base_url: str, model: str) -> int | None:
    """Return a documented per-request output cap for known provider models."""
    host = (base_url or "").lower()
    if "api.deepseek.com" in host:
        # DeepSeek Chat Completions documents max_tokens <= 393,216.
        return 393216
    if "xiaomimimo.com" not in (base_url or "").lower():
        return None
    model_name = (model or "").lower().strip()
    if model_name == "mimo-v2.5":
        return 32768
    if model_name.startswith("mimo-"):
        # MiMo's Chat Completions API documents 131,072 as the maximum for
        # v2.6 models and v2.5-pro. Keep this cap for other MiMo model IDs too
        # until their model metadata provides a more specific limit.
        return 131072
    return None


def cap_completion_tokens(base_url: str, model: str, requested: int) -> int:
    """Clamp a requested output budget to a known provider/model hard limit."""
    requested = max(1, int(requested))
    provider_limit = _provider_completion_limit(base_url, model)
    return min(requested, provider_limit) if provider_limit else requested


class ContextBudgetError(ValueError):
    """A single request cannot fit safely in the configured model context."""


class ContextBatchNeedsSplit(ContextBudgetError):
    """The request fits after the batch is split into smaller calls."""

    def __init__(self, prompt_tokens: int, requested_output: int, context_tokens: int):
        self.prompt_tokens = int(prompt_tokens)
        self.requested_output = int(requested_output)
        self.context_tokens = int(context_tokens)
        super().__init__(
            f"批次预估输入 {self.prompt_tokens} + 输出 {self.requested_output} "
            f"超过上下文窗口 {self.context_tokens}")


class LLMAnalyzer:
    def __init__(self, config: LLMConfig, folders: list[str] | None = None,
                 folder_profiles: list[dict] | None = None,
                 on_context_split=None,
                 default_folders: list[dict] | None = None):
        self.config = config
        self.folders = list(folders or [])
        self.folder_contexts = {
            str(row.get("id")): row for row in (folder_profiles or [])
            if isinstance(row, dict) and row.get("id")
        }
        self.folder_names_by_id = {
            str(row.get("id")): str(row.get("name") or "")
            for row in (folder_profiles or []) if isinstance(row, dict) and row.get("id")
        }
        # 未分拣收件箱（默认收藏夹）：它没有画像，但 _pack 要能把「内容在此」标注出来，
        # 否则模型会把它当成已经归位的内容，只凭一个大杂烩画像就判「留在原处」。
        self.default_folder_ids = {str(row.get("id")) for row in (default_folders or [])
                                   if isinstance(row, dict) and row.get("id")}
        self.default_folder_names = [str(row.get("name") or "")
                                     for row in (default_folders or [])]
        for row in (default_folders or []):
            if isinstance(row, dict) and row.get("id"):
                self.folder_names_by_id.setdefault(str(row["id"]), str(row.get("name") or ""))
        self._local = threading.local()
        self._sys = None
        self.on_context_split = on_context_split

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
            profile_rows = []
            for folder_id, row in self.folder_contexts.items():
                profile_rows.append({
                    "id": folder_id,
                    "name": str(row.get("name", ""))[:80],
                    "summary": str(row.get("summary", "")),
                    "topics": row.get("topics") or [],
                    "typical_content": row.get("typical_content") or [],
                    "out_of_scope": row.get("out_of_scope") or [],
                    "coherence": str(row.get("coherence", "")),
                    "confidence": row.get("confidence"),
                })
            profile_context = json.dumps(profile_rows, ensure_ascii=False, separators=(",", ":"))
            inbox_label = "、".join(n for n in self.default_folder_names if n) or "默认收藏夹"
            inbox_note = (f"「{inbox_label}」——里面的条目**尚未归类**，需要指派到下面有画像的收藏夹"
                          if self.default_folder_names else "（本次没有收件箱条目）")
            self._sys = (
                "你是 B 站收藏夹整理助手。任务是判断一批视频各自**归属于哪个收藏夹**。\n"
                "\n"
                f"【用户现有收藏夹】（共 {len(self.folders)} 个）\n{fl}\n"
                "\n【当前有效的收藏夹画像】（所有候选夹均有完整画像，列表不遗漏）\n"
                f"{profile_context if profile_rows else '（暂无最新画像）'}\n"
                f"\n【未分拣收件箱】{inbox_note}\n"
                "\n"
                "【输入】一个 JSON 对象：\n"
                '{"videos":[{"id":1,"title":"...","desc":"...","upper":"...",'
                '"in_default_inbox":true,'
                '"current_folders":[{"id":"...","name":"...",'
                '"profile":{"summary":"...","topics":[],"typical_content":[],'
                '"out_of_scope":[],"coherence":"...","confidence":0.0}}]}, ...]}\n'
                "\n"
                "【输出】必须是**纯 JSON**，不要任何额外文字：\n"
                '{"results":[{"id":1,"action":"move_to_existing|create_new|skip",'
                '"recommended":"收藏夹名","reason":"一句话理由","confidence":0.9,'
                '"profile_mismatch_folder_ids":["..."],"profile_mismatch_reason":"..."}, ...]}\n'
                "\n"
                "【规则】\n"
                "1. 每个输入 id 必须有一条对应结果，id 原样返回，不得遗漏、不得新增。\n"
                "2. 归类以画像的实际主题和收纳范围为**主要依据**，收藏夹名称只作弱提示；"
                "不得只凭名称判断内容适合某个夹。先在全部候选画像里比对，选出最合适的一个作为 recommended，"
                "action=move_to_existing。\n"
                "3. action=move_to_existing 且 recommended 就是它已经所在的夹时，表示「留在原位、不产生移动」。"
                "想表达「留下」时也要给出 recommended，不要用 skip 表达「留下」。\n"
                f"4. in_default_inbox=true 表示这条内容还躺在「{inbox_label}」里**等待分拣**。"
                "收件箱只是临时入口，不是归属：必须在上面有画像的收藏夹中挑出最合适的一个；"
                "只有所有画像都明显不符、且新主题清楚时才 action=create_new；"
                "确实无处可去时才 action=skip。不要因为「暂时放在收件箱也行」就跳过。\n"
                "5. 内容已经在一个或多个有画像的收藏夹里时，若其中一个夹的画像清楚表明适合留下，"
                "recommended 填那个夹名。只有内容明显偏离当前画像、且其他夹有充分依据时才建议移出；"
                "去向不明确时保留原位，不要为了整理而移动。\n"
                "6. 仅当标题/简介/UP主 全空或完全无法归类 → action=skip。\n"
                "7. confidence 为 0~1 的把握程度。\n"
                "8. 仅当输入条目提供了 current_folders.profile 时，才检查它是否明显偏离该夹画像；"
                "profile_mismatch_folder_ids 只能填本条 current_folders 中有 profile 的原始 ID。"
                "证据不明确时留空，不要因为收藏夹名称不贴合就判定偏离。\n"
                "9. profile_mismatch_reason 简短说明证据；没有偏离时留空。\n"
                "10. 理由务必简短（不超过 20 字），不要展开分析，避免浪费输出长度。\n"
                f"11.「{inbox_label}」只能移出，绝不能作为移入目标：recommended 永远不能填它，"
                "action=create_new 也不能新建同名收藏夹。\n"
                "只返回 JSON。"
            )
        return self._sys

    def _pack(self, videos: list[dict]) -> list[dict]:
        """把批量视频压成请求用的紧凑结构（局部 id = 1..N）。"""
        out = []
        for i, v in enumerate(videos, start=1):
            item = {"id": i, "title": (v.get("title") or "")[:TITLE_MAX]}
            d = (v.get("desc") or "").strip()
            if d:
                item["desc"] = d[:DESC_MAX]
            if v.get("upper_name"):
                item["upper"] = v["upper_name"]
            current_folders = []
            memberships = {str(x) for x in (v.get("folder_ids") or [])}
            if v.get("source_folder_id"):
                memberships.add(str(v["source_folder_id"]))
            for folder_id in sorted(memberships):
                name = self.folder_names_by_id.get(folder_id)
                if not name:
                    continue
                folder = {"id": folder_id, "name": name}
                profile = self.folder_contexts.get(folder_id)
                if profile:
                    folder["profile"] = {
                        "summary": str(profile.get("summary", "")),
                        "topics": profile.get("topics") or [],
                        "typical_content": profile.get("typical_content") or [],
                        "out_of_scope": profile.get("out_of_scope") or [],
                        "coherence": str(profile.get("coherence", "")),
                        "confidence": profile.get("confidence"),
                    }
                current_folders.append(folder)
            if current_folders:
                item["current_folders"] = current_folders
            # 待分拣标记：内容还躺在默认收藏夹（收件箱）里，需要被指派归属。
            if memberships & self.default_folder_ids:
                item["in_default_inbox"] = True
            out.append(item)
        return out

    # ---------- 单次调用 ----------
    def _prepare_call(self, videos: list[dict]) -> dict:
        """Estimate the complete request before sending and reserve output/context headroom."""
        packed_videos = self._pack(videos)
        system = self._system_prompt()
        user_text = json.dumps({"videos": packed_videos}, ensure_ascii=False)
        prompt_tokens = _estimate_tokens(system) + _estimate_tokens(user_text) + 16
        context_tokens = self.config.context_window_tokens
        safety_tokens = max(512, int(context_tokens * 0.15 + 0.999))
        requested_output = min(self.config.max_tokens, 3000 + 500 * len(videos))
        available_output = context_tokens - prompt_tokens - safety_tokens
        if available_output < 256:
            if len(videos) > 1:
                raise ContextBatchNeedsSplit(prompt_tokens, requested_output, context_tokens)
            raise ContextBudgetError(
                f"单条内容的预检请求无法安全放入模型上下文窗口（估算输入 {prompt_tokens}，"
                f"上下文 {context_tokens}，安全余量 {safety_tokens}）；未发送请求")
        if requested_output > available_output:
            if len(videos) > 1:
                raise ContextBatchNeedsSplit(prompt_tokens, requested_output, context_tokens)
            # 最大输出是上限。对单条请求按剩余上下文自动下调，仍保留安全余量。
            max_tokens = available_output
        else:
            max_tokens = requested_output
        return {"packed_videos": packed_videos, "system": system,
                "user_text": user_text, "max_tokens": max_tokens,
                "prompt_tokens": prompt_tokens, "safety_tokens": safety_tokens}

    def _call(self, videos: list[dict], prepared: dict | None = None):
        """调用一次模型。

        返回 (结果字典 bvid->result, 缺失视频列表, 原因)
        原因: "ok" | "partial" | "empty" | "parse" | "truncated" | "error"
        """
        n = len(videos)
        if n == 0:
            return {}, [], "ok"
        prepared = prepared or self._prepare_call(videos)
        max_tokens = prepared["max_tokens"]
        payload = build_chat_completion_payload(
            self.config,
            messages=[
                {"role": "system", "content": prepared["system"]},
                {"role": "user", "content": prepared["user_text"]},
            ],
            temperature=0.2,
            max_tokens=max_tokens,
        )
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
                choice = data["choices"][0]
                if choice.get("finish_reason") == "length":
                    log.warning("响应因 max_tokens 截断(batch=%d, max_tokens=%d)，将拆分", n, max_tokens)
                    return {}, list(videos), "truncated"
                content = choice["message"].get("content") or ""
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
                    # 默认收藏夹永远不是归类目的地。若模型误把它作为去向，降级为无操作，
                    # 避免前端把它误判成“新建收藏夹”或服务端发出移入请求。
                    if rec and (rec in self.default_folder_names or rec == "默认收藏夹"):
                        action = "skip"
                        rec = ""
                    try:
                        conf = float(r.get("confidence", 0) or 0)
                    except Exception:
                        conf = 0.0
                    current_ids = {str(x.get("id")) for x in prepared["packed_videos"][rid - 1].get("current_folders", [])}
                    checked_ids = current_ids.intersection(self.folder_contexts)
                    raw_mismatch_ids = r.get("profile_mismatch_folder_ids")
                    profile_check_returned = isinstance(raw_mismatch_ids, list)
                    if not profile_check_returned:
                        raw_mismatch_ids = []
                    mismatch_ids = sorted(checked_ids.intersection(str(x) for x in raw_mismatch_ids))
                    result = {
                        "bvid": bvid,
                        "recommended": rec,
                        "action": action,
                        "reason": str(r.get("reason", "")).strip(),
                        "confidence": conf,
                    }
                    if checked_ids and profile_check_returned:
                        result["profile_checked_folder_ids"] = sorted(checked_ids)
                        result["profile_checked_versions"] = {
                            folder_id: str(self.folder_contexts[folder_id].get("revision", ""))
                            for folder_id in sorted(checked_ids)}
                        result["profile_mismatch_folder_ids"] = mismatch_ids
                        result["profile_mismatch_reason"] = str(r.get("profile_mismatch_reason", "")).strip()[:160]
                    out[bvid] = result
                missing = [v for v in videos if v.get("bvid") not in out]
                return out, missing, ("ok" if not missing else "partial")
            except requests.HTTPError as e:
                if _context_limit_error(e):
                    log.warning("供应商拒绝请求：估算上下文可能超过实际限制(batch=%d)，将拆分", n)
                    return {}, list(videos), "context"
                reason = "error"
                log.warning("调用失败(batch=%d): %s", n, e)
                time.sleep(0.5)
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
        try:
            prepared = self._prepare_call(videos)
        except ContextBatchNeedsSplit as exc:
            if self.on_context_split:
                try:
                    self.on_context_split(len(videos), exc.prompt_tokens,
                                          exc.requested_output, exc.context_tokens)
                except Exception:
                    log.debug("Context split callback failed", exc_info=True)
            mid = len(videos) // 2
            combined = {}
            for part in (videos[:mid], videos[mid:]):
                try:
                    combined.update(self.analyze_batch(part, _depth + 1))
                except ContextBudgetError as err:
                    log.error("单批上下文预算保护阻止发送: %s", err)
            return combined
        got, missing, reason = self._call(videos, prepared)
        if not missing:
            return got
        if len(missing) == 1:
            # 解析失败/网络错误可再试一次；"空正文"重试无用，直接放弃
            if reason in ("parse", "error"):
                got2, _, _ = self._call(missing)
                got.update(got2)
            return got
        if len(missing) > 1 and reason in ("truncated", "context") and self.on_context_split:
            try:
                self.on_context_split(len(missing), prepared["prompt_tokens"],
                                      prepared["max_tokens"], self.config.context_window_tokens)
            except Exception:
                log.debug("Context split callback failed", exc_info=True)
        if _depth >= MAX_SPLIT_DEPTH and reason not in ("truncated", "context"):
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
        "请以每个收藏夹的 current_content_profile（由完整扫描生成）作为主要证据，再参考代表视频、收藏数量和跨夹重复数 overlaps；"
        "收藏夹名称仅作弱提示，不得仅凭名称判断主题。画像与代表内容冲突时降低把握，不要机械照搬画像。\n"
        "可推荐的情形：\n"
        "1. synonym：名称近义且内容方向一致。\n"
        "2. content_overlap：代表内容、关键对象或实际收藏明显交叉。\n"
        "3. natural_parent：多个较小子主题能形成自然、清晰、不过度宽泛的上位分类，且分开保留没有稳定的检索价值。\n"
        "不得推荐：仅同属一个大领域；合并后只能使用空泛名称；各夹内容较多且边界清晰；"
        "专业主题会被普通大类吞没；只是受众相同或处于同一应用链条。默认收藏夹不参与。\n"
        "建议分级：high 表示合并价值明确（confidence>=0.82）；medium 表示结构上合理但需人工判断（0.68~0.82）；"
        "低于 0.68 不输出。样本不足时不得给 high，但可以给 medium 候选。\n"
        "每个收藏夹最多出现在一组。目标夹选内容更完整、名称更具代表性或数量更多的夹。"
        "final_name 应准确覆盖全组，一般为 2~10 个字。reason 写具体依据，risk 写可能损失的分类边界；"
        "profile_evidence 简短说明画像如何支持或反对该合并。所有输入夹都带有完整画像字段（简介、主题、典型内容、范围外、连贯性、把握度）；"
        "以这些实际内容为主要证据，夹名只作弱提示。\n"
        "只返回纯 JSON："
        '{"groups":[{"final_name":"...","target_id":"...","source_ids":["..."],'
        '"level":"high|medium","merge_type":"synonym|content_overlap|natural_parent",'
        '"reason":"...","risk":"...","profile_evidence":"...","confidence":0.82}]}\n'
        "target_id 和 source_ids 必须原样使用输入 id，两者不得重复。没有合适建议时返回 {\"groups\":[]}。"
    )
    payload = build_chat_completion_payload(
        config,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps({"folders": folder_profiles}, ensure_ascii=False)},
        ],
        temperature=0.15,
        max_tokens=min(config.max_tokens, 16000),
    )
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


def _estimate_tokens(text: str) -> int:
    """Conservative rough estimate for mixed Chinese/ASCII text without model tokenizer access."""
    cjk = ascii_word = punctuation = whitespace = other = 0
    for char in text:
        cp = ord(char)
        if (0x2E80 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF or
                0x20000 <= cp <= 0x2FA1F or 0x3040 <= cp <= 0x30FF or
                0xAC00 <= cp <= 0xD7AF):
            cjk += 1
        elif char.isascii():
            if char.isalnum():
                ascii_word += 1
            elif char.isspace():
                whitespace += 1
            else:
                punctuation += 1
        else:
            other += 1
    return int(cjk * 1.2 + ascii_word / 3.2 + punctuation * 0.7 +
               whitespace / 8 + other * 1.2 + 0.999)


def _context_limit_error(exc: requests.HTTPError) -> bool:
    response = getattr(exc, "response", None)
    detail = " ".join((str(exc), getattr(response, "text", "") or "")).lower()
    return any(marker in detail for marker in (
        "context_length_exceeded", "maximum context length", "context length",
        "context window", "context limit", "too many tokens", "maximum number of tokens",
        "maximum input length", "prompt is too long", "reduce the length",
        "input token limit", "request too large",
    ))


class ProfileRateLimitError(RuntimeError):
    """Profile generation exhausted bounded retries after repeated HTTP 429 responses."""


class ProfileOutputTruncatedError(ValueError):
    """The provider stopped generation before returning a complete profile JSON object."""


class ProfileCancelledError(RuntimeError):
    """The user requested cancellation of a folder profile run."""


def _check_profile_cancelled(should_stop) -> None:
    if should_stop and should_stop():
        raise ProfileCancelledError("已按停止请求中止收藏夹画像任务")


def _profile_sleep(delay: float, should_stop=None) -> None:
    deadline = time.monotonic() + max(0.0, float(delay or 0))
    while True:
        _check_profile_cancelled(should_stop)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.5, remaining) if should_stop else remaining)


def _profile_chunks(items: list[dict], system: str, base: dict, field: str,
                    input_budget: int) -> list[list[dict]]:
    """Split all source entries into context-safe pages without dropping any entry."""
    fixed = dict(base)
    fixed[field] = []
    fixed_cost = _estimate_tokens(system) + _estimate_tokens(json.dumps(fixed, ensure_ascii=False))
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_cost = fixed_cost
    for item in items:
        item_cost = _estimate_tokens(json.dumps(item, ensure_ascii=False)) + 1
        if current and current_cost + item_cost > input_budget:
            chunks.append(current)
            current = []
            current_cost = fixed_cost
        if current_cost + item_cost > input_budget:
            raise ValueError("模型上下文窗口过小，无法容纳单条内容或压缩摘要")
        current.append(item)
        current_cost += item_cost
    if current:
        chunks.append(current)
    return chunks


def _wait_for_profile_request_slot(interval: float, request_tokens: int,
                                   tpm_limit: int, rate_key: str,
                                   throttle_callback=None,
                                   should_stop=None) -> list:
    """Enforce a request gap and a conservative rolling 60-second token budget."""
    global _profile_last_request_at
    interval = max(0.0, float(interval or 0))
    rolling_budget = max(1, int(int(tpm_limit) * 0.9))
    if request_tokens > rolling_budget:
        raise ValueError(
            f"单次画像请求估算 {request_tokens} tokens，超过配置 TPM 安全预算 {rolling_budget}；"
            "请调低画像上下文窗口或检查 TPM 设置")
    while True:
        _check_profile_cancelled(should_stop)
        with _profile_request_lock:
            window = _profile_token_windows.setdefault(rate_key, deque())
            now = time.monotonic()
            while window and now - window[0][0] >= 60:
                window.popleft()
            used = sum(entry[1] for entry in window)
            interval_wait = max(0.0, interval - (now - _profile_last_request_at))
            token_wait = 0.0
            if used + request_tokens > rolling_budget and window:
                token_wait = max(0.0, 60.0 - (now - window[0][0]))
            wait = max(interval_wait, token_wait)
            if wait <= 0:
                reservation = [now, request_tokens]
                window.append(reservation)
                _profile_last_request_at = now
                return reservation
        if throttle_callback and wait >= 5:
            try:
                reason = "TPM 滚动窗口" if token_wait >= interval_wait else "请求间隔"
                throttle_callback(wait, reason)
            except Exception:
                log.debug("Profile throttle callback failed", exc_info=True)
        _profile_sleep(wait, should_stop)


def _reconcile_profile_usage(reservation: list, usage: dict) -> None:
    """Replace a successful request's worst-case reservation with provider usage."""
    try:
        total = int(usage.get("prompt_tokens", 0) or 0) + int(
            usage.get("completion_tokens", 0) or 0)
    except (TypeError, ValueError):
        return
    if total <= 0:
        return
    with _profile_request_lock:
        reservation[1] = max(1, int(total * 1.05 + 0.999))


def _retry_after_seconds(response, attempt: int) -> float:
    raw = (getattr(response, "headers", {}) or {}).get("Retry-After", "")
    try:
        if raw:
            return max(1.0, min(300.0, float(raw)))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(raw)
            now = datetime.now(retry_at.tzinfo) if retry_at.tzinfo else datetime.now()
            return max(1.0, min(300.0, (retry_at - now).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            pass
    return min(60.0, 2.0 ** (attempt + 1))


def _profile_retry_delay(response, attempt: int) -> float:
    """Wait at least one minute before the first retry of a transient failure."""
    delay = _retry_after_seconds(response, attempt)
    return max(60.0, delay) if attempt == 0 else delay


def _provider_error_detail(response) -> tuple[str, str]:
    try:
        data = response.json()
    except Exception:
        data = {}
    message = data.get("message") if isinstance(data, dict) else ""
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        error = data["error"]
        message = error.get("message") or message
        detail = error.get("param") or error.get("detail")
        if detail and str(detail) not in str(message):
            message = f"{message}（{detail}）" if message else detail
        code = error.get("code")
        if code and str(code) not in str(message):
            message = f"{code}: {message}" if message else code
    trace_headers = getattr(response, "headers", {}) or {}
    trace_id = (trace_headers.get("x-siliconcloud-trace-id") or
                trace_headers.get("x-request-id") or trace_headers.get("x-trace-id") or "")
    return str(message or getattr(response, "text", "") or "").strip()[:500], str(trace_id)


def _notify_profile_retry(callback, attempt: int, delay: float, detail: str) -> None:
    if callback:
        try:
            callback(attempt, 8, delay, detail[:300])
        except Exception:
            log.debug("Profile retry callback failed", exc_info=True)


def _profile_chat(config: LLMConfig, system: str, user: dict, max_tokens: int,
                  request_interval: float = 2.0,
                  tpm_limit_tokens: int = 20000,
                  usage_callback=None, retry_callback=None,
                  throttle_callback=None, should_stop=None,
                  request_callback=None) -> dict:
    user_text = json.dumps(user, ensure_ascii=False)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_text},
    ]
    # Profile pages need compact, machine-readable JSON. These SiliconFlow model
    # families support explicitly disabling thinking for this structured task.
    siliconflow = "siliconflow.cn" in config.base_url.lower()
    qwen3 = "qwen3" in config.model.lower()
    deepseek_v4_flash = "deepseek-v4-flash" in config.model.lower()
    extra = {}
    if siliconflow and (qwen3 or deepseek_v4_flash):
        extra["enable_thinking"] = False
    if siliconflow and deepseek_v4_flash:
        extra["response_format"] = {"type": "json_object"}
    payload = build_chat_completion_payload(
        config, messages, max_tokens, temperature=0.2, extra=extra)
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    prompt_tokens_estimate = _estimate_tokens(system) + _estimate_tokens(user_text)
    request_tokens_estimate = int((prompt_tokens_estimate + max_tokens) * 1.1 + 0.999)
    rate_key = f"{config.base_url.lower()}|{config.model}"
    response = None
    for attempt in range(8):
        _check_profile_cancelled(should_stop)
        reservation = _wait_for_profile_request_slot(
            request_interval, request_tokens_estimate, tpm_limit_tokens,
            rate_key, throttle_callback, should_stop)
        try:
            if request_callback:
                request_callback()
            response = requests.post(f"{config.base_url}/chat/completions", json=payload,
                                     headers=headers, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            if attempt == 7:
                raise
            delay = 60.0 if attempt == 0 else min(60.0, 2.0 ** (attempt + 1))
            _notify_profile_retry(retry_callback, attempt + 2, delay,
                                  f"请求未收到 HTTP 响应：{str(exc)}")
            log.warning("Profile request failed before an HTTP response: %s; retrying after %.1f seconds",
                        str(exc)[:300], delay)
            _profile_sleep(delay, should_stop)
            continue
        if response.status_code == 429:
            delay = _profile_retry_delay(response, attempt)
            detail, trace_id = _provider_error_detail(response)
            if attempt == 7:
                raise ProfileRateLimitError(
                    f"模型接口连续限流（HTTP 429）：{detail or '服务商未返回具体原因'}；"
                    f"trace_id={trace_id or '无'}；建议等待约 {int(delay)} 秒后重新运行")
            _notify_profile_retry(retry_callback, attempt + 2, delay,
                                  f"HTTP 429：{detail or '服务商未返回具体原因'}")
            log.warning("Profile request rate-limited: %s; trace_id=%s; retrying after %.1f seconds",
                        detail or "no provider detail", trace_id or "none", delay)
            _profile_sleep(delay, should_stop)
            continue
        if response.status_code in (408, 425) or 500 <= response.status_code <= 599:
            if attempt == 7:
                detail, trace_id = _provider_error_detail(response)
                raise ValueError(
                    f"模型接口返回 HTTP {response.status_code}："
                    f"{detail or '服务商未返回具体原因'}；trace_id={trace_id or '无'}")
            delay = _profile_retry_delay(response, attempt)
            detail, trace_id = _provider_error_detail(response)
            _notify_profile_retry(retry_callback, attempt + 2, delay,
                                  f"HTTP {response.status_code}：{detail or '临时服务错误'}")
            log.warning("Profile API returned transient HTTP %d: %s; trace_id=%s; retrying after %.1f seconds",
                        response.status_code, detail or "no provider detail",
                        trace_id or "none", delay)
            _profile_sleep(delay, should_stop)
            continue
        if not response.ok:
            detail, trace_id = _provider_error_detail(response)
            raise ValueError(
                f"模型接口返回 HTTP {response.status_code}："
                f"{detail or '服务商未返回具体原因'}；trace_id={trace_id or '无'}")
        break
    result = response.json()
    usage = result.get("usage") or {}
    if isinstance(usage, dict):
        _reconcile_profile_usage(reservation, usage)
    if usage_callback and isinstance(usage, dict):
        try:
            usage_callback(int(usage.get("prompt_tokens", 0) or 0),
                           int(usage.get("completion_tokens", 0) or 0))
        except Exception:
            log.debug("Profile usage callback failed", exc_info=True)
    choice = (result.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "\n".join(str(item.get("text", "")) for item in content
                             if isinstance(item, dict))
    obj = _extract_json(content)
    if not isinstance(obj, dict):
        snippet = str(content or "").strip().replace("\n", " ")[:240]
        finish = choice.get("finish_reason") or "unknown"
        if not snippet and message.get("reasoning_content"):
            snippet = "仅返回 reasoning_content，未返回正文 content"
        used_tokens = usage.get("completion_tokens", "未知") if isinstance(usage, dict) else "未知"
        message = (f"模型未返回可解析的画像压缩 JSON (finish_reason={finish}, "
                   f"输出预算={max_tokens}, 实际输出={used_tokens} tokens, "
                   f"内容片段={snippet or '空'})")
        if finish == "length":
            raise ProfileOutputTruncatedError(message)
        raise ValueError(message)
    return obj


def _clean_profile_digest(obj: dict, items_covered: int) -> dict:
    raw = obj.get("digest") if isinstance(obj.get("digest"), dict) else obj

    themes = []
    for entry in raw.get("themes", []) if isinstance(raw.get("themes"), list) else []:
        if isinstance(entry, str):
            entry = {"theme": entry}
        if not isinstance(entry, dict):
            continue
        theme = str(entry.get("theme", "")).strip()
        if not theme:
            continue
        examples = entry.get("examples", [])
        themes.append({
            "theme": theme[:100],
            "evidence": str(entry.get("evidence", "")).strip()[:240],
            "examples": [str(x).strip()[:120] for x in examples
                         if str(x).strip()][:3] if isinstance(examples, list) else [],
        })
        if len(themes) >= 8:
            break

    outliers = []
    for entry in raw.get("outliers", []) if isinstance(raw.get("outliers"), list) else []:
        if isinstance(entry, str):
            entry = {"title": entry}
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title", "")).strip()
        if title:
            outliers.append({"title": title[:120],
                             "reason": str(entry.get("reason", "")).strip()[:180]})
        if len(outliers) >= 6:
            break
    notes = raw.get("notes", [])
    return {
        "items_covered": int(items_covered),
        "themes": themes,
        "outliers": outliers,
        "notes": [str(x).strip()[:180] for x in notes if str(x).strip()][:6]
                 if isinstance(notes, list) else [],
    }


def _compress_profile_pages(config: LLMConfig, folder: dict, items: list[dict],
                             *, field: str, system: str, output_tokens: int,
                             input_budget: int, context_window_tokens: int,
                             content_count: int,
                             stage: str, request_interval: float,
                             tpm_limit_tokens: int,
                             usage_callback=None,
                             retry_callback=None,
                             throttle_callback=None,
                             progress_callback=None,
                             should_stop=None,
                             request_callback=None) -> list[dict]:
    base = {"folder_name": folder.get("title", ""),
            "folder_item_count": int(content_count)}
    pending = [(page, output_tokens, 0)
               for page in _profile_chunks(items, system, base, field, input_budget)]
    planned = len(pending)
    completed = 0
    compressed: list[dict] = []
    while pending:
        _check_profile_cancelled(should_stop)
        page, page_output_tokens, output_retries = pending.pop(0)
        user = {**base, field: page}
        try:
            result = _profile_chat(config, system, user, page_output_tokens,
                                   request_interval, tpm_limit_tokens, usage_callback,
                                   retry_callback, throttle_callback, should_stop,
                                   request_callback)
        except requests.HTTPError as exc:
            if not _context_limit_error(exc) or len(page) <= 1:
                raise
            middle = len(page) // 2
            pending[0:0] = [(page[:middle], page_output_tokens, output_retries),
                            (page[middle:], page_output_tokens, output_retries)]
            planned += 1
            log.warning("Profile page exceeded model context; split %d items", len(page))
            continue
        except ProfileOutputTruncatedError as exc:
            if output_retries >= 2:
                raise ProfileOutputTruncatedError(
                    f"{exc}；已两次缩小批次并提高输出预算，停止当前画像以避免继续消耗 tokens") from exc
            middle = len(page) // 2
            child_pages = [page[:middle], page[middle:]] if middle else [page]
            replacements = []
            for child in child_pages:
                child_text = json.dumps({**base, field: child}, ensure_ascii=False)
                prompt_tokens = _estimate_tokens(system) + _estimate_tokens(child_text)
                tpm_cap = int(tpm_limit_tokens * 0.8) - prompt_tokens
                context_cap = (context_window_tokens - prompt_tokens -
                               max(512, int(context_window_tokens * 0.15)))
                next_output = min(config.max_tokens, 12000, tpm_cap, context_cap,
                                  max(page_output_tokens + 1024, int(page_output_tokens * 1.5)))
                if next_output < 256 or (len(page) == 1 and next_output <= page_output_tokens):
                    raise ProfileOutputTruncatedError(
                        f"{exc}；单条内容仍无法在 TPM/上下文预算内完成模型输出") from exc
                replacements.append((child, next_output, output_retries + 1))
            pending[0:0] = replacements
            planned += len(replacements) - 1
            log.warning("Profile output truncated; retry %d item(s) as %d page(s) with up to %d output tokens",
                        len(page), len(replacements), max(row[1] for row in replacements))
            if progress_callback:
                progress_callback("输出截断，调整批次和输出预算", completed, planned)
            continue

        if field == "items":
            covered = len(page)
        else:
            covered = sum(int(row.get("items_covered", 0) or 0)
                          for row in page if isinstance(row, dict))
        compressed.append(_clean_profile_digest(result, covered))
        completed += 1
        if completed == 1 or completed == planned or completed % 10 == 0:
            log.info("Folder profile compression: %d/%d pages", completed, planned)
            if progress_callback:
                progress_callback(stage, completed, planned)
    return compressed


def generate_folder_profile(config: LLMConfig, folder: dict,
                            samples: list[dict], content_count: int,
                            *, context_window_tokens: int = 32768,
                            tpm_limit_tokens: int = 20000,
                            request_interval: float = 2.0,
                            retry_callback=None,
                            throttle_callback=None,
                            progress_callback=None,
                            should_stop=None) -> dict:
    """Compress every local item in context-safe pages, then synthesize a folder profile."""
    folder_name = str(folder.get("title") or folder.get("name") or "").strip()
    if folder_name == "默认收藏夹":
        profile_policy = (
            "本次画像对象是默认收藏夹。请描述其现有内容适合的主题和明显不适合的内容范围，"
            "并在最终简介中包含规则：默认收藏夹只能移出，不能移入。"
        )
    else:
        profile_policy = (
            "整理规则：默认收藏夹只能移出、不能移入；若本次画像对象不是默认收藏夹，"
            "不要把这条操作规则误写成当前收藏夹的内容主题。"
        )
    map_system = (
        "你负责压缩一页收藏夹条目，给后续画像归纳保留可靠证据。收藏夹名只是弱提示，"
        "必须以条目内容为主。请找出稳定主题和少量明显离群内容；不要把偶然出现的主题夸大，"
        "不要编造条目中没有的信息。所有输入条目都已经计入覆盖数量。"
        f"{profile_policy}\n"
        "只输出 JSON：{"
        '"themes":[{"theme":"主题","evidence":"共同内容依据","examples":["输入中的代表标题"]}],'
        '"outliers":[{"title":"输入中的标题","reason":"为什么明显偏离本页主线"}],'
        '"notes":["其他有价值的观察"]}\n'
        "主题最多 8 个，离群内容最多 6 个，观察最多 6 条。例子必须逐字来自输入标题；"
        "如果没有明显离群内容，outliers 返回空数组。"
    )
    reduce_system = (
        "你负责合并同一个收藏夹的多页压缩摘要。各摘要覆盖的原始条目数以 items_covered 为准，"
        "不得漏掉覆盖范围；合并重复主题，同时保留确实不同的子主题和离群证据。"
        "不能把单页观察误当成整个收藏夹的主线，也不要编造内容。"
        f"{profile_policy}\n"
        "只输出与输入相同结构的 JSON："
        '{"themes":[{"theme":"主题","evidence":"跨页依据","examples":["已有代表标题"]}],'
        '"outliers":[{"title":"已有标题","reason":"偏离依据"}],"notes":["观察"]}\n'
        "themes 最多 8 个，outliers 最多 6 个，notes 最多 6 条。"
    )
    final_system = (
        "你是收藏夹内容画像整理助手。请根据压缩摘要归纳稳定主题，收藏夹名称只是弱提示，"
        "不能为了迎合名称而忽略条目证据。摘要覆盖了文件夹内全部本地条目；"
        "少量离群条目不应决定整体画像，但应反映清晰的内容分歧。不要编造摘要没有的事实。"
        f"{profile_policy}\n"
        "请输出纯 JSON："
        '{"summary":"一到三句的文件夹简介","topics":["主题"],'
        '"typical_content":["适合放入的内容"],"out_of_scope":["明显不适合的内容类型"],'
        '"coherence":"coherent|mixed|insufficient","confidence":0.0}\n'
        "summary 应可直接作为收藏夹简介；topics 2~8 项；typical_content 与 out_of_scope 各 0~5 项。"
        "只有摘要里确实呈现明显内容分歧时才标 mixed；证据不足时标 insufficient。"
        "confidence 为 0~1，表示画像代表性把握，而不是每条内容归类的置信度。"
    )
    direct_system = (
        "你是收藏夹内容画像整理助手。请根据输入的全部条目归纳收藏夹画像，收藏夹名称只是弱提示，"
        "必须以标题、简介、UP主和标签等实际内容为证据。不要逐条复述，不要编造信息；"
        "少量离群内容不应决定整体画像，但应反映清晰的内容分歧。"
        f"{profile_policy}\n"
        "请输出纯 JSON："
        '{"summary":"一到三句的文件夹简介","topics":["主题"],'
        '"typical_content":["适合放入的内容"],"out_of_scope":["明显不适合的内容类型"],'
        '"coherence":"coherent|mixed|insufficient","confidence":0.0}\n'
        "summary 应可直接作为收藏夹简介；topics 2~8 项；typical_content 与 out_of_scope 各 0~5 项。"
        "只有条目呈现明显内容分歧时才标 mixed；证据不足时标 insufficient。"
        "confidence 为 0~1，表示画像代表性把握。"
    )
    context_window_tokens = max(4096, int(context_window_tokens or 32768))
    tpm_limit_tokens = max(1000, int(tpm_limit_tokens or 20000))
    if not samples:
        raise ValueError("收藏夹没有可用于生成画像的本地内容")
    usage_totals = {"prompt": 0, "completion": 0, "calls": 0}

    def collect_usage(prompt_tokens: int, completion_tokens: int) -> None:
        usage_totals["prompt"] += max(0, int(prompt_tokens))
        usage_totals["completion"] += max(0, int(completion_tokens))

    def collect_request() -> None:
        usage_totals["calls"] += 1

    # SiliconFlow Qwen3 can use non-thinking mode. Step-3.5-Flash still emits
    # reasoning_content with enable_thinking=false, so it needs a larger budget.
    compact_output = ("siliconflow.cn" in config.base_url.lower() and
                      "qwen3" in config.model.lower())
    deepseek_v4_flash = ("siliconflow.cn" in config.base_url.lower() and
                         "deepseek-v4-flash" in config.model.lower())
    if deepseek_v4_flash:
        # The digest schema is intentionally short; a large provider TPM should
        # not turn into a 100K+ generation allowance for one summary page.
        map_output_tokens = min(config.max_tokens, 16000,
                                max(256, int(tpm_limit_tokens * 0.4)),
                                max(256, context_window_tokens // 4))
    else:
        map_output_tokens = min(config.max_tokens,
                                max(256, int(tpm_limit_tokens * (0.08 if compact_output else 0.4))),
                                max(256, context_window_tokens // 4))
    map_context_budget = context_window_tokens - map_output_tokens - max(
        512, int(context_window_tokens * 0.15))
    map_tpm_budget = int(tpm_limit_tokens * 0.8) - map_output_tokens
    map_input_budget = min(map_context_budget, map_tpm_budget)
    if map_input_budget <= 0:
        raise ValueError("配置的 TPM 上限不足以容纳画像压缩请求")

    # The final profile is short structured JSON. Reserving 60% of TPM for its
    # output left too little room for the cached page digests and caused needless
    # multi-round reduction. Keep a useful completion budget while giving the
    # final request enough room to combine the summaries in one pass.
    final_output_tokens = min(config.max_tokens,
                              max(256, min(6000, context_window_tokens // 3,
                                           int(tpm_limit_tokens * 0.3))))
    final_context_budget = context_window_tokens - final_output_tokens - max(
        512, int(context_window_tokens * 0.15))
    final_tpm_budget = int(tpm_limit_tokens * 0.8) - final_output_tokens
    final_input_budget = min(final_context_budget, final_tpm_budget)
    if final_input_budget <= 0:
        raise ValueError("配置的 TPM 上限不足以容纳画像总结请求")
    base = {"folder_name": folder.get("title", ""),
            "folder_item_count": int(content_count)}
    direct_output_tokens = map_output_tokens
    direct_input_budget = min(
        context_window_tokens - direct_output_tokens - max(
            512, int(context_window_tokens * 0.15)),
        int(tpm_limit_tokens * 0.8) - direct_output_tokens)
    direct_user = {**base, "items": samples}
    direct_size = (_estimate_tokens(direct_system) +
                   _estimate_tokens(json.dumps(direct_user, ensure_ascii=False)))
    obj = None
    if direct_input_budget > 0 and direct_size <= direct_input_budget:
        _check_profile_cancelled(should_stop)
        if progress_callback:
            progress_callback("单次请求生成画像", 1, 1)
        try:
            obj = _profile_chat(config, direct_system, direct_user,
                                direct_output_tokens, request_interval,
                                tpm_limit_tokens, collect_usage,
                                retry_callback, throttle_callback, should_stop, collect_request)
        except ProfileOutputTruncatedError:
            if progress_callback:
                progress_callback("单次输出截断，切换分批压缩", 0, 1)
        except requests.HTTPError as exc:
            if not _context_limit_error(exc):
                raise
            if progress_callback:
                progress_callback("单次请求超出模型上下文，切换分批压缩", 0, 1)

    if obj is None:
        summaries = _compress_profile_pages(
            config, folder, samples, field="items", system=map_system,
            output_tokens=map_output_tokens, input_budget=map_input_budget,
            context_window_tokens=context_window_tokens,
            content_count=content_count, stage="分批压缩内容",
            request_interval=request_interval,
            tpm_limit_tokens=tpm_limit_tokens,
            usage_callback=collect_usage,
            retry_callback=retry_callback,
            throttle_callback=throttle_callback,
            progress_callback=progress_callback,
            should_stop=should_stop,
            request_callback=collect_request)

        reduction_round = 0
        output_truncation_retries = 0
        while True:
            _check_profile_cancelled(should_stop)
            final_user = {**base, "compressed_summaries": summaries}
            final_size = (_estimate_tokens(final_system) +
                          _estimate_tokens(json.dumps(final_user, ensure_ascii=False)))
            if final_size <= final_input_budget:
                try:
                    obj = _profile_chat(config, final_system, final_user,
                                        final_output_tokens, request_interval,
                                        tpm_limit_tokens, collect_usage,
                                        retry_callback, throttle_callback, should_stop,
                                        collect_request)
                    break
                except (requests.HTTPError, ProfileOutputTruncatedError) as exc:
                    if isinstance(exc, ProfileOutputTruncatedError):
                        if output_truncation_retries >= 1:
                            raise
                        output_truncation_retries += 1
                    if (not isinstance(exc, ProfileOutputTruncatedError) and
                            not _context_limit_error(exc)):
                        raise
            reduced = _compress_profile_pages(
                    config, folder, summaries, field="summaries", system=reduce_system,
                    output_tokens=map_output_tokens, input_budget=map_input_budget,
                    context_window_tokens=context_window_tokens,
                    content_count=content_count, stage="合并压缩摘要",
                    request_interval=request_interval,
                    tpm_limit_tokens=tpm_limit_tokens,
                    usage_callback=collect_usage,
                    retry_callback=retry_callback,
                    throttle_callback=throttle_callback,
                    progress_callback=progress_callback,
                    should_stop=should_stop,
                    request_callback=collect_request)
            old_size = _estimate_tokens(json.dumps(summaries, ensure_ascii=False))
            new_size = _estimate_tokens(json.dumps(reduced, ensure_ascii=False))
            reduction_round += 1
            if ((len(reduced) >= len(summaries) and new_size >= old_size) or
                    reduction_round >= 2):
                raise ValueError("画像摘要无法继续压缩到模型上下文窗口内")
            summaries = reduced
    profile = obj.get("profile") if isinstance(obj.get("profile"), dict) else obj
    summary = str(profile.get("summary", "")).strip()
    if not summary:
        raise ValueError("模型返回的收藏夹画像缺少 summary")
    if folder_name == "默认收藏夹" and "默认收藏夹只能移出" not in summary:
        summary = summary[:1100].rstrip("。；， ") + "。默认收藏夹只能移出，不能移入。"
    for key in ("topics", "typical_content", "out_of_scope"):
        if not isinstance(profile.get(key), list):
            raise ValueError(f"模型返回的收藏夹画像缺少 {key} 列表")
    def clean_list(key: str, cap: int = 8) -> list[str]:
        raw = profile.get(key)
        if not isinstance(raw, list):
            return []
        return [str(x).strip()[:160] for x in raw if str(x).strip()][:cap]
    topics = clean_list("topics")
    if len(topics) < 2:
        raise ValueError("模型返回的收藏夹画像主题不足 2 项")
    try:
        confidence = float(profile["confidence"])
    except (TypeError, ValueError):
        raise ValueError("模型返回的收藏夹画像缺少有效 confidence")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("模型返回的收藏夹画像 confidence 超出 0~1")
    coherence = str(profile.get("coherence", "insufficient")).lower()
    if coherence not in ("coherent", "mixed", "insufficient"):
        raise ValueError("模型返回的收藏夹画像缺少有效 coherence")
    return {
        "summary": summary[:1200],
        "topics": topics,
        "typical_content": clean_list("typical_content", 5),
        "out_of_scope": clean_list("out_of_scope", 5),
        "coherence": coherence,
        "confidence": confidence,
        "sample_count": len(samples),
        "api_prompt_tokens": usage_totals["prompt"],
        "api_completion_tokens": usage_totals["completion"],
        "api_calls": usage_totals["calls"],
    }
