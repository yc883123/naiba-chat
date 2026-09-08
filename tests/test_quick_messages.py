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
    BUILTIN_STARTER_PRESETS,
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
    """开始页「自定义指令」与快捷消息是两份独立列表（互不污染）。

    注意：开始页列表现在**包含内置预设**（一次性并入 config.starter_prompts，
    使其可编辑/可删除），因此断言改为"用户新增的条目在列表里"而不是"列表只有它"。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = ConfigStore(Path(self.tmp.name) / "config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _titles(self):
        return [item["title"] for item in self.config.get_starter_prompts()]

    def test_adding_quick_message_does_not_touch_starter_prompts(self):
        before = self._titles()
        self.config.add_starter_prompt("开始页指令", "开始页内容")
        self.config.add_quick_message("快捷内容")
        starters = self.config.get_starter_prompts()
        quick = self.config.get_quick_messages()
        self.assertIn("开始页指令", [item["title"] for item in starters])
        self.assertEqual([item["text"] for item in quick], ["快捷内容"])
        # 内置预设仍在（新增用户条目不得挤掉它们）
        self.assertTrue(set(before) <= set(self._titles()))

    def test_user_starter_prompt_shape_stays_simple(self):
        self.config.add_starter_prompt("开始页指令", "开始页内容")
        entry = next(item for item in self.config.get_starter_prompts() if item["title"] == "开始页指令")
        self.assertEqual(set(entry), {"title", "text"}, "用户新增的开始页条目不带统计/图标字段")

    def test_removing_quick_message_keeps_starter_prompts(self):
        self.config.add_starter_prompt("开始页指令", "开始页内容")
        self.config.add_quick_message("快捷内容")
        before = len(self.config.get_starter_prompts())
        self.config.remove_quick_message(0)
        self.assertEqual(len(self.config.get_starter_prompts()), before)
        self.assertIn("开始页指令", self._titles())
        self.assertEqual(self.config.get_quick_messages(), [])


class StarterPresetSeedTests(unittest.TestCase):
    """内置开始页预设：并入 → 可编辑/可删除 → 删除不复活 → 新预设自动补齐 → 一键恢复。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self.config = ConfigStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _titles(self):
        return [item["title"] for item in self.config.get_starter_prompts()]

    def test_builtin_presets_seeded_with_id_desc_and_icon(self):
        presets = [item for item in self.config.get_starter_prompts() if item.get("preset_id")]
        self.assertEqual(len(presets), len(BUILTIN_STARTER_PRESETS))
        for item in presets:
            self.assertTrue(item.get("title") and item.get("text") and item.get("desc") and item.get("icon"))
        self.assertEqual(self.config.count_missing_starter_presets(), 0)

    def test_deleted_preset_is_recorded_and_never_resurrected(self):
        index = next(i for i, item in enumerate(self.config.get_starter_prompts()) if item["title"] == "列出可用工具")
        self.config.remove_starter_prompt(index)
        self.assertNotIn("列出可用工具", self._titles())
        # 重新加载（模拟下次启动 / 升级到新版本）：不得复活
        reloaded = ConfigStore(self.path)
        self.assertNotIn("列出可用工具", [item["title"] for item in reloaded.get_starter_prompts()])
        self.assertEqual(reloaded.count_missing_starter_presets(), 1)
        self.assertIn("list-tools", reloaded.data.get("starter_presets_dismissed") or [])

    def test_renamed_preset_is_not_duplicated_on_reload(self):
        """改过标题的内置预设靠 preset_id 识别，重载后不会被当成缺失而重复插入。"""
        prompts = self.config.get_starter_prompts()
        index = next(i for i, item in enumerate(prompts) if item.get("preset_id") == "list-tools")
        self.config.update_starter_prompt(index, "列出我的工具", "列出我所有工具。")
        reloaded = ConfigStore(self.path)
        titles = [item["title"] for item in reloaded.get_starter_prompts()]
        self.assertIn("列出我的工具", titles)
        self.assertNotIn("列出可用工具", titles, "改名后不得再插一份默认预设")
        self.assertEqual(reloaded.count_missing_starter_presets(), 0)

    def test_new_builtin_preset_added_in_newer_version_is_topped_up(self):
        """版本新增的内置预设（未被删除过）在下次启动自动补齐。"""
        # 模拟"上一版还没有这条预设"：删掉它并清空删除记录（等价于从未存在）
        prompts = self.config.get_starter_prompts()
        index = next(i for i, item in enumerate(prompts) if item.get("preset_id") == "await-instructions")
        self.config.remove_starter_prompt(index)
        self.config.data["starter_presets_dismissed"] = []
        self.config.save()
        reloaded = ConfigStore(self.path)
        self.assertIn("等待用户指令", [item["title"] for item in reloaded.get_starter_prompts()])

    def test_restore_readds_missing_presets_and_clears_dismissed(self):
        self.config.remove_starter_prompt(0)
        self.assertEqual(self.config.count_missing_starter_presets(), 1)
        restored = self.config.restore_starter_presets()
        titles = [item["title"] for item in restored]
        self.assertIn(BUILTIN_STARTER_PRESETS[0]["title"], titles)
        self.assertEqual(self.config.count_missing_starter_presets(), 0)
        self.assertEqual(self.config.data.get("starter_presets_dismissed"), [])
        self.assertEqual(len(titles), len(set(titles)), "恢复后不得出现重复条目")
        # 清空清单后，再启动也不会重复
        again = ConfigStore(self.path)
        self.assertEqual(len([item for item in again.get_starter_prompts()]), len(titles))

    def test_update_preserves_desc_and_icon(self):
        prompts = self.config.get_starter_prompts()
        index = next(i for i, item in enumerate(prompts) if item.get("icon"))
        self.config.update_starter_prompt(index, "改名后的预设", "新的指令正文")
        entry = self.config.get_starter_prompts()[index]
        self.assertEqual(entry["title"], "改名后的预设")
        self.assertEqual(entry["text"], "新的指令正文")
        self.assertTrue(entry.get("desc") and entry.get("icon"), "编辑不得丢掉副标题/图标")

    def test_legacy_seed_flag_migrates_to_dismissed_list(self):
        """旧方案（一次性标记）的配置：当前缺失的内置预设视为"用户已删除"。"""
        self.config.remove_starter_prompt(0)
        legacy = ConfigStore(self.path)
        legacy.data.pop("starter_presets_dismissed", None)
        legacy.data["starter_presets_seeded"] = True
        for item in legacy.data.get("starter_prompts") or []:
            item.pop("preset_id", None)  # 模拟旧版条目（无 preset_id）
        legacy.save()
        migrated = ConfigStore(self.path)
        titles = [item["title"] for item in migrated.get_starter_prompts()]
        self.assertNotIn(BUILTIN_STARTER_PRESETS[0]["title"], titles, "旧版删掉的不该被复活")
        self.assertEqual(migrated.count_missing_starter_presets(), 1)
        self.assertIn(BUILTIN_STARTER_PRESETS[0]["id"], migrated.data.get("starter_presets_dismissed") or [])


if __name__ == "__main__":
    unittest.main()
