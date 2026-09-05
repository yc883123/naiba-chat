# -*- coding: utf-8 -*-
"""护栏：ToolExecutor 确认协议与工具执行主路径（防归位时导入遗漏类回归）。

背景：executor 从 skill_runtime 归位到 naiba/tools/executor.py 时手写导入清单曾漏掉
uuid（confirm_id 生成）与其它符号，导致真实调用路径 NameError。本测试直接走
NEED_CONFIRM 协议路径——确认流必经 uuid——且不真正写盘。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.mcp import MCPRegistry  # noqa: E402
from naiba.tools.executor import ToolExecutor  # noqa: E402


class ToolExecutorConfirmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "b.txt").write_text("x", encoding="utf-8")
        self.executor = ToolExecutor(
            self.root, sys.executable, 60, MCPRegistry([]), permission_mode="confirm"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_file_confirm_flow_generates_confirm_id(self):
        target = self.root / "a.txt"
        ok, out = self.executor.execute(
            "write_file", {"path": str(target), "content": "Hello"}, []
        )
        # confirm 模式下写文件是高风险操作：返回 NEED_CONFIRM:<id>:... 且不落盘（uuid 路径被走到）。
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)
        self.assertFalse(target.exists(), "未确认前不得写盘")

    def test_list_directory_auto_smoke(self):
        ok, out = self.executor.execute("list_directory", {"path": str(self.root)}, [])
        self.assertTrue(ok)
        self.assertIn("b.txt", out)


if __name__ == "__main__":
    unittest.main()
