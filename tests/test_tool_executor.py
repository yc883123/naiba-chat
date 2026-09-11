# -*- coding: utf-8 -*-
"""护栏：ToolExecutor 确认协议与工具执行主路径（防归位时导入遗漏类回归）。

背景：executor 从 skill_runtime 归位到 naiba/tools/executor.py 时手写导入清单曾漏掉
uuid（confirm_id 生成）与其它符号，导致真实调用路径 NameError。本测试直接走
NEED_CONFIRM 协议路径——确认流必经 uuid——且不真正写盘。
"""

import sys
import tempfile
import threading
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


class ConfirmRunContextTests(unittest.TestCase):
    """回归：NEED_CONFIRM 暂存完整 run_context，批准执行时原样复用。

    旧实现 pending 只存 tool/arguments/active_skills，批准后 run_context 变成 None——
    依赖它的行为（产物目录、job 归属、reset_context、work dir）在"允许执行"后全部丢失。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.seen = []

    def tearDown(self):
        self.tmp.cleanup()

    def _executor(self):
        from naiba.mcp import MCPRegistry
        from naiba.tools.executor import ToolExecutor
        from naiba.tools.registry import ToolSpec

        def _execute(arguments, active_skills, run_context=None):
            self.seen.append(run_context)
            return True, "done"

        spec = ToolSpec(
            name="recorder_tool",
            description="测试桩：记录执行时收到的 run_context",
            parameters={"type": "object", "properties": {}},
            side_effect=True,
            policy=lambda tool, args, skills, mode, run_context, workspace=None: "测试确认",
            execute=_execute,
        )
        executor = ToolExecutor(
            self.root, sys.executable, 60, MCPRegistry([]), permission_mode="confirm"
        )
        executor.set_def_resolver(lambda name: spec if name == "recorder_tool" else None)
        executor.set_alias_resolver(lambda name: name)
        return executor

    def _run_context(self):
        return {"run_id": "R1", "workspace_dir": str(self.root), "cancel_event": threading.Event()}

    def test_confirm_execute_reuses_run_context(self):
        executor = self._executor()
        context = self._run_context()
        ok, out = executor.execute("recorder_tool", {}, [], context)
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)
        confirm_id = out.split(":", 3)[1]
        ok2, out2 = executor.confirm_execute(confirm_id)
        self.assertTrue(ok2, out2)
        self.assertEqual([context], self.seen)

    def test_async_confirm_reuses_run_context(self):
        executor = self._executor()
        context = self._run_context()
        ok, out = executor.execute("recorder_tool", {}, [], context)
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)
        confirm_id = out.split(":", 3)[1]
        executor.confirm_execute_async(confirm_id)
        ok2, out2 = executor.wait_for_confirmation(confirm_id, timeout=5)
        self.assertTrue(ok2, out2)
        self.assertEqual([context], self.seen)


if __name__ == "__main__":
    unittest.main()
