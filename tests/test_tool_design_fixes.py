# -*- coding: utf-8 -*-
"""护栏：工具设计修正（read_file 双预算/失败语义/确定性枚举/搜索语义/游标闭环）。

保护对象：
- read_file 按行双预算（50 行 × 30000 字符）截断、截断标记（含行区间与续读起点）、
  with_line_numbers / end_line；
- pwsh / run_skill_script 非零退出码、http_request >=400 → success=False（原恒 True）；
- glob_files / list_directory 按名称排序、绝对路径、start_after 续枚举与超限提示；
- search_files ignore_case 统一生效、超大文件跳过计数、命中总数汇总；
- job_output 增量游标闭环（推进 cursor 回传）。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.mcp import MCPRegistry  # noqa: E402
from naiba.tools.providers import core as core_provider  # noqa: E402
from naiba.tools.providers.core import _result_success  # noqa: E402


def _ctx(workspace: Path):
    return core_provider.ToolContext(
        workspace=workspace,
        python_executable=sys.executable,
        command_timeout=60,
        mcp_registry=MCPRegistry([]),
        mcp_register=None,
    )


class ReadFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba-readfile-"))
        self.ctx = _ctx(self.tmp)

    def _make(self, lines: list[str]) -> Path:
        target = self.tmp / "sample.txt"
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return target

    def test_line_budget_truncates_at_50_and_marks_resume(self) -> None:
        target = self._make([f"line-{i:03d}" for i in range(1, 61)])
        out = core_provider._tool_read_file(self.ctx, {"path": str(target)}, None)
        self.assertIn("line-001", out)
        self.assertIn("line-050", out)
        self.assertNotIn("line-051", out)
        self.assertIn("已返回第 1-50 行", out)
        self.assertIn("start_line=51", out)

    def test_resume_from_marker_returns_remaining(self) -> None:
        target = self._make([f"line-{i:03d}" for i in range(1, 61)])
        first = core_provider._tool_read_file(self.ctx, {"path": str(target)}, None)
        resume = int(first.rsplit("start_line=", 1)[1].split()[0].rstrip("）"))
        out = core_provider._tool_read_file(self.ctx, {"path": str(target), "start_line": resume}, None)
        self.assertIn("line-051", out)
        self.assertIn("line-060", out)
        self.assertNotIn("截图", out)  # 无截断标记（已到文件尾）

    def test_char_budget_truncates_midline_and_resume_at_cut_line(self) -> None:
        # 单行远超 30000 字符：按字符截断该行，resume 指向该行本身。
        target = self.tmp / "big.txt"
        target.write_text(("X" * 40000) + "\n", encoding="utf-8")
        out = core_provider._tool_read_file(self.ctx, {"path": str(target)}, None)
        self.assertEqual(out.count("X"), 30000)
        self.assertIn("第 1 行按字符预算截断", out)
        self.assertIn("start_line=1", out)

    def test_end_line_and_with_line_numbers(self) -> None:
        target = self._make([f"row-{i:02d}" for i in range(1, 20)])
        out = core_provider._tool_read_file(
            self.ctx, {"path": str(target), "start_line": 5, "end_line": 7, "with_line_numbers": True}, None
        )
        self.assertIn("5: row-05", out)
        self.assertIn("7: row-07", out)
        self.assertNotIn("row-08", out)
        self.assertNotIn("已达读取上限", out)

    def test_start_line_beyond_end_returns_explicit_note(self) -> None:
        target = self._make(["a", "b"])
        out = core_provider._tool_read_file(self.ctx, {"path": str(target), "start_line": 9}, None)
        self.assertIn("文件共 2 行", out)

    def test_max_chars_hidden_from_schema_and_capped(self) -> None:
        """max_chars 不得对模型暴露；执行层兼容旧调用但硬上限 30000 字符（防浪费）。"""
        from naiba.tools.registry import build_core_tool_specs

        spec = next(item for item in build_core_tool_specs() if item.name == "read_file")
        self.assertNotIn("max_chars", (spec.parameters or {}).get("properties", {}))
        target = self.tmp / "huge.txt"
        target.write_text(("X" * 40000) + "\n", encoding="utf-8")
        out = core_provider._tool_read_file(
            self.ctx, {"path": str(target), "max_chars": 100000}, None
        )
        self.assertEqual(out.count("X"), 30000, "执行层必须把 max_chars 封顶在 30000")

    def test_empty_file(self) -> None:
        target = self.tmp / "empty.txt"
        target.write_text("", encoding="utf-8")
        out = core_provider._tool_read_file(self.ctx, {"path": str(target)}, None)
        self.assertIn("文件为空", out)


class FailureSemanticsTests(unittest.TestCase):
    def test_exit_code_tools_fail_on_nonzero(self) -> None:
        self.assertTrue(_result_success("pwsh", "exit_code=0\nok"))
        self.assertFalse(_result_success("pwsh", "exit_code=1\nboom"))
        self.assertFalse(_result_success("run_skill_script", "exit_code=-1073741510\n"))
        self.assertTrue(_result_success("read_file", "内容") and _result_success("edit_file", "已修改"))

    def test_http_status_ge_400_fails(self) -> None:
        self.assertTrue(_result_success("http_request", "HTTP 200\nok"))
        self.assertFalse(_result_success("http_request", "HTTP 404\nnot found"))
        self.assertFalse(_result_success("http_request", "HTTP 500\nboom"))


class DeterministicEnumerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba-enum-"))
        self.ctx = _ctx(self.tmp)
        for name in ("b.txt", "a.txt", "c.txt", "d.txt"):
            (self.tmp / name).write_text(name, encoding="utf-8")

    def test_glob_sorted_and_resume_marker(self) -> None:
        out = core_provider._tool_glob_files(self.ctx, {"path": str(self.tmp), "pattern": "*.txt", "limit": 2}, None)
        lines = out.splitlines()
        self.assertIn("a.txt", lines[0])
        self.assertIn("b.txt", lines[1])
        self.assertIn("共 4 项，已列出前 2 项", out)
        self.assertIn("start_after=", out)
        last = lines[1]
        resumed = core_provider._tool_glob_files(
            self.ctx, {"path": str(self.tmp), "pattern": "*.txt", "limit": 2, "start_after": last}, None
        )
        self.assertIn("c.txt", resumed)
        self.assertIn("d.txt", resumed)

    def test_list_directory_absolute_sorted(self) -> None:
        out = core_provider._tool_list_directory(self.ctx, {"path": str(self.tmp)}, None)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("FILE "))
        self.assertIn(str(self.tmp) + "\\a.txt", lines[0])
        self.assertIn("\\d.txt", lines[-1])


class SearchSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba-search-"))
        self.ctx = _ctx(self.tmp)
        self.target = self.tmp / "code.txt"
        self.target.write_text("foo\nFOO\nbar\n", encoding="utf-8")

    def test_substring_default_case_sensitive(self) -> None:
        out = core_provider._tool_search_files(self.ctx, {
            "path": str(self.tmp), "query": "foo", "pattern": "*.txt",
        }, None)
        self.assertIn("共 1 处命中，已显示前 1 组", out)

    def test_ignore_case_applies_to_substring(self) -> None:
        out = core_provider._tool_search_files(self.ctx, {
            "path": str(self.tmp), "query": "foo", "pattern": "*.txt", "ignore_case": True,
        }, None)
        self.assertIn("共 2 处命中", out)

    def test_large_files_skipped_are_reported(self) -> None:
        big = self.tmp / "big.bin"
        big.write_bytes(b"x" * 2048)
        out = core_provider._tool_search_files(self.ctx, {
            "path": str(self.tmp), "query": "x", "max_file_size": 1024,
        }, None)
        self.assertIn("已跳过 1 个超过 1024 字节的文件", out)


class JobOutputCursorTests(unittest.TestCase):
    def test_cursor_marker_roundtrip(self) -> None:
        from naiba.subagent import job_tool_handler_factory

        fake_jobs = SimpleNamespace(
            get=lambda job_id: {"status": "running"},
            read=lambda job_id, cursor=0: {"events": [{"line": "hello"}], "cursor": 7},
        )
        app = SimpleNamespace(jobs=fake_jobs)
        handler = job_tool_handler_factory(app)["job_output"]
        ok, out = handler({"job_id": "j1"}, [], None)
        self.assertTrue(ok)
        self.assertIn("hello", out)
        self.assertIn("cursor=7", out)


if __name__ == "__main__":
    unittest.main()
