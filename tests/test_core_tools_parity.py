"""对拍：core 域 Provider 新实现 vs ToolExecutor 旧实现（Phase 3 双轨）。

原则：确定性工具（read/write/list/glob/search/edit + call_mcp legacy 只读转发）逐输出对拍；
pwsh/run_skill_script/http_request/register_mcp 涉及子进程/网络/注册副作用，不在此处对拍，
由 golden 与五点冒烟兜底。任何行为差异必须报告（旧实现为基准，Phase 5 前不得漂移）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from naiba.mcp import MCPRegistry
from naiba.tools.executor import ToolExecutor
from naiba.tools.providers import core as core_provider
from naiba.tools.registry import ToolRegistry, build_core_tool_specs

ROOT = Path(__file__).resolve().parent.parent


def _make_ctx(workspace: Path) -> core_provider.ToolContext:
    mcp = MCPRegistry([])
    return core_provider.ToolContext(
        workspace=workspace,
        python_executable=sys.executable,
        command_timeout=60,
        mcp_registry=mcp,
        mcp_register=None,
    )


class CoreToolsParityTests(unittest.TestCase):
    """新旧实现对拍 + Provider def 一致性。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba-parity-"))
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.executor = ToolExecutor(self.workspace, sys.executable, 60, MCPRegistry([]))
        self.ctx = _make_ctx(self.workspace)
        self.provider = core_provider.CoreToolProvider(self.ctx)
        self.specs = {spec.name: spec for spec in self.provider.tools()}
        self.old_methods = {
            "read_file": self.executor._tool_read_file,
            "write_file": self.executor._tool_write_file,
            "list_directory": self.executor._tool_list_directory,
            "search_files": self.executor._tool_search_files,
            "glob_files": self.executor._tool_glob_files,
            "edit_file": self.executor._tool_edit_file,
            "call_mcp": self.executor._tool_call_mcp,
        }

    def _run_new(self, name: str, arguments: dict, skills: list | None = None) -> tuple[bool, str]:
        return self.specs[name].execute(arguments, skills or [], None)

    def test_provider_covers_exactly_core_declarations(self) -> None:
        declared = {spec.name for spec in build_core_tool_specs()}
        covered = set(self.specs)
        self.assertEqual(declared, covered, "Provider 未覆盖/多出 core 声明")

    def test_provider_specs_match_declarations(self) -> None:
        """Provider def 与声明表逐字段一致（execute/policy 除外，仅替换 execute）。"""
        by_name = {spec.name: spec for spec in build_core_tool_specs()}
        for name, new_spec in self.specs.items():
            with self.subTest(tool=name):
                old_spec = by_name[name]
                self.assertEqual(new_spec.description, old_spec.description)
                self.assertEqual(new_spec.parameters, old_spec.parameters)
                self.assertEqual(new_spec.side_effect, old_spec.side_effect)
                self.assertEqual(new_spec.retryable, old_spec.retryable)
                self.assertEqual(new_spec.timeout, old_spec.timeout)
                self.assertEqual(new_spec.permission, old_spec.permission)
                self.assertEqual(new_spec.annotations, old_spec.annotations)
                self.assertIs(new_spec.policy, old_spec.policy, f"{name}: policy 被 Provider 改为 None")
                self.assertIsNotNone(new_spec.execute, f"{name}: Provider 未绑定 execute")

    def test_parity_read_file(self) -> None:
        target = self.workspace / "sample.txt"
        target.write_text("line1 hello\nline2 world\nline3 tail\n", encoding="utf-8")
        cases = [
            {"path": str(target), "max_chars": 10},
            {"path": str(target), "start_line": 2, "max_chars": 500},
            {"path": str(target)},
        ]
        for case in cases:
            with self.subTest(case=case):
                old = self.old_methods["read_file"](case, None)
                ok, new = self._run_new("read_file", case)
                self.assertTrue(ok)
                self.assertEqual(old, new, f"read_file 对拍不一致：{case}")

    def test_parity_write_file(self) -> None:
        old_path = self.workspace / "old.txt"
        new_path = self.workspace / "new.txt"
        case = {"path": str(old_path), "content": "内容abc\n第二行"}
        old_out = self.old_methods["write_file"](case)
        new_case = {"path": str(new_path), "content": "内容abc\n第二行"}
        ok, new_out = self._run_new("write_file", new_case)
        self.assertTrue(ok)
        self.assertEqual(old_out.replace(str(old_path), "<PATH>"), new_out.replace(str(new_path), "<PATH>"))
        self.assertEqual(old_path.read_bytes(), new_path.read_bytes(), "写入字节不一致")
        # append 模式
        old_out2 = self.old_methods["write_file"]({"path": str(old_path), "content": "x", "append": True})
        ok2, new_out2 = self._run_new("write_file", {"path": str(new_path), "content": "x", "append": True})
        self.assertEqual(old_path.read_bytes(), new_path.read_bytes(), "append 后字节不一致")

    def test_parity_list_directory_and_glob(self) -> None:
        (self.workspace / "dir_a").mkdir()
        (self.workspace / "dir_a" / "f1.py").write_text("x", encoding="utf-8")
        (self.workspace / "dir_a" / "f2.png").write_bytes(b"png")
        (self.workspace / "root.md").write_text("m", encoding="utf-8")
        cases = [
            {"path": str(self.workspace), "limit": 50},
            {"path": str(self.workspace), "recursive": True, "limit": 50},
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(
                    self.old_methods["list_directory"](case, None),
                    self._run_new("list_directory", case)[1],
                )
        glob_case = {"path": str(self.workspace), "pattern": "**/*.py", "limit": 50}
        self.assertEqual(
            self.old_methods["glob_files"](glob_case, None),
            self._run_new("glob_files", glob_case)[1],
        )

    def test_parity_search_files(self) -> None:
        (self.workspace / "codes.txt").write_text("alpha\nBETA\ngamma beta\n", encoding="utf-8")
        (self.workspace / "other.md").write_text("beta only here\n", encoding="utf-8")
        cases = [
            {"path": str(self.workspace), "query": "beta", "limit": 20},
            {"path": str(self.workspace), "query": "beta", "regex": True, "ignore_case": True, "limit": 20},
            {"path": str(self.workspace), "query": "beta", "context_lines": 1, "limit": 20},
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(
                    self.old_methods["search_files"](case, None),
                    self._run_new("search_files", case)[1],
                    f"search_files 对拍不一致：{case}",
                )

    def test_parity_edit_file(self) -> None:
        old_path = self.workspace / "edit_old.txt"
        new_path = self.workspace / "edit_new.txt"
        old_path.write_text("AAA\nBBB\nAAA\n", encoding="utf-8")
        new_path.write_text("AAA\nBBB\nAAA\n", encoding="utf-8")
        old_out = self.old_methods["edit_file"]({"path": str(old_path), "old_text": "AAA", "new_text": "ZZZ", "all": True})
        ok, new_out = self._run_new("edit_file", {"path": str(new_path), "old_text": "AAA", "new_text": "ZZZ", "all": True})
        self.assertTrue(ok)
        self.assertEqual(old_out.replace(str(old_path), "<PATH>"), new_out.replace(str(new_path), "<PATH>"))
        self.assertEqual(old_path.read_text(encoding="utf-8"), new_path.read_text(encoding="utf-8"))

    def test_parity_call_mcp_legacy_readonly(self) -> None:
        """legacy：call_mcp(naiba-chat, read_file) 转发本机只读工具。"""
        target = self.workspace / "legacy.txt"
        target.write_text("legacy body", encoding="utf-8")
        case = {
            "server": "naiba-chat",
            "tool": "read_file",
            "arguments": {"path": str(target)},
        }
        old_ok, old_out = self.old_methods["call_mcp"](case, None)
        new_ok, new_out = self._run_new("call_mcp", case)
        self.assertTrue(old_ok)
        self.assertTrue(new_ok)
        self.assertEqual(old_out, new_out)

    def test_registry_execute_without_engine_uses_new_impl(self) -> None:
        """无引擎时 registry.execute 走 def.execute（新实现），与旧实现输出一致。"""
        registry = ToolRegistry()
        registry.register_provider(self.provider)
        target = self.workspace / "reg.txt"
        target.write_text("through registry", encoding="utf-8")
        ok, out = registry.execute("read_file", {"path": str(target)}, [])
        self.assertTrue(ok)
        self.assertEqual(out, "through registry")


if __name__ == "__main__":
    unittest.main()
