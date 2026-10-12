"""Pure LLM request parameter normalization."""
from __future__ import annotations


def thinking_params(effort: str) -> dict:
    effort = (effort or "").strip().lower()
    if not effort or effort in ("default", "auto"):
        return {}
    if effort in ("off", "none", "disable", "disabled"):
        return {"enable_thinking": False}
    params: dict = {"enable_thinking": True}
    if effort in ("low", "medium", "high", "max"):
        params["reasoning_effort"] = effort
    elif effort == "xhigh":
        params["reasoning_effort"] = "max"
    return params
