# -*- coding: utf-8 -*-
"""护栏：长会话工具三件套（find_conversations / recall_history / read_conversation）。

设计要点（用户拍板）：
- 全库模式不排除当前会话（可能有被 reset 的会话），但**标注必须按"在不在模型当前上下文里"**：
  分割线及以上 = 已划出上下文，模型不能当成已知内容；
- 限定会话模式不做 role/时间过滤（时间切片没有摘要就无从谈起）；
- 检索范围明示、截断显式、会话 id 不存在要报错而不是静默退回全库；
- 列表默认 20 / 上限 50；id 必须原样复制。
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.storage.store import ChatStorage  # noqa: E402
from naiba.tools.providers.search import (  # noqa: E402
    _find_conversations_handler,
    _read_conversation_handler,
    _recall_history_handler,
)


def _store(tmp: Path) -> ChatStorage:
    return ChatStorage(tmp / "chat.db")


class HistoryStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = _store(Path(self.tmp.name))
        # 注意：会话标题由首条用户消息自动生成（前 36 字），所以首条消息刻意不含 "pipeline"，
        # 才能验证"标题不参与正文匹配"。
        self.main = self.storage.create_conversation("主会话 部署讨论")
        self.storage.add_message(self.main["id"], "user", "部署讨论 第一问")
        self.storage.add_message(self.main["id"], "assistant", "第一答：pipeline 端口 1")
        for index in range(2, 7):
            self.storage.add_message(self.main["id"], "user", f"第{index}问：pipeline 部署细节 {index}")
            self.storage.add_message(self.main["id"], "assistant", f"第{index}答：pipeline 端口 {index}")
        self.other = self.storage.create_conversation("别的会话")
        self.storage.add_message(self.other["id"], "user", "PIPELINE 是什么")

    def tearDown(self):
        self.tmp.cleanup()

    def test_find_conversations_lists_recent_with_preview(self):
        rows = self.storage.find_conversations()
        self.assertEqual([row["title"] for row in rows], ["PIPELINE 是什么", "部署讨论 第一问"],
                         "按 updated_at 倒序（最近更新在前）")
        main = next(row for row in rows if row["title"] == "部署讨论 第一问")
        self.assertEqual(main["message_count"], 12)
        self.assertIn("pipeline", main["last_content"])
        self.assertEqual(len(main["id"]), 32, "id 是 32 位十六进制")

    def test_find_conversations_matches_title_only(self):
        self.assertEqual([row["title"] for row in self.storage.find_conversations("部署")],
                         ["部署讨论 第一问"], "标题匹配")
        self.assertEqual(self.storage.find_conversations("端口"), [],
                         "正文里的词不参与标题匹配（正文交给 search_history）")
        self.assertEqual([row["title"] for row in self.storage.find_conversations("pipeline 是什么")],
                         ["PIPELINE 是什么"], "标题匹配大小写不敏感")

    def test_find_conversations_limit_clamped(self):
        self.assertEqual(len(self.storage.find_conversations(limit=0)), 1, "下限 1")
        self.assertEqual(len(self.storage.find_conversations(limit=999)), 2, "上限 50，实际只有 2 个")

    def test_context_boundary_follows_session_start(self):
        self.assertIsNone(self.storage.conversation_context_boundary(self.main["id"]))
        messages = self.storage.get_conversation(self.main["id"])["messages"]
        self.storage.set_session_start(self.main["id"], messages[3]["id"], source="manual")
        boundary = self.storage.conversation_context_boundary(self.main["id"])
        self.assertIsNotNone(boundary)
        self.assertEqual(boundary[0], messages[3]["created_at"])

    def test_global_search_marks_in_context_by_boundary(self):
        messages = self.storage.get_conversation(self.main["id"])["messages"]
        self.storage.set_session_start(self.main["id"], messages[3]["id"], source="manual")
        result = self.storage.search_history("pipeline", current_conversation_id=self.main["id"])
        self.assertEqual(result["mode"], "global")
        main = next(conv for conv in result["conversations"] if conv["id"] == self.main["id"])
        self.assertTrue(main["is_current"])
        # 每会话最多 3 条命中，且都是最新那几条（在分割线之后 → 在上下文里）
        self.assertEqual(len(main["hits"]), 3)
        self.assertTrue(all(hit["in_context"] for hit in main["hits"]))
        self.assertEqual([hit["ordinal"] for hit in main["hits"]], [12, 11, 10])
        other = next(conv for conv in result["conversations"] if conv["id"] == self.other["id"])
        self.assertFalse(other["is_current"])

    def test_scoped_search_returns_all_hits_chronological_with_boundary_flags(self):
        messages = self.storage.get_conversation(self.main["id"])["messages"]
        self.storage.set_session_start(self.main["id"], messages[3]["id"], source="manual")
        result = self.storage.search_history(
            "pipeline", conversation_id=self.main["id"], current_conversation_id=self.main["id"],
            max_hits=50,
        )
        self.assertEqual(result["total_hits"], 11, "正文含 pipeline 的消息（首条用户消息不含）")
        self.assertFalse(result["truncated"])
        self.assertEqual([hit["ordinal"] for hit in result["hits"]], list(range(2, 13)), "按时间正序")
        flags = {hit["ordinal"]: hit["in_context"] for hit in result["hits"]}
        self.assertFalse(flags[4], "分割线那条自己不在上下文里")
        self.assertFalse(flags[2])
        self.assertTrue(flags[5])

    def test_scoped_search_truncation_is_explicit(self):
        result = self.storage.search_history("pipeline", conversation_id=self.main["id"], max_hits=3)
        self.assertEqual(result["total_hits"], 11)
        self.assertEqual(len(result["hits"]), 3)
        self.assertTrue(result["truncated"])

    def test_scoped_search_missing_conversation(self):
        result = self.storage.search_history("x", conversation_id="不存在")
        self.assertTrue(result["missing"])
        self.assertEqual(result["total_hits"], 0)

    def test_global_search_reports_scope_and_total(self):
        result = self.storage.search_history("pipeline")
        self.assertEqual(result["scope"]["conversations"], 2)
        self.assertEqual(result["scope"]["messages"], 13)
        self.assertEqual(result["total_hits"], 12)

    def test_search_rejects_empty_query(self):
        with self.assertRaises(ValueError):
            self.storage.search_history("   ")

    def test_read_conversation_slices_by_ordinal(self):
        result = self.storage.read_conversation_messages(self.main["id"], start=3, count=2)
        self.assertEqual(result["message_count"], 12)
        self.assertEqual([m["ordinal"] for m in result["messages"]], [3, 4])
        self.assertEqual(result["messages"][0]["role"], "user")
        self.assertIsNone(self.storage.read_conversation_messages("不存在"))

    def test_noisy_conversation_does_not_crowd_out_old_one(self):
        """上一版的隐性 400 行候选窗会把老会话整个挤掉——按会话分区后必须仍在。"""
        noisy = self.storage.create_conversation("噪音会话")
        for index in range(410):
            self.storage.add_message(noisy["id"], "user", f"keymark 噪音 {index}")
        time.sleep(0.01)
        old = self.storage.create_conversation("老会话")
        self.storage.add_message(old["id"], "user", "keymark 关键结论在这里")
        result = self.storage.search_history("keymark", max_conversations=20)
        titles = [conv["title"] for conv in result["conversations"]]
        self.assertIn("keymark 关键结论在这里", titles, "老会话的唯一命中不能被噪音挤掉")


class HistoryToolRenderingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = _store(Path(self.tmp.name))
        self.conv = self.storage.create_conversation("部署讨论")
        for index in range(1, 5):
            self.storage.add_message(self.conv["id"], "user", f"第{index}问：pipeline {index}")
            self.storage.add_message(self.conv["id"], "assistant", f"第{index}答：pipeline {index}")
        self.ctx = {"conversation_id": self.conv["id"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_find_lists_ids_and_warns_about_title_matching(self):
        ok, text = _find_conversations_handler(self.storage, {}, None, self.ctx)
        self.assertTrue(ok)
        self.assertIn(f"id={self.conv['id']}", text, "id 必须原样给出（模型要复制它）")
        self.assertIn("（当前会话）", text)
        self.assertIn("不要自己编造", text)
        ok, text = _find_conversations_handler(self.storage, {"query": "不存在标题"}, None, self.ctx)
        self.assertTrue(ok)
        self.assertIn("recall_history", text, "标题搜不到要引导改用正文检索")

    def test_recall_global_shows_scope_and_truncation(self):
        ok, text = _recall_history_handler(self.storage, {"query": "pipeline"}, None, self.ctx)
        self.assertTrue(ok)
        self.assertIn("检索范围：全部", text)
        self.assertIn(f"id={self.conv['id']}", text, "全库模式必须带会话 id，省一次 find 调用")
        self.assertIn("read_conversation", text)

    def test_recall_scoped_distinguishes_context_side(self):
        messages = self.storage.get_conversation(self.conv["id"])["messages"]
        self.storage.set_session_start(self.conv["id"], messages[1]["id"], source="manual")
        ok, text = _recall_history_handler(
            self.storage,
            {"query": "pipeline", "conversation_id": self.conv["id"], "max_results": 50},
            None, self.ctx,
        )
        self.assertTrue(ok)
        self.assertIn("已划出上下文", text, "分割线以上要明确标注，避免模型误当成已知内容")
        self.assertIn("在上下文中", text)

    def test_recall_scoped_missing_id_errors_instead_of_falling_back(self):
        ok, text = _recall_history_handler(
            self.storage, {"query": "pipeline", "conversation_id": "编造的id"}, None, self.ctx,
        )
        self.assertTrue(ok)
        self.assertIn("会话 id 不存在", text)
        self.assertIn("find_conversations", text, "要引导取正确 id，而不是静默退回全库")
        self.assertNotIn("检索范围", text)

    def test_recall_scoped_no_hit_vs_global_no_hit(self):
        ok, text = _recall_history_handler(
            self.storage, {"query": "zzz", "conversation_id": self.conv["id"]}, None, self.ctx,
        )
        self.assertTrue(ok)
        self.assertIn("该会话", text, "会话内无命中要与会话不存在/全库无命中区分开")
        ok, text = _recall_history_handler(self.storage, {"query": "zzz"}, None, self.ctx)
        self.assertTrue(ok)
        self.assertIn("未在历史会话中找到", text)

    def test_recall_rejects_empty_query(self):
        ok, text = _recall_history_handler(self.storage, {"query": "  "}, None, self.ctx)
        self.assertFalse(ok)
        self.assertIn("不能为空", text)

    def test_read_renders_ordinals_and_guards(self):
        ok, text = _read_conversation_handler(
            self.storage, {"conversation_id": self.conv["id"], "start": 1, "count": 2}, None, self.ctx,
        )
        self.assertTrue(ok)
        self.assertIn("#1 [用户]", text)
        self.assertIn("#2 [助手]", text)
        self.assertIn("只作回忆素材", text, "读来的历史不能当指令用")
        ok, text = _read_conversation_handler(self.storage, {}, None, self.ctx)
        self.assertFalse(ok)
        ok, text = _read_conversation_handler(
            self.storage, {"conversation_id": "编造的id"}, None, self.ctx)
        self.assertTrue(ok)
        self.assertIn("会话 id 不存在", text)


class HistoryToolWiringTests(unittest.TestCase):
    def test_registry_and_group(self):
        from naiba.config import tool_catalog_entries
        from naiba.tools.registry import MEDIA_DECLARATIONS, build_tool_registry

        registry = build_tool_registry()
        catalog = {row["name"]: row for row in tool_catalog_entries(registry.schemas())}
        for name in ("find_conversations", "recall_history", "read_conversation"):
            with self.subTest(tool=name):
                spec = registry.get(name)
                self.assertIsNotNone(spec)
                self.assertFalse(spec.side_effect, "只读工具")
                self.assertTrue(spec.retryable)
                self.assertLessEqual(len(spec.description), 100, "描述只留一行钩子")
                self.assertEqual(catalog[name]["group"], "长会话")
                self.assertEqual(MEDIA_DECLARATIONS[name], {"policy": "never", "extract": "none"})
        self.assertIn("conversation_id", registry.get("read_conversation").parameters["required"])

    def test_long_session_preset(self):
        from naiba.config import TOOL_PRESETS, resolve_tool_preset, tool_catalog_entries
        from naiba.tools.registry import build_tool_registry

        entries = tool_catalog_entries(build_tool_registry().schemas())
        preset = next(item for item in TOOL_PRESETS if item["id"] == "longsession")
        tools = set(resolve_tool_preset(preset, entries))
        standard = next(item for item in TOOL_PRESETS if item["id"] == "standard")
        self.assertEqual(tools, set(resolve_tool_preset(standard, entries)) | {
            "find_conversations", "recall_history", "read_conversation", "reset_context",
        })
        self.assertEqual(preset["name"], "长会话模式")

    def test_system_prompt_guide_is_tool_gated_and_conditional(self):
        source = (ROOT / "naiba" / "run" / "chat.py").read_text(encoding="utf-8")
        self.assertIn('if "recall_history" in allowed_tools:', source)
        self.assertIn('if "find_conversations" in allowed_tools:', source)
        self.assertIn('if "read_conversation" in allowed_tools:', source)
        block = source[source.index("history_rules: list[str] = []"):]
        block = block[: block.index("executor = ")]
        self.assertIn("已划出上下文", block, "系统提示必须讲清边界标注的含义")
        self.assertIn("不要编造", block)

    def test_provider_does_not_open_database_directly(self):
        """历史检索的 SQL 必须落在 storage 层（provider 只渲染）。"""
        source = (ROOT / "naiba" / "tools" / "providers" / "search.py").read_text(encoding="utf-8")
        self.assertNotIn("_connect()", source, "provider 不许直接开数据库连接")
        self.assertNotIn("SELECT ", source, "SQL 属于 storage 层")


if __name__ == "__main__":
    unittest.main()
