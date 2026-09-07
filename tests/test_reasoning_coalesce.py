# -*- coding: utf-8 -*-
"""护栏：推理流合流（reasoning_delta → 整段 reasoning）与终态 snapshot 收缩。

背景：存量库 run_events 90 万行中 87 万行是逐 token 的 reasoning_delta（96.6%），
且 background_tasks.snapshot 因每轮固化完整会话消息累积 81 MB。本组测试守护：
- 推理 delta 按缓冲合流落库（阈值 2048 字符 / 1s），非 delta 事件前必先落库；
- 合流事件形态为整段 `reasoning`（前端与 _rebuild_partial_run 均兼容）；
- 终态（completed/failed/cancelled）收缩 snapshot 的 conversation_messages，
  interrupted 保留（恢复重建需要）。
"""

import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.run.stream import (  # noqa: E402
    REASONING_FLUSH_CHARS,
    _RunEventSink,
)
from naiba.storage.store import ChatStorage  # noqa: E402


class RecordingManager:
    def __init__(self):
        self.events: list[dict] = []

    def emit(self, run_id, payload):
        self.events.append(dict(payload))
        return {"run_id": run_id, "sequence": len(self.events)}


class ReasoningCoalesceTests(unittest.TestCase):
    def setUp(self):
        self.manager = RecordingManager()
        self.sink = _RunEventSink(self.manager, "r1", threading.Event())

    def test_deltas_coalesce_until_flush_signal(self):
        for i in range(50):  # 50 * 20 字符 = 1000 字符 < 2048，且无时间窗口触发
            self.sink({"type": "reasoning_delta", "content": "字" * 20})
        self.assertEqual(self.manager.events, [], "未达阈值不应落库")
        self.sink({"type": "reasoning_end"})
        self.assertEqual(len(self.manager.events), 2)
        self.assertEqual(self.manager.events[0]["type"], "reasoning")
        self.assertEqual(self.manager.events[0]["content"], "字" * 1000)
        self.assertEqual(self.manager.events[1]["type"], "reasoning_end")

    def test_char_threshold_flushes_mid_stream(self):
        piece = "x" * (REASONING_FLUSH_CHARS // 2)
        self.sink({"type": "reasoning_delta", "content": piece})
        self.sink({"type": "reasoning_delta", "content": piece})  # 累计 2048 >= 阈值即 flush
        self.assertEqual(len(self.manager.events), 1)
        self.assertEqual(self.manager.events[0]["type"], "reasoning")
        self.assertEqual(self.manager.events[0]["content"], piece * 2)
        # 剩余缓冲在下次 flush 信号落库
        self.sink({"type": "reasoning_end"})
        self.assertEqual(len(self.manager.events), 2)

    def test_reasoning_before_delta_ordering(self):
        self.sink({"type": "reasoning_delta", "content": "思考"})
        self.sink({"type": "delta", "content": "正文"})
        self.sink.flush()
        self.assertEqual([e["type"] for e in self.manager.events], ["reasoning", "delta"])

    def test_explicit_reasoning_passes_through(self):
        # 非 delta 的整段 reasoning 事件原样落库，不误合流。
        self.sink({"type": "reasoning", "content": "完整段落"})
        self.assertEqual(len(self.manager.events), 1)
        self.assertEqual(self.manager.events[0]["type"], "reasoning")

    def test_flush_reasoning_on_cancel_path(self):
        self.sink({"type": "reasoning_delta", "content": "未完成思考"})
        self.sink.flush()
        self.assertEqual(len(self.manager.events), 1)
        self.assertEqual(self.manager.events[0]["content"], "未完成思考")


class RunSnapshotSlimTests(unittest.TestCase):
    def _make_run(self, storage, conversation_id, agent):
        run, _history = storage.create_chat_run(
            conversation_id, "测试消息", [], agent, {"model_key": "online:demo"}, "craft"
        )
        return run

    def test_terminal_status_slims_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            convo = storage.create_conversation()
            agent = {"id": "general", "name": "通用 Agent"}
            run = self._make_run(storage, convo["id"], agent)
            before = storage.get_run_snapshot(run["id"])
            self.assertIn("conversation_messages", before)
            storage.update_background_task(run["id"], status="completed", finished=True)
            after = storage.get_run_snapshot(run["id"])
            self.assertNotIn("conversation_messages", after)
            self.assertEqual(after.get("model_key"), "online:demo")

    def test_failed_and_cancelled_also_slim(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            convo = storage.create_conversation()
            agent = {"id": "general", "name": "通用 Agent"}
            for status in ("failed", "cancelled"):
                run = self._make_run(storage, convo["id"], agent)
                storage.update_background_task(run["id"], status=status, finished=True)
                self.assertNotIn(
                    "conversation_messages", storage.get_run_snapshot(run["id"]),
                    f"{status} 后 snapshot 应收缩",
                )

    def test_interrupted_keeps_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            convo = storage.create_conversation()
            agent = {"id": "general", "name": "通用 Agent"}
            run = self._make_run(storage, convo["id"], agent)
            storage.update_background_task(run["id"], status="interrupted", finished=True)
            self.assertIn("conversation_messages", storage.get_run_snapshot(run["id"]))


if __name__ == "__main__":
    unittest.main()
