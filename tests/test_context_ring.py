# -*- coding: utf-8 -*-
"""守门：上下文圆环随**每次模型请求**刷新，而不是等整轮结束。

保护对象：
1. `public/js/12-chat-input.js::handleUsageEvent`——流式 usage 事件（每完成一次模型请求
   发射一次）必须立即刷新圆环；
2. `public/js/03-media.js::setContextUsage`——`state.contextUsage` 的唯一写入点，
   历史渲染与终态（done/error/取消）路径统一经 `updateContextUsage` 复用它；
3. 圆环百分比仍只由 `renderContextUsage` 写入。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _read(name: str) -> str:
    return (ROOT / "public" / "js" / name).read_text(encoding="utf-8")


def _function_body(source: str, signature: str) -> str:
    """取函数体（到下一个顶层 function/export function 之前）。"""
    start = source.index(signature)
    rest = source[start:]
    match = re.search(r"\n(?:export )?function ", rest[1:])
    return rest if match is None else rest[: match.start() + 1]


class ContextRingRefreshTests(unittest.TestCase):
    def test_usage_event_refreshes_ring_immediately(self) -> None:
        body = _function_body(_read("12-chat-input.js"), "function handleUsageEvent")
        self.assertIn(
            "setContextUsage(event.usage",
            body,
            "usage 事件（每次模型请求）必须立即刷新上下文圆环",
        )
        self.assertIn("usageMarkup(event.usage", body, "消息尾端用量框仍要更新")

    def test_context_usage_state_has_single_writer(self) -> None:
        source = _read("03-media.js")
        writes = re.findall(r"state\.contextUsage\s*=", source)
        self.assertEqual(
            len(writes), 1, f"state.contextUsage 只允许一个写入点，实际 {len(writes)} 处"
        )
        setter = _function_body(source, "export function setContextUsage")
        self.assertIn("state.contextUsage", setter)
        self.assertIn("renderContextUsage()", setter)

    def test_update_context_usage_delegates_to_setter(self) -> None:
        body = _function_body(_read("03-media.js"), "export function updateContextUsage")
        self.assertIn(
            "setContextUsage(",
            body,
            "历史渲染与终态路径必须复用同一写入点（否则两套口径会漂移）",
        )

    def test_ring_percent_written_only_by_renderer(self) -> None:
        source = _read("03-media.js")
        body = _function_body(source, "export function renderContextUsage")
        start = source.index("export function renderContextUsage")
        span = (start, start + len(body))
        positions = [m.start() for m in re.finditer(r"--context-percent", source)]
        self.assertTrue(positions, "圆环百分比写入点丢失")
        for pos in positions:
            self.assertTrue(
                span[0] <= pos <= span[1],
                "圆环百分比只能由 renderContextUsage 写入",
            )


if __name__ == "__main__":
    unittest.main()
