# -*- coding: utf-8 -*-
"""护栏：权限矩阵（_confirmation_reason 行为规格）——自动化覆盖回归清单 §E 项。

覆盖：full 全放行 / confirm 默认 / auto 快捷 / deny 硬拒绝；
只读工具（工作区内免确认、越界必确认）；写工具（auto+工作区内免确认）；
MCP 工具 annotations（readOnlyHint 免确认、destructiveHint 在 auto 下仍确认）。

引擎接线与生产等价（def 解析器 + 别名解析器，见 tool_testkit）。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.tools.registry import ToolRegistry  # noqa: E402

from tool_testkit import assembled_registry, wired_executor  # noqa: E402


class _FakeMCP:
    """无连接 MCP：connection 恒 None，call 从不被真实调用。"""

    def connection(self, server_id):
        return None

    def call(self, server_id, tool, arguments):
        return False, "not connected"


class PermissionMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "b.txt").write_text("x", encoding="utf-8")
        self.outside = Path(self.tmp.name) / ".." / "outside.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def _executor(self, mode="confirm"):
        return wired_executor(self.root, mode=mode)

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
        mcp = _FakeMCP()
        registry = assembled_registry(self.root, mcp)
        registry.register_mcp_tools("srv", [{"name": "safe_tool", "annotations": {"readOnlyHint": True}}])
        executor = wired_executor(self.root, mode="confirm", mcp_registry=mcp)
        executor.set_def_resolver(registry.get)
        executor.set_alias_resolver(registry.resolve)
        ok, out = executor.execute("mcp__srv__safe_tool", {}, [])
        # 无真实 MCP 连接时到达"执行"分支并返回失败——但绝不是 NEED_CONFIRM。
        self.assertNotIn("NEED_CONFIRM", out)
        self.assertTrue(ok is False and out, out)

    def test_mcp_destructive_hint_auto_still_asks(self):
        mcp = _FakeMCP()
        registry = assembled_registry(self.root, mcp)
        registry.register_mcp_tools("srv", [{"name": "risky_tool", "annotations": {"destructiveHint": True}}])
        executor = wired_executor(self.root, mode="auto", mcp_registry=mcp)
        executor.set_def_resolver(registry.get)
        executor.set_alias_resolver(registry.resolve)
        ok, out = executor.execute("mcp__srv__risky_tool", {}, [])
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)

    def test_mcp_readonly_hint_auto_no_confirm(self):
        mcp = _FakeMCP()
        registry = assembled_registry(self.root, mcp)
        registry.register_mcp_tools("srv", [{"name": "auto_tool", "annotations": {}}])
        executor = wired_executor(self.root, mode="auto", mcp_registry=mcp)
        executor.set_def_resolver(registry.get)
        executor.set_alias_resolver(registry.resolve)
        ok, out = executor.execute("mcp__srv__auto_tool", {}, [])
        # auto 模式、无 destructive 标注：免确认，走到执行分支（连接缺失返回失败而非 NEED_CONFIRM）。
        self.assertNotIn("NEED_CONFIRM", out)
        self.assertTrue(ok is False and out, out)


if __name__ == "__main__":
    unittest.main()
