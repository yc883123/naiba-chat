# -*- coding: utf-8 -*-
"""首轮上下文落盘与查询契约（first_turn 折叠卡数据链路）。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.storage.store import ChatStorage  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
