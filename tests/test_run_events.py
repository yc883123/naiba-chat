# -*- coding: utf-8 -*-
"""护栏：Run 事件流与取消竞态行为规格。

保护对象：阶段 2 将 async_tasks 拆成 run/{manager,stream,session,chat} 时的行为等价性。
覆盖：emit→状态映射、cancelling 冻结、ACTIVE_RUN 互斥、3 秒强制取消看门狗兜底。
（看门狗/互斥用例为确定性实现：patch time.sleep 使 3 秒延迟归零。）
"""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import async_tasks  # noqa: E402
from async_tasks import ConversationRunManager  # noqa: E402
from naiba.run import manager as run_manager  # noqa: E402
from storage import ChatStorage  # noqa: E402


class RecordingStorage:
    def __init__(self):
        self.events = []
        self.task = {"status": "running", "conversation_id": "c1", "detail": {}}
        self.updates = []

    def append_run_event(self, run_id, payload):
        self.events.append((run_id, payload))
        return {"run_id": run_id, "sequence": len(self.events)}

    def get_background_task(self, run_id):
        return dict(self.task)

    def update_background_task(self, run_id, **kw):
        self.updates.append((run_id, kw))


class StubApp:
    def __init__(self):
        self.storage = RecordingStorage()


class RunEventTests(unittest.TestCase):
    def setUp(self):
        self.storage = RecordingStorage()
        self.manager = ConversationRunManager(StubApp())
        self.manager.app.storage = self.storage

    def test_status_event_marks_running(self):
        self.manager.emit("r1", {"type": "status", "message": "开始执行"})
        run_id, kw = self.storage.updates[-1]
        self.assertEqual(run_id, "r1")
        self.assertEqual(kw["status"], "running")
        self.assertEqual(kw["detail"]["message"], "开始执行")

    def test_tool_confirm_marks_waiting(self):
        self.manager.emit(
            "r1",
            {"type": "tool_confirm", "tool_name": "pwsh", "tool_desc": "执行命令",
             "arguments": {"command": "dir"}, "confirm_id": "c9"},
        )
        run_id, kw = self.storage.updates[-1]
        self.assertEqual(kw["status"], "waiting")
        self.assertIn("等待工具确认", kw["detail"]["message"])
        self.assertEqual(kw["detail"]["confirm_id"], "c9")

    def test_tool_result_reads_back_to_running(self):
        self.manager.emit("r1", {"type": "tool_result", "tool": "pwsh"})
        run_id, kw = self.storage.updates[-1]
        self.assertEqual(kw["status"], "running")

    def test_cancelling_freezes_status_updates(self):
        self.storage.task["status"] = "cancelling"
        self.manager.emit("r1", {"type": "status", "message": "不应改写状态"})
        # 终态冻结：status 不落库（update 仍会带 detail 但 status=None）。
        run_id, kw = self.storage.updates[-1]
        self.assertIsNone(kw["status"])

    def test_watchdog_forces_cancelled_when_run_stuck(self):
        # 3 秒兜底看门狗：run 线程未及时收尾时，仍把状态置 cancelled 并发出事件。
        self.storage.task = {"status": "cancelling", "conversation_id": "c1", "detail": {}}
        with mock.patch.object(run_manager.time, "sleep", return_value=None):
            self.manager._schedule_forced_cancel("r1")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            cancelled = [
                kw for _run_id, kw in self.storage.updates
                if kw.get("status") == "cancelled"
            ]
            if cancelled:
                break
            time.sleep(0.02)
        self.assertTrue(cancelled, "看门狗未把卡住的 run 置为 cancelled")
        self.assertTrue(
            any(payload.get("type") == "cancelled" for _rid, payload in self.storage.events),
            "看门狗未发出 cancelled 事件",
        )


class ActiveRunInterlockTests(unittest.TestCase):
    def test_second_chat_run_rejected_with_active_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            conversation = storage.create_conversation()
            snapshot = {"model_key": "online:demo", "provider_id": "demo"}
            agent = {"id": "general", "name": "通用 Agent"}
            storage.create_chat_run(conversation["id"], "第一条", [], agent, snapshot, "craft")
            with self.assertRaisesRegex(RuntimeError, "ACTIVE_RUN"):
                storage.create_chat_run(conversation["id"], "第二条", [], agent, snapshot, "craft")


if __name__ == "__main__":
    unittest.main()
