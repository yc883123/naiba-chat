# -*- coding: utf-8 -*-
"""护栏：工具描述瘦身（均衡档）——模型可见描述的简洁性、术语清洁与职责边界。

防回潮规则：
- 描述 ≤ 100 字、≤ 3 个句号句（规则一律迁系统提示常驻区，描述只答"干什么/何时用/关键参数"）；
- 禁止内部术语（Harness/ToolRegistry/ToolSpec/宿主 等用户视角不可见词）；
- 禁止跨工具编排长句式（系统提示常驻区负责编排规则，描述只留一行钩子）。
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.tools.registry import build_tool_registry  # noqa: E402

INTERNAL_TERMS = ("Harness", "ToolRegistry", "ToolSpec", "宿主")
# 跨工具编排句式：描述里出现即视为职责越界（应从描述迁出）
ORCHESTRATION_PHRASES = ("先用", "随后用", "然后用", "再调用", "最后用", "再利用", "必须先", "应该先", "提交前先用")
MAX_DESC_LEN = 100
MAX_SENTENCES = 3


class ToolDescriptionSlimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = build_tool_registry().schemas()

    def test_descriptions_within_budget(self) -> None:
        for row in self.rows:
            desc = str(row["description"] or "")
            with self.subTest(tool=row["name"]):
                self.assertLessEqual(
                    len(desc), MAX_DESC_LEN,
                    f"{row['name']}: 描述超长（{len(desc)} 字）——规则应迁系统提示常驻区",
                )

    def test_sentence_count_within_budget(self) -> None:
        for row in self.rows:
            desc = str(row["description"] or "")
            sentences = [s for s in re.split(r"。", desc) if s.strip()]
            with self.subTest(tool=row["name"]):
                self.assertLessEqual(len(sentences), MAX_SENTENCES, f"{row['name']}: 句数过多")

    def test_no_internal_terms(self) -> None:
        for row in self.rows:
            desc = str(row["description"] or "")
            for term in INTERNAL_TERMS:
                with self.subTest(tool=row["name"], term=term):
                    self.assertNotIn(term, desc, f"{row['name']}: 描述含内部术语「{term}」")

    def test_no_cross_tool_orchestration_sentences(self) -> None:
        for row in self.rows:
            desc = str(row["description"] or "")
            for phrase in ORCHESTRATION_PHRASES:
                with self.subTest(tool=row["name"], phrase=phrase):
                    self.assertNotIn(phrase, desc, "跨工具编排规则应放系统提示常驻区，描述只留一行钩子")


if __name__ == "__main__":
    unittest.main()
