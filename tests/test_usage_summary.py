# -*- coding: utf-8 -*-
"""用量汇总契约：逐次请求明细（requests_detail）生成与兼容。

保护对象：naiba/skills/agent.py `SkillAgent._summarize_usage`——
1. 每次请求 token/耗时进 records（带 request_ms）后，汇总携带逐次明细；
2. 旧数据 record 无 request_ms 时明细不报错（display 端显示 —）；
3. 明细与汇总口径独立：汇总仍为最后一次请求口径（命中率不被多轮稀释）。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.skills.agent import SkillAgent  # noqa: E402


class UsageSummaryTests(unittest.TestCase):
    def test_requests_detail_includes_each_request(self):
        records = [
            {"input_tokens": 1000, "output_tokens": 50, "cached_tokens": 950, "request_ms": 2100},
            {"input_tokens": 2000, "output_tokens": 60, "cached_tokens": 1800, "request_ms": 3100},
            {"input_tokens": 3000, "output_tokens": 70, "cached_tokens": 2700, "request_ms": 4100},
        ]
        summary = SkillAgent._summarize_usage(records)
        detail = summary["requests_detail"]
        self.assertEqual(len(detail), 3)
        self.assertEqual(detail[0]["index"], 1)
        self.assertEqual(detail[0]["input_tokens"], 1000)
        self.assertEqual(detail[0]["request_ms"], 2100)
        self.assertEqual(detail[2]["request_ms"], 4100)
        # 汇总口径不变：最后一次请求（不求和）。
        self.assertEqual(summary["input_tokens"], 3000)
        self.assertEqual(summary["requests"], 3)

    def test_legacy_records_without_request_ms_tolerated(self):
        records = [{"input_tokens": 100, "output_tokens": 10, "cached_tokens": 50}]
        summary = SkillAgent._summarize_usage(records)
        self.assertEqual(summary["requests_detail"][0]["request_ms"], 0)
        self.assertEqual(summary["requests_detail"][0]["total_tokens"], 110)

    def test_empty_records_no_detail(self):
        self.assertEqual(SkillAgent._summarize_usage([]), {})


if __name__ == "__main__":
    unittest.main()
