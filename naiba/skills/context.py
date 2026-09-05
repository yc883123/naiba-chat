"""上下文窗口预算与 token 估算（原 SkillAgent 上下文辅助，纯函数化）。"""

from __future__ import annotations

from typing import Any


# Conservative context ceiling (tokens) used when a provider exposes no window
# (e.g. DeepSeek's /v1/models returns no context-length field, so auto-detection
# yields 0). Rather than silently truncating history — which both drops context
# and re-breaks DeepSeek's token-prefix cache every turn — a conversation is
# blocked with a user-visible notice once it reaches this bound.
DEFAULT_CONTEXT_WINDOW = 256000




def _summarize_usage(records: list[dict[str, int]]) -> dict[str, Any]:
    if not records:
        return {}
    # 缓存命中率与 token 数均采用“最后一次模型调用”（per-request）口径，而不是
    # 把本轮多次调用求和后取 Σcached/Σinput。后者会被长 agent 轮次里新增的工具内容
    # 稀释，导致“本轮”命中率看起来异常低、跨轮不可比。
    last = records[-1]
    input_tokens = max(0, int(last.get("input_tokens") or 0))
    output_tokens = max(0, int(last.get("output_tokens") or 0))
    cached_tokens = max(0, int(last.get("cached_tokens") or 0))
    total_tokens = max(0, int(last.get("total_tokens") or 0)) or input_tokens + output_tokens
    summary = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "uncached_tokens": max(0, input_tokens - cached_tokens),
        "requests": len(records),
        "last_input_tokens": input_tokens,
        "last_output_tokens": output_tokens,
        "context_tokens": input_tokens + output_tokens,
    }
    summary["cache_hit_rate"] = (
        round(cached_tokens / input_tokens * 100, 1) if input_tokens else 0.0
    )
    return summary


def _context_budget(
    profile: dict[str, Any],
    options: dict[str, Any],
    system_prompt: str,
) -> tuple[int, int]:
    """Return (effective_context_limit, history_budget) for a run.

    An unknown window (auto-detection returned 0) falls back to
    DEFAULT_CONTEXT_WINDOW so the conversation is still bounded. Output
    capacity and system overhead are reserved separately and are never
    treated as the window value itself.
    """
    try:
        window = max(0, int(profile.get("context_window") or 0))
    except (TypeError, ValueError):
        window = 0
    limit = window or DEFAULT_CONTEXT_WINDOW
    try:
        configured_output = max(
            0,
            int(options.get("max_tokens") or profile.get("max_output_tokens") or 0),
        )
    except (TypeError, ValueError):
        configured_output = 0
    output_reserve = configured_output or min(8192, max(1024, limit // 8))
    fixed_tokens = _estimate_content_tokens(system_prompt) + 512
    history_budget = max(256, limit - output_reserve - fixed_tokens)
    return limit, history_budget


def _content_text(content: Any) -> str:
    """Retrieve the plain-text payload of a message for inspections.

    Accepts either a plain string or the OpenAI multimodal ``content`` list
    (a sequence of ``{"type": "text"|"image", ...}`` parts, as produced for
    image-bearing user messages), so anti-hallucination guards that run on
    replayed assistant history are not bypassed merely because the message
    carries multipart content.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text")
        )
    return str(content or "")


def _context_fits(
    history: list[dict[str, Any]],
    profile: dict[str, Any],
    options: dict[str, Any],
    system_prompt: str,
    extra_tokens: int = 0,
) -> tuple[bool, int, int, int]:
    """Return (fits, limit, used, budget) for replaying ``history`` verbatim.

    ``used``/``budget`` are heuristic estimates (not the real tokenizer),
    used only to decide whether to block with a notice instead of truncating.
    """
    limit, history_budget = _context_budget(profile, options, system_prompt)
    used = sum(
        _estimate_content_tokens(item.get("content")) + 8
        for item in history
        if item.get("role") in {"user", "assistant"} and item.get("content")
    )
    used += max(0, int(extra_tokens or 0))
    return used <= history_budget, limit, used, history_budget


def _select_history(
    history: list[dict[str, Any]],
    profile: dict[str, Any],
    options: dict[str, Any],
    system_prompt: str,
) -> list[dict[str, Any]]:
    """Return the conversation history verbatim, never truncating.

    A conversation that reaches the effective context limit is blocked before
    the request is built (see run()); silently dropping the oldest turns
    would both lose context and re-break the provider's token-prefix cache on
    every subsequent turn.

    The replayed ``trace`` from a prior turn carries native tool-call records:
    an assistant message with empty ``content`` but ``tool_calls``, plus the
    matching ``role: tool`` results. Those must survive so the current request
    stays byte-identical to the previous turn (caching) and so the model still
    sees the tool context it needs.
    """
    return [
        item for item in history
        if isinstance(item, dict)
        and item.get("role") in {"user", "assistant", "tool"}
        and (
            item.get("content")
            or item.get("tool_calls")
            or item.get("role") == "tool"
        )
    ]


def _estimate_content_tokens(content: Any) -> int:
    """Conservative tokenizer-free estimate for mixed Chinese/ASCII text."""
    if isinstance(content, list):
        return sum(
            1024 if part.get("type") == "image" else _estimate_content_tokens(
                str(part.get("text") or "")
            )
            for part in content if isinstance(part, dict)
        )
    text = str(content or "")
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    return max(1, (ascii_chars + 3) // 4 + (len(text) - ascii_chars)) if text else 0

