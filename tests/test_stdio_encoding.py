# -*- coding: utf-8 -*-
"""标准输出编码守门：包内中文输出不得因宿主控制台编码而抛异常。

事故（GitHub Actions windows runner 实测，2026-02 发布流水线红）：
``naiba/jobs.py`` 工作线程里的 ``print("…产物已写回消息…")`` 在 cp1252 控制台上
抛 ``UnicodeEncodeError: 'charmap' codec can't encode``，直接把单元测试打成失败。
根因是**库代码假定了宿主控制台编码**——本机（UTF-8 控制台）永远复现不了。

两道防线（本文件逐条守门）：
1. ``naiba`` 包导入期把 stdout/stderr 切成 UTF-8（``ensure_utf8_stdio``，唯一实现）；
2. Job 工作线程等"可能在任意宿主里跑"的库代码禁止裸 ``print``，一律走 ``logging``。
"""

import ast
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core.diagnostics import ensure_utf8_stdio  # noqa: E402


def _cp1252_stream() -> io.TextIOWrapper:
    """构造一个 strict cp1252 文本流：模拟 Windows 英文控制台。"""
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")


class EnsureUtf8StdioTests(unittest.TestCase):
    def test_strict_cp1252_stream_rejects_chinese(self) -> None:
        """先固化事故前提：未修复前中文确实写不进去（否则本文件其余断言没意义）。"""
        stream = _cp1252_stream()
        with self.assertRaises(UnicodeEncodeError):
            print("产物已写回消息", file=stream)

    def test_ensure_utf8_stdio_makes_chinese_writable(self) -> None:
        stream = _cp1252_stream()
        ensure_utf8_stdio([stream])
        print("产物已写回消息", file=stream)
        stream.flush()
        self.assertEqual(stream.buffer.getvalue(), "产物已写回消息\r\n".encode("utf-8"))

    def test_ensure_utf8_stdio_tolerates_streams_without_reconfigure(self) -> None:
        """无控制台（pythonw / 冻结版 / 被测试框架替换的流）必须静默跳过，不得抛异常。"""
        class _Dumb:
            def write(self, _text):  # 故意不提供 reconfigure
                return 0

        ensure_utf8_stdio([_Dumb(), object(), None])

    def test_package_import_applies_utf8_stdio(self) -> None:
        """包导入期必须调用唯一实现（防止有人顺手删掉这行而流水线再次变红）。"""
        source = (ROOT / "naiba" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("from naiba.core.diagnostics import ensure_utf8_stdio", source)
        self.assertIn("_ensure_utf8_stdio()", source)


class LibraryPrintBanTests(unittest.TestCase):
    """Job 工作线程跑在没有编码保障的宿主里，禁止裸 print。"""

    def test_jobs_module_has_no_bare_print(self) -> None:
        source = (ROOT / "naiba" / "jobs.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ]
        self.assertEqual(
            offenders, [],
            f"naiba/jobs.py 不得用裸 print（{offenders} 行）——"
            "改用 logger.info/warning/exception（控制台编码不可假定）",
        )


if __name__ == "__main__":
    unittest.main()
