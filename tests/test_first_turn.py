# -*- coding: utf-8 -*-
"""首轮上下文落盘与查询契约（first_turn 折叠卡数据链路）。

覆盖：run 快照合并/取最早 run（v16 之前的数据形态）；会话级 `conversations.first_turn`
列（分支对话继承、清空已结束任务后不丢）；`_first_turn_info` 的取数顺序与旧结构归一。
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.storage.store import ChatStorage  # noqa: E402


class FirstTurnMigrationTests(unittest.TestCase):
    """迁移 v16：`conversations.first_turn` 纯增量列、幂等。"""

    def test_schema_version_and_idempotent_migration(self):
        from naiba.storage.store import CURRENT_SCHEMA_VERSION, MIGRATIONS

        self.assertGreaterEqual(CURRENT_SCHEMA_VERSION, 16)
        self.assertIn(16, MIGRATIONS)
        with tempfile.TemporaryDirectory(prefix="naiba_firstturn_mig_") as tmp:
            db_path = Path(tmp) / "chat.db"
            storage = ChatStorage(db_path)
            conn = sqlite3.connect(storage.db_path)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
                self.assertIn("first_turn", columns, "初始化后必须已有 first_turn 列")
                # 重复执行不得抛错（列已存在时跳过），否则升级路径会被二次迁移打断。
                MIGRATIONS[16](conn)
                MIGRATIONS[16](conn)
                again = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
                self.assertIn("first_turn", again)
            finally:
                conn.close()


class FirstTurnStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.conversation = self.storage.create_conversation()

    def tearDown(self):
        self.tmp.cleanup()

    def _create_chat_run(self, snapshot_extra, finish=True):
        snapshot = {
            "model_key": "online:demo",
            "allowed_tools": ["read_file", "pwsh"],
            "is_first_turn": True,
            **snapshot_extra,
        }
        run = self.storage.create_run(
            self.conversation["id"],
            "首轮测试",
            {"id": "general", "name": "通用 Agent", "system_prompt": "", "skill_ids": []},
            snapshot,
            kind="chat",
        )
        if finish:
            self.storage.update_background_task(run["id"], status="completed", finished=True)
        return run

    def test_update_run_snapshot_merges(self):
        run = self._create_chat_run({})
        merged = self.storage.update_run_snapshot(run["id"], {"first_turn": {"prompt": "p1", "tools": []}})
        self.assertEqual(merged["first_turn"]["prompt"], "p1")
        self.assertEqual(merged["model_key"], "online:demo", "既有键保留")

    def test_first_chat_run_snapshot_returns_earliest_run(self):
        first = self._create_chat_run({"model_key": "online:first"})
        self._create_chat_run({"model_key": "online:second"})
        snapshot = self.storage.first_chat_run_snapshot(self.conversation["id"])
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["model_key"], "online:first", "取最早的 chat run")

        # 再写一次 first_turn 到第一个 run，second run 不受影响（first_chat_run_snapshot 读第一个）
        self.storage.update_run_snapshot(first["id"], {"first_turn": {"prompt": "首轮提示词"}})
        snapshot = self.storage.first_chat_run_snapshot(self.conversation["id"])
        self.assertEqual(snapshot["first_turn"]["prompt"], "首轮提示词")

    def test_no_chat_run_returns_none(self):
        self.assertIsNone(self.storage.first_chat_run_snapshot(self.conversation["id"]))

    def test_trace_summarizer_strips_big_image_base64(self):
        from naiba.run.chat import _summarize_trace_messages

        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": [
                {"type": "text", "text": "看图"},
                {"type": "image", "data": "x" * 999, "media_type": "image/png"},
            ]},
            {"role": "user", "content": [{"type": "image", "data": "short"}]},
            {"role": "assistant", "content": "ok"},
        ]
        out = _summarize_trace_messages(messages)
        self.assertEqual(out[1]["content"][1]["data"], "[base64 图片数据已省略]")
        self.assertEqual(out[2]["content"][0]["data"], "short", "小负载不动")
        self.assertEqual(out[0], messages[0])
        self.assertEqual(out[3], messages[3])


class FirstTurnBranchTests(unittest.TestCase):
    """分支对话必须继承会话级首轮上下文（用户报障：分支后顶部折叠卡不显示）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.conversation = self.storage.create_conversation()
        self.first_turn = {
            "system": "系统提示词原文",
            "tools": [{"name": "read_file"}],
            "model_key": "online:demo",
            "agent_name": "通用 Agent",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _two_turn_history(self) -> str:
        """造两轮历史，返回第二轮用户消息 id（分支点）。"""
        self.storage.add_message(self.conversation["id"], "user", "第一问")
        self.storage.add_message(self.conversation["id"], "assistant", "第一答")
        branch = self.storage.add_message(self.conversation["id"], "user", "第二问")
        self.storage.add_message(self.conversation["id"], "assistant", "第二答")
        return str(branch["id"])

    def _chat_run(self, snapshot_extra=None, finish=False):
        run = self.storage.create_run(
            self.conversation["id"],
            "首轮测试",
            {"id": "general", "name": "通用 Agent", "system_prompt": "", "skill_ids": []},
            {"is_first_turn": True, **(snapshot_extra or {})},
            kind="chat",
        )
        if finish:
            self.storage.update_background_task(run["id"], status="completed", finished=True)
        return run

    def test_conversation_first_turn_roundtrip(self):
        self.assertIsNone(self.storage.conversation_first_turn(self.conversation["id"]))
        self.storage.set_conversation_first_turn(self.conversation["id"], self.first_turn)
        self.assertEqual(self.storage.conversation_first_turn(self.conversation["id"]), self.first_turn)

    def test_branch_inherits_first_turn(self):
        branch_id = self._two_turn_history()
        self.storage.set_conversation_first_turn(self.conversation["id"], self.first_turn)
        result = self.storage.branch_conversation(self.conversation["id"], branch_id)
        self.assertEqual(
            self.storage.conversation_first_turn(result["conversation"]["id"]),
            self.first_turn,
            "分支继承消息前缀，首轮上下文必须一并继承",
        )

    def test_branch_from_first_message_does_not_inherit(self):
        first = self.storage.add_message(self.conversation["id"], "user", "第一问")
        self.storage.set_conversation_first_turn(self.conversation["id"], self.first_turn)
        result = self.storage.branch_conversation(self.conversation["id"], str(first["id"]))
        self.assertIsNone(
            self.storage.conversation_first_turn(result["conversation"]["id"]),
            "分支点是首条消息 → 新会话按自己的新首轮重新落盘，不继承",
        )

    def test_branch_falls_back_to_legacy_run_snapshot(self):
        """v16 之前的老会话：列里为空，首轮上下文只在最早 chat run 快照里。"""
        branch_id = self._two_turn_history()
        run = self._chat_run()
        self.storage.update_run_snapshot(run["id"], {"first_turn": self.first_turn})
        result = self.storage.branch_conversation(self.conversation["id"], branch_id)
        self.assertEqual(
            self.storage.conversation_first_turn(result["conversation"]["id"]), self.first_turn,
        )

    def test_first_turn_survives_clearing_terminal_tasks(self):
        """「清空已结束任务」会删掉 chat run 行（无 kind 过滤），会话级副本必须还在。"""
        self._chat_run(finish=True)
        self.storage.set_conversation_first_turn(self.conversation["id"], self.first_turn)
        self.storage.clear_terminal_background_tasks()
        self.assertEqual(self.storage.conversation_first_turn(self.conversation["id"]), self.first_turn)
        self.assertIsNone(self.storage.first_chat_run_snapshot(self.conversation["id"]),
                          "run 快照确实被清空（说明会话级副本是唯一留存）")


class FirstTurnInfoTests(unittest.TestCase):
    """`_first_turn_info` 取数顺序：会话列优先 → 老会话回退 run 快照 → 旧 prompt 字段归一。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.conversation = self.storage.create_conversation()

    def tearDown(self):
        self.tmp.cleanup()

    def _info(self):
        from naiba.app import NaibaChatApp

        return NaibaChatApp._first_turn_info(
            SimpleNamespace(storage=self.storage), self.conversation["id"],
        )

    def test_prefers_conversation_column(self):
        self.storage.set_conversation_first_turn(
            self.conversation["id"], {"system": "会话列", "tools": []})
        run = self.storage.create_run(
            self.conversation["id"], "首轮",
            {"id": "general", "name": "通用 Agent", "system_prompt": "", "skill_ids": []},
            {"is_first_turn": True}, kind="chat",
        )
        self.storage.update_run_snapshot(run["id"], {"first_turn": {"system": "快照", "tools": []}})
        self.assertEqual(self._info()["system"], "会话列")

    def test_legacy_snapshot_fallback(self):
        run = self.storage.create_run(
            self.conversation["id"], "首轮",
            {"id": "general", "name": "通用 Agent", "system_prompt": "", "skill_ids": []},
            {"is_first_turn": True}, kind="chat",
        )
        self.storage.update_run_snapshot(run["id"], {"first_turn": {"prompt": "旧结构", "tools": []}})
        info = self._info()
        self.assertEqual(info["system"], "旧结构", "旧版 prompt 字段归一成 system")
        self.assertEqual(info["tools"], [])

    def test_no_data_returns_none(self):
        self.assertIsNone(self._info())


if __name__ == "__main__":
    unittest.main()
