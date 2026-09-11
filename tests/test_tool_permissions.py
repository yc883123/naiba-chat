# -*- coding: utf-8 -*-
"""护栏：权限矩阵（_confirmation_reason 行为规格）——自动化覆盖回归清单 §E 项。

覆盖：full 全放行 / confirm 默认 / auto 快捷 / deny 硬拒绝；
只读工具（工作区内免确认、越界必确认）；写工具（auto+工作区内免确认）；
MCP 工具 annotations（readOnlyHint 免确认、destructiveHint 在 auto 下仍确认）；
**Run 工作区同源**（装配期工作区 ≠ run_context.workspace_dir 时，判定与执行都按 Run 工作区，
越界仍确认且确认理由带「当前工作区」诊断）。

引擎接线与生产等价（def 解析器 + 别名解析器，见 tool_testkit）。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.tools.registry import ToolRegistry  # noqa: E402

from tool_testkit import assembled_registry, run_context_for, wired_executor  # noqa: E402


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


class AutoRunWorkspaceSourceTests(unittest.TestCase):
    """回归：auto 模式下"判定与执行都必须按当前 Run 工作区"。

    装配期工作区（引擎启动默认）与会话工作区不同源时：
    - 界内相对/绝对路径免确认（修"工作区内被判越界 → 反复要允许"）；
    - 相对路径按 Run 工作区解析（修"仍用启动时旧工作区"）；
    - 工作区外仍必须确认，且确认理由带当前工作区（诊断）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.app_ws = base / "app-workspace"
        self.run_ws = base / "run-workspace"
        self.outside = base / "elsewhere"
        for folder in (self.app_ws, self.run_ws, self.outside):
            folder.mkdir()
        (self.app_ws / "target.txt").write_text("CONTENT-APP", encoding="utf-8")
        (self.run_ws / "target.txt").write_text("CONTENT-RUN", encoding="utf-8")
        (self.run_ws / "sub").mkdir()
        (self.run_ws / "sub" / "inner.txt").write_text("INNER-RUN", encoding="utf-8")
        (self.outside / "secret.txt").write_text("SECRET-OUT", encoding="utf-8")
        self.run_ctx = run_context_for(self.run_ws)

    def tearDown(self):
        self.tmp.cleanup()

    def _executor(self, mode="auto"):
        # 引擎持有装配期工作区（另一目录）：判定与执行都必须以 run_context.workspace_dir 为准
        return wired_executor(self.app_ws, mode=mode)

    def test_read_file_relative_resolves_against_run_workspace(self):
        ok, out = self._executor().execute("read_file", {"path": "target.txt"}, [], self.run_ctx)
        self.assertFalse(out.startswith("NEED_CONFIRM:"), out)
        self.assertTrue(ok, out)
        self.assertIn("CONTENT-RUN", out)
        self.assertNotIn("CONTENT-APP", out)

    def test_read_file_absolute_inside_run_workspace_no_confirm(self):
        target = self.run_ws / "sub" / "inner.txt"
        ok, out = self._executor().execute("read_file", {"path": str(target)}, [], self.run_ctx)
        self.assertFalse(out.startswith("NEED_CONFIRM:"), out)
        self.assertTrue(ok, out)
        self.assertIn("INNER-RUN", out)

    @unittest.skipUnless(os.name == "nt", "Windows 路径大小写语义")
    def test_read_file_case_variant_inside_workspace_no_confirm(self):
        variant = str(self.run_ws / "target.txt").upper()
        ok, out = self._executor().execute("read_file", {"path": variant}, [], self.run_ctx)
        self.assertFalse(out.startswith("NEED_CONFIRM:"), out)
        self.assertTrue(ok, out)
        self.assertIn("CONTENT-RUN", out)

    def test_list_directory_defaults_to_run_workspace(self):
        ok, out = self._executor().execute(
            "list_directory", {"path": "", "files_only": True}, [], self.run_ctx
        )
        self.assertFalse(out.startswith("NEED_CONFIRM:"), out)
        self.assertTrue(ok, out)
        self.assertIn("target.txt", out)
        self.assertNotIn(str(self.app_ws), out)

    def test_search_files_relative_no_confirm(self):
        ok, out = self._executor().execute(
            "search_files", {"path": "", "query": "CONTENT-RUN"}, [], self.run_ctx
        )
        self.assertFalse(out.startswith("NEED_CONFIRM:"), out)
        self.assertTrue(ok, out)
        self.assertIn("run-workspace", out)

    def test_write_relative_resolves_against_run_workspace(self):
        ok, out = self._executor().execute(
            "write_file", {"path": "new.txt", "content": "n"}, [], self.run_ctx
        )
        self.assertFalse(out.startswith("NEED_CONFIRM:"), out)
        self.assertTrue(ok, out)
        self.assertTrue((self.run_ws / "new.txt").exists(), out)
        self.assertFalse((self.app_ws / "new.txt").exists())

    def test_outside_path_still_requires_confirm_with_workspace_hint(self):
        target = self.outside / "secret.txt"
        ok, out = self._executor().execute("read_file", {"path": str(target)}, [], self.run_ctx)
        self.assertFalse(ok)
        self.assertTrue(out.startswith("NEED_CONFIRM:"), out)
        self.assertIn("工作区外", out)
        # 诊断：确认理由必须带"当前工作区"，用户能直接看出判定根
        self.assertIn("当前工作区", out)


class DocumentsRunWorkspaceSourceTests(unittest.TestCase):
    """回归：read_pdf 的策略与执行同源（原实现忽略 run_context，执行按装配期工作区）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.app_ws = base / "app-workspace"
        self.run_ws = base / "run-workspace"
        self.app_ws.mkdir()
        self.run_ws.mkdir()
        (self.run_ws / "doc.pdf").write_text("pdf", encoding="utf-8")
        self.run_ctx = run_context_for(self.run_ws)

    def tearDown(self):
        self.tmp.cleanup()

    def _provider(self):
        from naiba.mcp import MCPRegistry
        from naiba.tools.providers.core import ToolContext
        from naiba.tools.providers.documents import DocumentToolProvider

        context = ToolContext(
            workspace=self.app_ws,
            python_executable=sys.executable,
            command_timeout=60,
            mcp_registry=MCPRegistry([]),
            mcp_register=None,
        )
        return DocumentToolProvider(context, lambda: self.app_ws / ".cache")

    def _read_pdf_spec(self):
        return next(spec for spec in self._provider().tools() if spec.name == "read_pdf")

    def test_policy_no_confirm_inside_run_workspace(self):
        reason = self._read_pdf_spec().policy(
            "read_pdf", {"path": "doc.pdf"}, [], "auto", self.run_ctx, self.app_ws
        )
        self.assertEqual("", reason)

    def test_execute_resolves_relative_against_run_workspace(self):
        import naiba.tools.providers.documents as documents

        seen: list[str] = []
        original = documents._IMPLS["read_pdf"]
        documents._IMPLS["read_pdf"] = (
            lambda ctx, args, skills, data_dir: (seen.append(str(ctx.workspace)), "ok")[1]
        )
        try:
            ok, out = self._read_pdf_spec().execute({"path": "doc.pdf"}, [], self.run_ctx)
        finally:
            documents._IMPLS["read_pdf"] = original
        self.assertTrue(ok, out)
        self.assertEqual([str(self.run_ws)], seen)


if __name__ == "__main__":
    unittest.main()
