# -*- coding: utf-8 -*-
"""护栏：快捷消息（会话内面板专用列表）的统计、排序与「与开始页自定义指令互不干扰」。

保护对象：
- 快捷消息条目字段 index/count/added_at/used_at（旧数据缺字段按 0 兼容）；
- 编辑只改标题与内容、不动统计；删除后 index 重新变密；
- 排序权重 min(点击次数,50)×2 + 新鲜度加分（7 天 +6 / 30 天 +3 / 90 天 +1），
  并列取新增时间倒序；
- 两份列表独立：开始页 starter_prompts 与快捷消息 quick_messages 互不影响。
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.config import (  # noqa: E402
    QUICK_MESSAGE_USE_CAP,
    ConfigStore,
    quick_message_score,
)

DAY_MS = 86400000


class QuickMessageStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = ConfigStore(Path(self.tmp.name) / "config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_sets_stats_and_index(self):
        messages = self.config.add_quick_message("把下面的工作流整理成步骤")
        self.assertEqual(len(messages), 1)
        entry = messages[0]
        self.assertEqual(entry["index"], 0)
        self.assertEqual(entry["text"], "把下面的工作流整理成步骤")
        self.assertNotIn("title", entry, "快捷消息只有正文，不设标题")
        self.assertEqual(entry["count"], 0)
        self.assertEqual(entry["used_at"], 0)
        self.assertGreater(entry["added_at"], 0)

    def test_record_use_increments_count_and_used_at(self):
        self.config.add_quick_message("内容 A")
        self.config.add_quick_message("内容 B")
        self.config.record_quick_message_use(1)
        messages = self.config.get_quick_messages()
        self.assertEqual(messages[1]["count"], 1)
        self.assertGreater(messages[1]["used_at"], 0)
        self.assertEqual(messages[0]["count"], 0, "未使用的条目不受影响")
        self.config.record_quick_message_use(1)
        self.assertEqual(self.config.get_quick_messages()[1]["count"], 2)

    def test_record_use_out_of_range_is_noop(self):
        self.config.add_quick_message("内容 A")
        self.config.record_quick_message_use(9)
        self.assertEqual(self.config.get_quick_messages()[0]["count"], 0)

    def test_update_keeps_stats(self):
        self.config.add_quick_message("内容 A")
        self.config.record_quick_message_use(0)
        before = self.config.get_quick_messages()[0]
        self.config.update_quick_message(0, "内容 A2")
        after = self.config.get_quick_messages()[0]
        self.assertEqual(after["text"], "内容 A2")
        self.assertEqual(after["count"], before["count"], "编辑不得清空使用次数")
        self.assertEqual(after["added_at"], before["added_at"], "编辑不得改写新增时间")

    def test_remove_keeps_index_dense(self):
        self.config.add_quick_message("内容 A")
        self.config.add_quick_message("内容 B")
        self.config.add_quick_message("内容 C")
        messages = self.config.remove_quick_message(1)
        self.assertEqual([item["text"] for item in messages], ["内容 A", "内容 C"])
        self.assertEqual([item["index"] for item in messages], [0, 1], "删除后 index 重新变密")

    def test_legacy_entries_without_stats_are_normalized(self):
        self.config.data["quick_messages"] = [{"text": "旧内容"}]
        self.config.save()
        entry = self.config.get_quick_messages()[0]
        self.assertEqual(entry["count"], 0)
        self.assertEqual(entry["added_at"], 0)
        self.assertEqual(entry["used_at"], 0)
        self.assertEqual(entry["index"], 0)

    def test_legacy_title_field_is_dropped(self):
        # 旧数据（曾带 title）读取时只保留正文与统计，标题字段不再返回。
        self.config.data["quick_messages"] = [{"title": "旧标题", "text": "旧正文", "count": 2}]
        self.config.save()
        entry = self.config.get_quick_messages()[0]
        self.assertEqual(entry["text"], "旧正文")
        self.assertEqual(entry["count"], 2)
        self.assertNotIn("title", entry)

    def test_blank_text_rejected(self):
        with self.assertRaises(ValueError):
            self.config.add_quick_message("   ")


class QuickMessageScoreTests(unittest.TestCase):
    NOW = 1_800_000_000_000

    def _score(self, count, age_days):
        return quick_message_score(
            {"count": count, "added_at": self.NOW - int(age_days * DAY_MS)}, self.NOW
        )

    def test_count_weight(self):
        self.assertEqual(self._score(3, 200), 6.0)

    def test_count_capped(self):
        self.assertEqual(self._score(QUICK_MESSAGE_USE_CAP + 10, 200), QUICK_MESSAGE_USE_CAP * 2)

    def test_recency_bonus_ladder(self):
        self.assertEqual(self._score(0, 1), 6.0)
        self.assertEqual(self._score(0, 10), 3.0)
        self.assertEqual(self._score(0, 40), 1.0)
        self.assertEqual(self._score(0, 200), 0.0)

    def test_new_entry_beats_stale_zero_use_entry(self):
        self.assertGreater(self._score(0, 1), self._score(0, 200))

    def test_legacy_entry_without_added_at_has_no_bonus(self):
        self.assertEqual(quick_message_score({"count": 0, "added_at": 0}, self.NOW), 0.0)


class QuickMessageSortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = ConfigStore(Path(self.tmp.name) / "config.json")
        now = int(time.time() * 1000)
        self.config.data["quick_messages"] = [
            {"text": "高频", "count": 5, "added_at": now - 200 * DAY_MS, "used_at": now},
            {"text": "新条目", "count": 0, "added_at": now - DAY_MS, "used_at": 0},
            {"text": "中频", "count": 2, "added_at": now - 100 * DAY_MS, "used_at": now},
        ]
        self.config.save()

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_order_is_insertion_order(self):
        texts = [item["text"] for item in self.config.get_quick_messages()]
        self.assertEqual(texts, ["高频", "新条目", "中频"])

    def test_usage_sort_ranks_by_weighted_score(self):
        # 权重：高频 5 次×2=10 > 新条目 0 次+7 天加分 6 > 中频 2 次×2=4（100 天无加分）。
        # 新条目靠新鲜度加分越过"很久以前点过两次"的条目，是刻意设计（防新条目永远沉底）。
        messages = self.config.get_quick_messages("usage")
        self.assertEqual([item["text"] for item in messages], ["高频", "新条目", "中频"])
        self.assertEqual([item["index"] for item in messages], [0, 1, 2], "index 仍是原始插入序号")

    def test_usage_sort_tie_breaks_by_newest_added(self):
        now = int(time.time() * 1000)
        self.config.data["quick_messages"] = [
            {"text": "旧", "count": 1, "added_at": now - 100 * DAY_MS},
            {"text": "新", "count": 1, "added_at": now - 100 * DAY_MS + 1000},
        ]
        self.config.save()
        self.assertEqual(
            [item["text"] for item in self.config.get_quick_messages("usage")], ["新", "旧"]
        )


class PromptListsIndependenceTests(unittest.TestCase):
    """开始页「自定义指令」与快捷消息是两份独立列表（互不污染）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = ConfigStore(Path(self.tmp.name) / "config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_adding_quick_message_does_not_touch_starter_prompts(self):
        self.config.add_starter_prompt("开始页指令", "开始页内容")
        self.config.add_quick_message("快捷内容")
        starters = self.config.get_starter_prompts()
        quick = self.config.get_quick_messages()
        self.assertEqual([item["title"] for item in starters], ["开始页指令"])
        self.assertEqual([item["text"] for item in quick], ["快捷内容"])

    def test_starter_prompt_shape_stays_simple(self):
        self.config.add_starter_prompt("开始页指令", "开始页内容")
        entry = self.config.get_starter_prompts()[0]
        self.assertEqual(set(entry), {"title", "text"}, "开始页条目不再带使用统计字段")

    def test_removing_quick_message_keeps_starter_prompts(self):
        self.config.add_starter_prompt("开始页指令", "开始页内容")
        self.config.add_quick_message("快捷内容")
        self.config.remove_quick_message(0)
        self.assertEqual(len(self.config.get_starter_prompts()), 1)
        self.assertEqual(self.config.get_quick_messages(), [])


if __name__ == "__main__":
    unittest.main()
