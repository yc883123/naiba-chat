# -*- coding: utf-8 -*-
"""`/api/file` 单区间 Range 解析守门（视频/音频可 seek 的前提）。

语义（与主流静态服务器一致）：
- 无 Range / 非 bytes 单位 / 语法非法 / 多区间 → None（整包 200）；
- 语法合法但不可满足（起点越界、空文件、后缀 0）→ ValueError（416 + bytes */size）；
- 越界终点自动夹到 size-1；后缀长度超过文件时取全文件。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.core.http_range import content_range_header, parse_byte_range  # noqa: E402


class ParseByteRangeTests(unittest.TestCase):
    SIZE = 1000

    def test_no_range_header(self) -> None:
        for raw in (None, "", "   "):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_byte_range(raw, self.SIZE))

    def test_other_units_and_malformed_are_ignored(self) -> None:
        for raw in ("items=0-10", "bytes=", "bytes=abc", "bytes=abc-def", "bytes=0-1,5-6", "bytes=1-"):
            with self.subTest(raw=raw):
                result = parse_byte_range(raw, self.SIZE)
                if raw == "bytes=1-":
                    self.assertEqual(result, (1, self.SIZE - 1))
                else:
                    self.assertIsNone(result)

    def test_normal_ranges(self) -> None:
        self.assertEqual(parse_byte_range("bytes=0-0", self.SIZE), (0, 0))
        self.assertEqual(parse_byte_range("bytes=100-199", self.SIZE), (100, 199))
        self.assertEqual(parse_byte_range("bytes=500-", self.SIZE), (500, self.SIZE - 1))
        self.assertEqual(parse_byte_range("bytes=-200", self.SIZE), (self.SIZE - 200, self.SIZE - 1))

    def test_clamped_ranges(self) -> None:
        # 终点越界 → 夹到 size-1；后缀超长 → 全文件
        self.assertEqual(parse_byte_range("bytes=900-99999", self.SIZE), (900, self.SIZE - 1))
        self.assertEqual(parse_byte_range("bytes=-99999", self.SIZE), (0, self.SIZE - 1))

    def test_reversed_range_ignored(self) -> None:
        self.assertIsNone(parse_byte_range("bytes=10-5", self.SIZE))

    def test_unsatisfiable_raises(self) -> None:
        for raw in ("bytes=1000-", "bytes=1000-2000", "bytes=-0"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_byte_range(raw, self.SIZE)

    def test_empty_file_unsatisfiable(self) -> None:
        for raw in ("bytes=0-", "bytes=-1", "bytes=0-0"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    parse_byte_range(raw, 0)

    def test_negative_size_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_byte_range("bytes=0-1", -1)

    def test_content_range_header(self) -> None:
        self.assertEqual(content_range_header(0, 99, 1000), "bytes 0-99/1000")
        self.assertEqual(content_range_header(900, 999, 1000), "bytes 900-999/1000")


if __name__ == "__main__":
    unittest.main()
