"""运行事件流基建（原 async_tasks.py 的事件 sink 与活动时间线，阶段 2 第二步）。

- ``safe_activity`` / ``build_activity_timeline``：把事件序列编排成前端可渲染的
  思维链/工具链时间线（模块级纯函数，任何异常不阻断消息保存/发送）。
- ``_RunEventSink``：模型事件持久化协调器——delta 合流（≥4096 字符或 ≥0.1s 落库）、
  tool_requested/tool_started 去重为单个 tool_start、取消即抛 TaskCancelled。
  双线程（run 线程与看门狗线程）共享同一 sink，delta 缓冲由锁保护。
"""

from __future__ import annotations

import threading
import time
from typing import Any

from naiba.core.exceptions import TaskCancelled

# 推理流采用「流式即刻落库 + 终态合流」：reasoning_delta 到达即落库并推送（保留
# 模型一个词一个词的实时显示节奏）；run 结束后由收尾合流（chat.py「终态压缩」
# → store.compress_run_events）把该 run 的 delta 事件重新整理为整段 reasoning
# （与存量压缩迁移 v14 同口径），历史库不膨胀。


def _safe_activity(
    events: list[dict[str, Any]], reasonings: list[str], runs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """用 try/except 包裹时间线构建，任何异常都不阻断消息保存/发送。"""
    try:
        return _build_activity_timeline(events, reasonings, runs)
    except Exception:
        return []


def _build_activity_timeline(
    events: list[dict[str, Any]], reasonings: list[str], runs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """按运行事件的时间序，把思考段、正文与工具调用交错产出，供前端按时间显示思维链/工具链。

    正文（delta 事件）只有当本轮确实调用了工具时才作为 ``{"type": "prose", "text": ...}``
    条目插入到它发生的时间点（"中途回复的正文"随工具链交错展示）；若没有任何工具调用，
    正文就是整段的最终回复，统一放到末尾 content 里，避免"正文跑到最前面、思考在最后"
    的倒序观感。内容复用传入的 reasonings 与 runs（避免重复/不一致），仅用 events 的
    先后顺序决定交错。计数不齐时把剩余段落追加到末尾兜底。本函数为模块级。
    """
    activity: list[dict[str, Any]] = []
    has_tools = any(str(ev.get("type") or "") in {"tool_start", "tool_result"} for ev in events)
    ri = 0
    ti = 0
    in_reasoning = False
    prose: list[str] = []

    def flush_prose() -> None:
        if prose and has_tools:
            activity.append({"type": "prose", "text": "".join(prose)})
        prose.clear()

    def flush_reasoning() -> None:
        nonlocal ri
        if in_reasoning and ri < len(reasonings):
            activity.append({"type": "reasoning", "text": reasonings[ri]})
            ri += 1

    for ev in events:
        kind = str(ev.get("type") or "")
        if kind == "delta":
            prose.append(str(ev.get("content") or ""))
            continue
        flush_prose()
        if kind == "reasoning_start":
            in_reasoning = True
        elif kind == "reasoning_end":
            flush_reasoning()
            in_reasoning = False
        elif kind == "reasoning":
            flush_reasoning()
            if ri < len(reasonings):
                activity.append({"type": "reasoning", "text": reasonings[ri]})
                ri += 1
        elif kind == "reasoning_delta":
            # Streaming reasoning deltas are coalesced; the text is matched via
            # reasonings at reasoning_end, so keep state without adding here.
            pass
        elif kind == "tool_result":
            if ti < len(runs):
                activity.append({"type": "tool", "run": runs[ti]})
                ti += 1
    flush_prose()
    flush_reasoning()
    while ri < len(reasonings):
        activity.append({"type": "reasoning", "text": reasonings[ri]})
        ri += 1
    while ti < len(runs):
        activity.append({"type": "tool", "run": runs[ti]})
        ti += 1
    # 某些模型把思考作为一整块在最后才给出（buffered）。此时按事件顺序它会排在正文之后，
    # 造成"正文在前、思考在后"的倒序；而思考逻辑上发生在答复之前，应移到最前面。
    # 仅在"末尾是 reasoning"时重排，不影响处于工具之间的中途思考。
    trailing: list[dict[str, Any]] = []
    while activity and activity[-1].get("type") == "reasoning":
        trailing.insert(0, activity.pop())
    if trailing:
        activity[:0] = trailing
    return activity


class _RunEventSink:
    """Persist model events while coalescing high-frequency text deltas."""

    def __init__(self, manager: Any, run_id: str, cancel_event: threading.Event):
        self.manager = manager
        self.run_id = run_id
        self.cancel_event = cancel_event
        self._delta = ""
        self._last_flush = time.monotonic()
        self._announced_tools: set[str] = set()
        self.failure_message: str | None = None
        # Guard the delta buffer so the run thread and the watchdog thread can both
        # flush safely (the watchdog may persist the aborted message without the run
        # thread ever reaching its own flush path).
        self._flush_lock = threading.Lock()

    def __call__(self, payload: dict[str, Any]) -> None:
        if self.cancel_event.is_set():
            raise TaskCancelled("任务已取消")
        if str(payload.get("type") or "") == "delta":
            self._delta += str(payload.get("content") or "")
            now = time.monotonic()
            if len(self._delta) >= 4096 or now - self._last_flush >= 0.1:
                self.flush()
            return
        if str(payload.get("type") or "") == "reasoning_delta":
            # 流式即刻落库：保留"一个词一个词"的实时推送节奏（落库即推送，
            # _stream_run 每事件 flush）；最终形态由 run 收尾的终态合流整理。
            self.manager.emit(self.run_id, payload)
            return
        self.flush()
        kind = str(payload.get("type") or "")
        if kind == "run_failed":
            self.failure_message = str(payload.get("error") or "任务执行失败")
        # SkillAgent emits a rich `tool_requested` event before dispatch and a
        # lower-level `tool_started` event inside the executor. The browser
        # protocol has one lifecycle event, so publish one `tool_start` and
        # suppress the duplicate while retaining the request metadata.
        if kind == "tool_requested":
            tool = str(payload.get("tool") or "")
            if tool:
                self._announced_tools.add(tool)
            payload = {**payload, "type": "tool_start"}
        elif kind == "tool_started":
            tool = str(payload.get("tool") or "")
            if tool in self._announced_tools:
                return
            payload = {**payload, "type": "tool_start"}
            if tool:
                self._announced_tools.add(tool)
        # 视觉工具与其它工具同构：不再旁路为 vision_start/vision_done 状态事件。
        self.manager.emit(self.run_id, payload)

    def flush(self) -> None:
        with self._flush_lock:
            if not self._delta:
                return
            content = self._delta
            self._delta = ""
            self._last_flush = time.monotonic()
        # Emit outside the lock: the content is already claimed above, so a
        # concurrent flush sees an empty buffer and returns without duplicating.
        self.manager.emit(self.run_id, {"type": "delta", "content": content})
