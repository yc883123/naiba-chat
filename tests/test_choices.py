# -*- coding: utf-8 -*-
"""护栏：选项识别 _detect_choice_groups 行为规格。

保护对象：阶段 1 将 _detect_choice_groups 迁出 server.py 到 core/choices.py 时的行为等价性。
选项识别在 1.6.6-beta 刚做过收紧（只认明确意图、剥离编号残留、紧凑选项拆分），
是本项目回归风险最高的纯函数之一。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import _detect_choice_groups, _detect_choices  # noqa: E402


class ChoiceDetectionTests(unittest.TestCase):
    def test_compact_cjk_same_line(self):
        groups = _detect_choice_groups("请选择语言：1. 中文 2. 英文")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["choices"], ["中文", "英文"])
        self.assertIn("请选择", groups[0]["prompt"])

    def test_numbered_list_with_cue(self):
        groups = _detect_choice_groups("请选择一个方案：\n1. 方案A\n2. 方案B\n3. 方案C")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["choices"], ["方案A", "方案B", "方案C"])

    def test_bullet_with_number_residue_stripped(self):
        groups = _detect_choice_groups("请选择：\n- 1. 安装依赖\n- 2. 跳过")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["choices"], ["安装依赖", "跳过"])

    def test_lettered_english_phrase(self):
        groups = _detect_choice_groups("Pick one:\nA. Option 1\nB. Option 2")
        self.assertEqual(groups[0]["choices"], ["Option 1", "Option 2"])

    def test_which_one_phrase(self):
        groups = _detect_choice_groups("Which one do you prefer?\n1. X\n2. Y")
        self.assertEqual(groups[0]["choices"], ["X", "Y"])

    def test_bare_select_not_a_cue(self):
        groups = _detect_choice_groups("SELECT * FROM users\n1. a\n2. b")
        self.assertEqual(groups, [])

    def test_cue_too_far_from_choices(self):
        groups = _detect_choice_groups("请选择：\n\n\n1. A\n2. B")
        self.assertEqual(groups, [])

    def test_blank_line_gap_within_distance(self):
        groups = _detect_choice_groups("请选择：\n\n1. A\n2. B")
        self.assertEqual(groups[0]["choices"], ["A", "B"])

    def test_overlong_option_rejected(self):
        long_option = "这是一段非常长的选项文本" + "很长" * 30
        groups = _detect_choice_groups(f"请选择：\n1. {long_option}\n2. 短选项")
        self.assertEqual(groups, [])

    def test_overlong_cue_rejected(self):
        long_cue = "前半部分" * 20 + "请选择" + "后半部分" * 20
        groups = _detect_choice_groups(f"{long_cue}\n1. A\n2. B")
        self.assertEqual(groups, [])

    def test_non_consecutive_numbering_rejected(self):
        groups = _detect_choice_groups("请选择：\n1. A\n3. C")
        self.assertEqual(groups, [])

    def test_circled_numbers(self):
        groups = _detect_choice_groups("请选择：\n① A\n② B")
        self.assertEqual(groups[0]["choices"], ["A", "B"])

    def test_fenced_code_excluded(self):
        text = "这里没有选项\n```\n1. 代码行\n2. 代码行二\n```"
        self.assertEqual(_detect_choice_groups(text), [])

    def test_skill_manual_not_choices(self):
        text = '使用说明 <skill name="x">……</skill>\n1. 步骤A\n2. 步骤B'
        self.assertEqual(_detect_choice_groups(text), [])

    def test_legacy_first_group_helper(self):
        self.assertEqual(_detect_choices("请选择：\n1. A\n2. B"), ["A", "B"])
        self.assertEqual(_detect_choices("无选项的普通文本"), [])


if __name__ == "__main__":
    unittest.main()
