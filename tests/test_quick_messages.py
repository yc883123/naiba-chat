# -*- coding: utf-8 -*-
"""护栏：快捷消息（复用「自定义指令」数据）的使用统计与排序规格。

保护对象：
- 每条附带的 index/count/added_at/used_at 字段（旧配置缺字段按 0 兼容）；
- 编辑只改标题与内容、不动统计；
- 排序权重 min(点击次数,50)×2 + 新鲜度加分（7 天 +6 / 30 天 +3 / 90 天 +1），
  并列取新增时间倒序；默认顺序仍是插入顺序（开始新对话页卡片行为不变）。
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.config import (  # noqa: E402
    STARTER_PROMPT_USE_CAP,
    ConfigStore,
    starter_prompt_score,
)

DAY_MS = 86400000


class StarterPromptStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = ConfigStore(Path(self.tmp.name) / "config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_sets_stats_and_index(self):
        prompts = self.config.add_starter_prompt("整理工作流", "把下面的工作流整理成步骤")
        self.assertEqual(len(prompts), 1)
        entry = prompts[0]
        self.assertEqual(entry["index"], 0)
        self.assertEqual(entry["count"], 0)
        self.assertEqual(entry["used_at"], 0)
        self.assertGreater(entry["added_at"], 0)

    def test_record_use_increments_count_and_used_at(self):
        self.config.add_starter_prompt("A", "内容 A")
        self.config.add_starter_prompt("B", "内容 B")
        self.config.record_starter_prompt_use(1)
        prompts = self.config.get_starter_prompts()
        self.assertEqual(prompts[1]["count"], 1)
        self.assertGreater(prompts[1]["used_at"], 0)
        self.assertEqual(prompts[0]["count"], 0, "未使用的条目不受影响")
        self.config.record_starter_prompt_use(1)
        self.assertEqual(self.config.get_starter_prompts()[1]["count"], 2)

    def test_record_use_out_of_range_is_noop(self):
        self.config.add_starter_prompt("A", "内容 A")
        self.config.record_starter_prompt_use(9)
        self.assertEqual(self.config.get_starter_prompts()[0]["count"], 0)

    def test_update_keeps_stats(self):
        self.config.add_starter_prompt("A", "内容 A")
        self.config.record_starter_prompt_use(0)
        before = self.config.get_starter_prompts()[0]
        self.config.update_starter_prompt(0, "A2", "内容 A2")
        after = self.config.get_starter_prompts()[0]
        self.assertEqual(after["title"], "A2")
        self.assertEqual(after["text"], "内容 A2")
        self.assertEqual(after["count"], before["count"], "编辑不得清空使用次数")
        self.assertEqual(after["added_at"], before["added_at"], "编辑不得改写新增时间")

    def test_remove_keeps_index_dense(self):
        self.config.add_starter_prompt("A", "内容 A")
        self.config.add_starter_prompt("B", "内容 B")
        self.config.add_starter_prompt("C", "内容 C")
        prompts = self.config.remove_starter_prompt(1)
        self.assertEqual([item["title"] for item in prompts], ["A", "C"])
        self.assertEqual([item["index"] for item in prompts], [0, 1], "删除后 index 重新变密")

    def test_legacy_entries_without_stats_are_normalized(self):
        self.config.data["starter_prompts"] = [{"title": "旧", "text": "旧内容"}]
        self.config.save()
        entry = self.config.get_starter_prompts()[0]
        self.assertEqual(entry["count"], 0)
        self.assertEqual(entry["added_at"], 0)
        self.assertEqual(entry["used_at"], 0)
        self.assertEqual(entry["index"], 0)

    def test_blank_text_rejected(self):
        with self.assertRaises(ValueError):
            self.config.add_starter_prompt("标题", "   ")


class StarterPromptScoreTests(unittest.TestCase):
    NOW = 1_800_000_000_000

    def _score(self, count, age_days):
        return starter_prompt_score(
            {"count": count, "added_at": self.NOW - int(age_days * DAY_MS)}, self.NOW
        )

    def test_count_weight(self):
        self.assertEqual(self._score(3, 200), 6.0)

    def test_count_capped(self):
        self.assertEqual(self._score(STARTER_PROMPT_USE_CAP + 10, 200), STARTER_PROMPT_USE_CAP * 2)

    def test_recency_bonus_ladder(self):
        self.assertEqual(self._score(0, 1), 6.0)
        self.assertEqual(self._score(0, 10), 3.0)
        self.assertEqual(self._score(0, 40), 1.0)
        self.assertEqual(self._score(0, 200), 0.0)

    def test_new_entry_beats_stale_zero_use_entry(self):
        self.assertGreater(self._score(0, 1), self._score(0, 200))

    def test_legacy_entry_without_added_at_has_no_bonus(self):
        self.assertEqual(starter_prompt_score({"count": 0, "added_at": 0}, self.NOW), 0.0)


class StarterPromptSortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = ConfigStore(Path(self.tmp.name) / "config.json")
        now = int(time.time() * 1000)
        self.config.data["starter_prompts"] = [
            {"title": "高频", "text": "a", "count": 5, "added_at": now - 200 * DAY_MS, "used_at": now},
            {"title": "新条目", "text": "b", "count": 0, "added_at": now - DAY_MS, "used_at": 0},
            {"title": "中频", "text": "c", "count": 2, "added_at": now - 100 * DAY_MS, "used_at": now},
        ]
        self.config.save()

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_order_is_insertion_order(self):
        titles = [item["title"] for item in self.config.get_starter_prompts()]
        self.assertEqual(titles, ["高频", "新条目", "中频"])

    def test_usage_sort_ranks_by_weighted_score(self):
        # 权重：高频 5 次×2=10 > 新条目 0 次+7 天加分 6 > 中频 2 次×2=4（100 天无加分）。
        # 新条目靠新鲜度加分越过"很久以前点过两次"的条目，是刻意设计（防新条目永远沉底）。
        prompts = self.config.get_starter_prompts("usage")
        self.assertEqual([item["title"] for item in prompts], ["高频", "新条目", "中频"])
        self.assertEqual([item["index"] for item in prompts], [0, 1, 2], "index 仍是原始插入序号")

    def test_usage_sort_tie_breaks_by_newest_added(self):
        now = int(time.time() * 1000)
        self.config.data["starter_prompts"] = [
            {"title": "旧", "text": "a", "count": 1, "added_at": now - 100 * DAY_MS},
            {"title": "新", "text": "b", "count": 1, "added_at": now - 100 * DAY_MS + 1000},
        ]
        self.config.save()
        self.assertEqual(
            [item["title"] for item in self.config.get_starter_prompts("usage")], ["新", "旧"]
        )


if __name__ == "__main__":
    unittest.main()
