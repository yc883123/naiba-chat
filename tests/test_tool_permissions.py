# -*- coding: utf-8 -*-
"""护栏：权限矩阵（_confirmation_reason 行为规格）——自动化覆盖回归清单 §E 项。

覆盖：full 全放行 / confirm 默认 / auto 快捷 / deny 硬拒绝；
只读工具（工作区内免确认、越界必确认）；写工具（auto+工作区内免确认）；
MCP 工具 annotations（readOnlyHint 免确认、destructiveHint 在 auto 下仍确认）。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.mcp import MCPRegistry  # noqa: E402
from naiba.tools.executor import ToolExecutor  # noqa: E402


class PermissionMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "b.txt").write_text("x", encoding="utf-8")
        self.outside = Path(self.tmp.name) / ".." / "outside.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def _executor(self, mode="confirm"):
        return ToolExecutor(self.root, sys.executable, 60, MCPRegistry([]), permission_mode=mode)

    def test_full_mode_never_asks(self):
        ok, out = self._executor("full").execute("pwsh", {"command": "dir"}, [])
        # full 模式下 pwsh 也直接执行（返回执行结果而非 NEED_CONFIRM）。
        self.assertTrue(ok, out)
        self.assertNotIn("NEED_CONFIRM", out)

    def test_read_inside_workspace_no_confirm(self):
        ok, out = self._executor("confirm").execute("read_file", {"path": str(self.root / "b.txt")}, [])
        self.assertTrue(ok)
        self.assertIn("x", out)

    def test_read_outside_workspace_requires_confirm(self):
        ok, out = self._executor("confirm").execute("read_file", {"path": str(self.outside.resolve())}, [])
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)
        self.assertIn("工作区外", out)

    def test_write_auto_inside_workspace_no_confirm(self):
        ok, out = self._executor("auto").execute("write_file", {"path": str(self.root / "a.txt"), "content": "y"}, [])
        self.assertTrue(ok, out)
        self.assertTrue((self.root / "a.txt").exists())

    def test_write_confirm_even_inside_workspace(self):
        ok, out = self._executor("confirm").execute("write_file", {"path": str(self.root / "c.txt"), "content": "y"}, [])
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)

    def test_deny_mode_rejects_high_risk(self):
        ok, out = self._executor("deny").execute("write_file", {"path": str(self.root / "d.txt"), "content": "y"}, [])
        self.assertFalse(ok)
        self.assertIn("权限被拒绝", out)
        self.assertNotIn("NEED_CONFIRM", out)

    def test_mcp_readonly_hint_no_confirm(self):
        executor = self._executor("confirm")
        executor._mcp_tool_annotations = lambda tool: {"readOnlyHint": True}  # type: ignore[assignment]
        ok, out = executor.execute("mcp__srv__safe_tool", {}, [])
        # 无真实 MCP 服务时到达"执行"分支并返回失败——但绝不是 NEED_CONFIRM。
        self.assertNotIn("NEED_CONFIRM", out)
        self.assertTrue(ok is False and out, out)

    def test_mcp_destructive_hint_auto_still_asks(self):
        executor = self._executor("auto")
        executor._mcp_tool_annotations = lambda tool: {"destructiveHint": True}  # type: ignore[assignment]
        ok, out = executor.execute("mcp__srv__risky_tool", {}, [])
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)


if __name__ == "__main__":
    unittest.main()
