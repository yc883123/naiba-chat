# -*- coding: utf-8 -*-
"""护栏：推理流流式缓冲（delta 块）与终态单 run 合流。

背景：流式期必须以 delta 块快速落库保证前端平滑显示（合流窗口会切成碎片）；
run 结束后由收尾（chat.py finally → store.compress_run_events）把该 run 的
delta 事件合并为整段 reasoning（与迁移 v14 同口径），历史库不膨胀。
本组测试守护：
- 流式期：未达阈值（512 字符 / 0.1s）不落库；flush 后为 reasoning_delta 块；
- 顺序：推理块先于正文块；整段 reasoning 事件透传不受缓冲影响；
- 终态合流：仅 compress 指定 run、文本总量不变、幂等；
- 终态（completed/failed/cancelled）收缩 snapshot 的 conversation_messages，
  interrupted 保留（恢复重建需要）。
"""

import json
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.run.stream import (  # noqa: E402
    REASONING_STREAM_CHARS,
    _RunEventSink,
)
from naiba.storage.store import ChatStorage  # noqa: E402


class RecordingManager:
    def __init__(self):
        self.events: list[dict] = []

    def emit(self, run_id, payload):
        self.events.append(dict(payload))
        return {"run_id": run_id, "sequence": len(self.events)}


class ReasoningStreamTests(unittest.TestCase):
    def setUp(self):
        self.manager = RecordingManager()
        self.sink = _RunEventSink(self.manager, "r1", threading.Event())

    def test_deltas_buffered_until_flush_signal(self):
        for i in range(10):  # 10 * 20 字符 = 200 < 512：未达阈值不落库
            self.sink({"type": "reasoning_delta", "content": "字" * 20})
        self.assertEqual(self.manager.events, [], "未达阈值不应落库")
        self.sink({"type": "reasoning_end"})
        self.assertEqual(len(self.manager.events), 2)
        self.assertEqual(self.manager.events[0]["type"], "reasoning_delta", "流式块保持 delta 形态")
        self.assertEqual(self.manager.events[0]["content"], "字" * 200)
        self.assertEqual(self.manager.events[1]["type"], "reasoning_end")

    def test_char_threshold_flushes_mid_stream(self):
        piece = "x" * (REASONING_STREAM_CHARS // 2)
        self.sink({"type": "reasoning_delta", "content": piece})
        self.sink({"type": "reasoning_delta", "content": piece})  # 累计 512 >= 阈值即 flush
        self.assertEqual(len(self.manager.events), 1)
        self.assertEqual(self.manager.events[0]["type"], "reasoning_delta")
        self.assertEqual(self.manager.events[0]["content"], piece * 2)
        self.sink({"type": "reasoning_end"})
        self.assertEqual(len(self.manager.events), 2)

    def test_reasoning_before_delta_ordering(self):
        self.sink({"type": "reasoning_delta", "content": "思考"})
        self.sink({"type": "delta", "content": "正文"})
        self.sink.flush()
        self.assertEqual([e["type"] for e in self.manager.events], ["reasoning_delta", "delta"])

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


class CompressRunEventsTests(unittest.TestCase):
    def _insert_delta_events(self, db, run_id: str, count: int, chars: int, created_base: int = 1000):
        rows = []
        for i in range(count):
            rows.append((
                run_id, i + 1, "reasoning_delta",
                json.dumps({"type": "reasoning_delta", "content": "字" * chars}, ensure_ascii=False),
                created_base + i,
            ))
        db.executemany(
            "INSERT INTO run_events(run_id, sequence, event_type, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        db.commit()

    def test_compress_scoped_to_run_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            convo = storage.create_conversation()
            agent = {"id": "general", "name": "通用 Agent"}
            run_a, _h = storage.create_chat_run(convo["id"], "A", [], agent, {"model_key": "m"}, "craft")
            storage.update_background_task(run_a["id"], status="completed", finished=True)
            run_b, _h = storage.create_chat_run(convo["id"], "B", [], agent, {"model_key": "m"}, "craft")
            storage.update_background_task(run_b["id"], status="interrupted", finished=True)
            with closing(__import__("sqlite3").connect(Path(tmp) / "chat.db")) as db:
                self._insert_delta_events(db, run_a["id"], 300, 10)  # 3000 字符 -> 2 段
                self._insert_delta_events(db, run_b["id"], 100, 10)
                processed = storage.compress_run_events(run_a["id"])
                self.assertEqual(processed, 300)
                # 仅 run_a 被压缩；run_b 保持 delta
                a_delta = db.execute(
                    "SELECT COUNT(*) FROM run_events WHERE event_type='reasoning_delta' AND run_id=?",
                    (run_a["id"],),
                ).fetchone()[0]
                b_delta = db.execute(
                    "SELECT COUNT(*) FROM run_events WHERE event_type='reasoning_delta' AND run_id=?",
                    (run_b["id"],),
                ).fetchone()[0]
                self.assertEqual(a_delta, 0)
                self.assertEqual(b_delta, 100)
                # 文本不丢
                a_text = sum(
                    len(json.loads(r[0]).get("content") or "")
                    for r in db.execute(
                        "SELECT payload FROM run_events WHERE event_type='reasoning' AND run_id=?",
                        (run_a["id"],),
                    )
                )
                self.assertEqual(a_text, 3000)
                # 幂等
                self.assertEqual(storage.compress_run_events(run_a["id"]), 0)


if __name__ == "__main__":
    unittest.main()
