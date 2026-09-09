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

from tool_testkit import wired_executor  # noqa: E402


class ToolExecutorConfirmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "b.txt").write_text("x", encoding="utf-8")
        self.executor = wired_executor(self.root, mode="confirm")

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

    def test_need_confirm_reason_survives_colon_split(self):
        """回归：NEED_CONFIRM 协议以半角冒号分三段（agent.py split(":", 3)），

        Windows 盘符路径（C:\\…）含半角冒号曾把确认描述截断为「写入文件：C」；
        生成端已把确认理由冒号全角化，此处复刻解析端行为验证不截断。
        """
        target = self.root / "a.txt"
        ok, out = self.executor.execute("write_file", {"path": str(target), "content": "x"}, [])
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)
        parts = out.split(":", 3)
        self.assertGreaterEqual(len(parts), 4, "协议段数异常")
        self.assertEqual(parts[0], "NEED_CONFIRM")
        self.assertIn("写入文件", parts[2], "确认描述缺失")
        # 生成端对确认理由做了半角冒号→全角归一（防盘符截断）；还原后应含完整路径
        normalized_desc = parts[2].replace("：", ":")
        # 执行器构造时对 workspace 做过 resolve()（executor.py:38），比较前同样解析
        self.assertIn(str(target.resolve()), normalized_desc, "确认描述被盘符冒号截断（描述应含完整路径）")


if __name__ == "__main__":
    unittest.main()
