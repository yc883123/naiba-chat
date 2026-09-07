# -*- coding: utf-8 -*-
"""护栏：存量历史数据压缩迁移 v14。

背景：run_events 90 万行中 87 万行是逐 token 推理 delta；done/cancelled 载荷
携带完整消息；终态 snapshot 固化完整会话消息（O(N²) 累积）。v14 迁移全部幂等：
- reasoning_delta 按「2048 字符 / 1s」窗口合流为整段 reasoning；
- done/cancelled 去掉 message / aborted_message；
- 终态 snapshot 去掉 conversation_messages（interrupted 保留）；
- 迁移前自动备份整库到 data/backups。
"""

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.storage import store as storage_module  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402


def _insert_delta_events(db, run_id: str, count: int, chars: int):
    now = int(time.time() * 1000)
    rows = []
    for i in range(count):
        rows.append((
            run_id, i + 1, "reasoning_delta",
            json.dumps({"type": "reasoning_delta", "content": "字" * chars}, ensure_ascii=False),
            now + i,
        ))
    db.executemany(
        "INSERT INTO run_events(run_id, sequence, event_type, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    db.commit()


def _reasoning_text_total(db, run_id: str) -> int:
    total = 0
    for (payload,) in db.execute(
        "SELECT payload FROM run_events WHERE event_type='reasoning' AND run_id=?", (run_id,)
    ):
        total += len(json.loads(payload).get("content") or "")
    return total


class MigrationV14Tests(unittest.TestCase):
    def _make_v13_like_db(self, tmp: Path) -> ChatStorage:
        """用当前 schema 建库后把 user_version 降回 13，模拟待迁移的旧库。"""
        storage = ChatStorage(tmp / "chat.db")
        storage.set_user_version(13)
        return storage

    def test_fresh_db_reaches_v14_without_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            self.assertEqual(storage.get_user_version(), 14)
            self.assertFalse((Path(tmp) / "backups").exists(), "新库不应产生备份")

    def test_v13_data_migrated_and_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = self._make_v13_like_db(Path(tmp))
            convo = storage.create_conversation()
            agent = {"id": "general", "name": "通用 Agent"}
            done_run, _h = storage.create_chat_run(
                convo["id"], "消息1", [], agent, {"model_key": "online:demo"}, "craft"
            )
            # 终态化（ACTIVE 集合释放，才能创建下一个 run）
            storage.update_background_task(done_run["id"], status="completed", finished=True)
            cancelled_run, _h = storage.create_chat_run(
                convo["id"], "消息2", [], agent, {"model_key": "online:demo"}, "craft"
            )
            storage.update_background_task(cancelled_run["id"], status="failed", finished=True)
            interrupted_run, _h = storage.create_chat_run(
                convo["id"], "消息3", [], agent, {"model_key": "online:demo"}, "craft"
            )
            # interrupted 始终保留（恢复重建需要）
            storage.update_background_task(interrupted_run["id"], status="interrupted", finished=True)

            # 造大量推理 delta + done/cancelled 全量消息事件
            with closing(sqlite3.connect(Path(tmp) / "chat.db")) as db:
                _insert_delta_events(db, interrupted_run["id"], 5000, 20)  # 100000 字符
                for run_id in (done_run["id"], cancelled_run["id"]):
                    payload = json.dumps(
                        {"type": "done" if run_id == done_run["id"] else "cancelled",
                         "message": {"content": "x" * 5000, "metadata": {"trace": ["y" * 2000]}},
                         "aborted_message": {"content": "z" * 1000}},
                        ensure_ascii=False,
                    )
                    db.execute(
                        "INSERT INTO run_events(run_id, sequence, event_type, payload, created_at) "
                        "VALUES (?, 1, ?, ?, 1)", (run_id, "done" if run_id == done_run["id"] else "cancelled", payload),
                    )
                    db.execute(
                        "INSERT INTO run_events(run_id, sequence, event_type, payload, created_at) "
                        "VALUES (?, 2, 'run_completed', '{\"type\":\"run_completed\",\"run_id\":\"x\"}', 1)",
                        (run_id,),
                    )
                db.commit()

            # 重新打开：触发 v14 迁移
            reopened = ChatStorage(Path(tmp) / "chat.db")
            self.assertEqual(reopened.get_user_version(), 14)
            with closing(sqlite3.connect(Path(tmp) / "chat.db")) as db:
                # 1) 推理合流
                deltas = db.execute(
                    "SELECT COUNT(*) FROM run_events WHERE event_type='reasoning_delta'"
                ).fetchone()[0]
                self.assertEqual(deltas, 0, "delta 事件应全部合流")
                merged = db.execute(
                    "SELECT COUNT(*) FROM run_events WHERE event_type='reasoning'"
                ).fetchone()[0]
                self.assertGreaterEqual(merged, 48)  # 100000/2048 ≈ 49 段
                self.assertEqual(
                    _reasoning_text_total(db, interrupted_run["id"]), 100000,
                    "合流不应丢文本",
                )
                # 2) done/cancelled 瘦身
                for event_type in ("done", "cancelled"):
                    payload = db.execute(
                        "SELECT payload FROM run_events WHERE event_type=? AND sequence=1",
                        (event_type,),
                    ).fetchone()[0]
                    obj = json.loads(payload)
                    self.assertNotIn("message", obj)
                    self.assertNotIn("aborted_message", obj)
                # 3) snapshot 收缩
                snap = db.execute(
                    "SELECT snapshot FROM background_tasks WHERE id=?", (done_run["id"],)
                ).fetchone()[0]
                self.assertNotIn("conversation_messages", json.loads(snap))
                snap = db.execute(
                    "SELECT snapshot FROM background_tasks WHERE id=?", (interrupted_run["id"],)
                ).fetchone()[0]
                self.assertIn("conversation_messages", json.loads(snap))
                ok = db.execute("PRAGMA integrity_check").fetchone()[0]
                self.assertEqual(ok, "ok")
            # 备份存在
            backup_dir = Path(tmp) / "backups"
            self.assertTrue(backup_dir.exists(), "迁移前应自动备份")
            self.assertTrue(list(backup_dir.glob("chat.db*")), "备份文件应为 chat.db (+wal/shm)")

    def test_migration_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = self._make_v13_like_db(Path(tmp))
            convo = storage.create_conversation()
            agent = {"id": "general", "name": "通用 Agent"}
            run, _h = storage.create_chat_run(
                convo["id"], "消息", [], agent, {"model_key": "online:demo"}, "craft"
            )
            storage.update_background_task(run["id"], status="completed", finished=True)
            with closing(sqlite3.connect(Path(tmp) / "chat.db")) as db:
                _insert_delta_events(db, run["id"], 300, 10)
            storage.apply_pending_migrations()  # 手动重跑：无 delta 行、无重复段
            with closing(sqlite3.connect(Path(tmp) / "chat.db")) as db:
                deltas = db.execute(
                    "SELECT COUNT(*) FROM run_events WHERE event_type='reasoning_delta'"
                ).fetchone()[0]
                self.assertEqual(deltas, 0)
                # 合流后文本总量不变
                self.assertEqual(_reasoning_text_total(db, run["id"]), 3000)

    def test_coalesce_function_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = ChatStorage(Path(tmp) / "chat.db")
            convo = storage.create_conversation()
            agent = {"id": "general", "name": "通用 Agent"}
            run, _h = storage.create_chat_run(
                convo["id"], "消息", [], agent, {"model_key": "online:demo"}, "craft"
            )
            with closing(sqlite3.connect(Path(tmp) / "chat.db")) as db:
                _insert_delta_events(db, run["id"], 10, 5)  # 50 字符 < 2048 -> 1 段
                processed = storage_module._coalesce_reasoning_deltas(db)
                self.assertEqual(processed, 10)
                again = storage_module._coalesce_reasoning_deltas(db)
                self.assertEqual(again, 0, "幂等：第二次无 delta 行")
                row = db.execute(
                    "SELECT payload, sequence FROM run_events "
                    "WHERE event_type='reasoning' AND run_id=?",
                    (run["id"],),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(json.loads(row[0])["content"], "字" * 50)
                self.assertEqual(row[1], 1, "合流行使用段首 sequence")


if __name__ == "__main__":
    unittest.main()
