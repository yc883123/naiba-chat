# -*- coding: utf-8 -*-
"""活动时间线（metadata.activity）契约：严格物理序 + 条目时间戳。

保护对象：run/stream.py `_build_activity_timeline`——
1. **严格物理序**：按事件序列顺序输出，不做任何"语义修正重排"
   （buffered 思考出现在末尾就保持在末尾，与事件发生顺序一致）；
2. **条目 ts**：reasoning/prose/tool 条目附带对应 run_events 事件的 created_at
   毫秒时间戳（前端备用字段，当前仅传递不强制显示）；
3. 无时间戳数据（旧事件/测试桩）时省略 ts 键，不报错。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.run.stream import _build_activity_timeline  # noqa: E402


def ev(kind: str, **extra):
    base = {"type": kind}
    base.update(extra)
    return base


class ActivityTimelineTests(unittest.TestCase):
    def test_strict_physical_order_keeps_trailing_reasoning_last(self):
        # buffered 思考（推理在最后才给出）必须保持在末尾——严格物理序，不允许重排。
        events = [
            ev("delta", content="先说一句", created_at=100),
            ev("reasoning_start", created_at=200),
            ev("reasoning_delta", content="后补的思考", created_at=250),
            ev("reasoning_end", created_at=300),
        ]
        activity = _build_activity_timeline(events, ["后补的思考"], [])
        types = [item["type"] for item in activity]
        # 无工具轮：prose 不内嵌（归到末尾 content），活动仅一条思考且保持在末尾。
        self.assertEqual(types, ["reasoning"])
        self.assertEqual(activity[0]["text"], "后补的思考")

    def test_trailing_reasoning_after_tools_stays_last(self):
        # 工具轮 + 后续正文与 buffered 思考：思考/工具保持物理序，最终答复固定到末尾。
        events = [
            ev("delta", content="调一下工具", created_at=100),
            ev("tool_start", created_at=200),
            ev("tool_result", created_at=300),
            ev("reasoning_start", created_at=400),
            ev("reasoning_delta", content="思考", created_at=500),
            ev("reasoning_end", created_at=600),
            ev("delta", content="完成", created_at=700),
        ]
        runs = [{"tool": "read_file", "success": True, "result": "ok"}]
        activity = _build_activity_timeline(events, ["思考"], runs)
        self.assertEqual(
            [item["type"] for item in activity],
            ["prose", "tool", "reasoning", "prose"],
            "中途正文保持物理位置；最终答复（最后一 prose 段）固定到时间线末尾",
        )
        self.assertEqual(activity[-1]["text"], "完成")

    def test_request_index_groups_by_usage_events(self):
        # usage 事件是请求轮次边界：其后到达的活动条目归属下一次请求（request_index+1）。
        events = [
            ev("tool_start", created_at=100),
            ev("tool_result", created_at=200),
            ev("usage", usage={"requests": 1}, created_at=300),
            ev("reasoning_start", created_at=400),
            ev("reasoning_delta", content="第二次请求的思考", created_at=500),
            ev("reasoning_end", created_at=600),
            ev("delta", content="最终答复", created_at=700),
        ]
        runs = [{"tool": "pwsh", "success": True, "result": "out"}]
        activity = _build_activity_timeline(events, ["第二次请求的思考"], runs)
        tool = next(item for item in activity if item["type"] == "tool")
        reasoning = next(item for item in activity if item["type"] == "reasoning")
        prose = next(item for item in activity if item["type"] == "prose")
        self.assertEqual(tool["request_index"], 1, "usage 前的条目归属请求 1")
        self.assertEqual(reasoning["request_index"], 2, "usage 后的条目归属请求 2")
        self.assertEqual(prose["request_index"], 2)

    def test_entries_carry_ts_from_events(self):
        events = [
            ev("delta", content="正文段", created_at=100),
            ev("delta", content="继续", created_at=150),
            ev("tool_start", created_at=200),
            ev("tool_result", created_at=300),
            ev("reasoning_start", created_at=400),
            ev("reasoning_delta", content="思考内容", created_at=450),
            ev("reasoning_end", created_at=500),
        ]
        runs = [{"tool": "pwsh", "success": True, "result": "out"}]
        activity = _build_activity_timeline(events, ["思考内容"], runs)
        prose = next(item for item in activity if item["type"] == "prose")
        tool = next(item for item in activity if item["type"] == "tool")
        reasoning = next(item for item in activity if item["type"] == "reasoning")
        self.assertEqual(prose["ts"], 100, "prose ts 取本段首个 delta 事件")
        self.assertEqual(tool["ts"], 300, "tool ts 取 tool_result 事件")
        self.assertEqual(reasoning["ts"], 500, "reasoning ts 取 reasoning_end 事件")

    def test_no_ts_key_without_created_at(self):
        events = [
            ev("delta", content="正文"),
            ev("tool_start"),
            ev("tool_result"),
        ]
        activity = _build_activity_timeline(events, [], [{"tool": "pwsh", "success": True, "result": "out"}])
        for item in activity:
            self.assertNotIn("ts", item, "无时间戳事件不产出 ts 键")

    def test_rational_reasoning_between_tools_keeps_interleave(self):
        # 工具之间的中途思考保持交错位置（物理序）。
        events = [
            ev("tool_start", created_at=100),
            ev("tool_result", created_at=200),
            ev("reasoning_start", created_at=300),
            ev("reasoning_delta", content="中途思考", created_at=350),
            ev("reasoning_end", created_at=400),
            ev("tool_start", created_at=500),
            ev("tool_result", created_at=600),
        ]
        runs = [
            {"tool": "a", "success": True, "result": "1"},
            {"tool": "b", "success": True, "result": "2"},
        ]
        activity = _build_activity_timeline(events, ["中途思考"], runs)
        self.assertEqual(
            [item["type"] for item in activity],
            ["tool", "reasoning", "tool"],
            "工具之间的思考保持事件物理序",
        )


if __name__ == "__main__":
    unittest.main()
