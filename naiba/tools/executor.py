"""工具执行器（原 skill_runtime.py 的 ToolExecutor 归位：工具声明/执行/权限确认同层）。

权限确认状态机（confirm/auto/full/deny + NEED_CONFIRM 协议）、文件/脚本/HTTP 工具实现、
MCP 网关调用（call_mcp/register_mcp）与本地只读转发。专属辅助：POWERSHELL_UTF8_PREFIX、
_powershell_literal（原 skill_runtime 模块级，仅执行器使用）。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from naiba import net as net_io
from naiba.core.exceptions import TaskCancelled
from naiba.mcp import MCPRegistry


POWERSHELL_UTF8_PREFIX = (
    "$utf8 = [System.Text.UTF8Encoding]::new($false); "
    "[Console]::InputEncoding = $utf8; [Console]::OutputEncoding = $utf8; $OutputEncoding = $utf8;"
)


def _powershell_literal(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class ToolExecutor:
    VALID_PERMISSION_MODES = {"confirm", "auto", "full", "deny"}
    DANGEROUS_TOOLS = {
        "pwsh": "执行 PowerShell 命令",
        "edit_file": "精确修改文件",
        "write_file": "写入文件",
        "run_skill_script": "运行技能脚本",
        "http_request": "发送HTTP请求",
        "call_mcp": "调用MCP工具",
        "register_mcp": "注册MCP服务",
    }
    TOOL_ALIASES = {"read":"read_file", "write":"write_file", "edit":"edit_file", "glob":"glob_files", "grep":"search_files"}
    def __init__(
        self,
        workspace: Path,
        python_executable: str,
        command_timeout: int,
        mcp_registry: MCPRegistry,
        permission_mode: str = "confirm",
        mcp_register: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ):
        self.workspace = workspace.resolve()
        self.python_executable = python_executable
        self.command_timeout = command_timeout
        self.mcp_registry = mcp_registry
        self.mcp_register = mcp_register
        self.permission_mode = "confirm"
        self.set_permission_mode(permission_mode)
        self.pending_confirmation: dict[str, dict[str, Any]] = {}
        self.confirmation_results: dict[str, tuple[bool, str]] = {}
        self._confirmation_lock = threading.RLock()

    def set_permission_mode(self, mode: str) -> None:
        normalized = str(mode or "confirm").strip().lower()
        self.permission_mode = normalized if normalized in self.VALID_PERMISSION_MODES else "confirm"

    def clone_for_permission(self, mode: str) -> "ToolExecutor":
        """Create an isolated executor for one Run while sharing external services."""
        return ToolExecutor(
            self.workspace,
            self.python_executable,
            self.command_timeout,
            self.mcp_registry,
            permission_mode=mode,
            mcp_register=self.mcp_register,
        )

    @staticmethod
    def _path_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _resolve_tool_path(self, raw: Any, default_workspace: bool = False) -> Path:
        value = str(raw or "").strip()
        if not value and default_workspace:
            return self.workspace
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        return path.resolve()

    def _resolve_read_path(
        self, raw: Any, active_skills: list[dict[str, Any]] | None = None,
        default_workspace: bool = False,
    ) -> Path:
        """Resolve relative paths against an active Skill before workspace."""
        value = str(raw or "").strip()
        if not value and default_workspace:
            return self.workspace
        path = Path(value).expanduser()
        if path.is_absolute():
            return path.resolve()
        workspace_candidate = (self.workspace / path).resolve()
        if workspace_candidate.exists():
            return workspace_candidate
        matches: list[Path] = []
        for root in self._read_roots(active_skills or [])[1:]:
            candidate = (root / path).resolve()
            if self._path_within(candidate, root) and candidate.exists():
                matches.append(candidate)
        unique = list(dict.fromkeys(matches))
        if len(unique) == 1:
            return unique[0]
        if len(unique) > 1:
            choices = "、".join(str(item) for item in unique[:4])
            raise ValueError(f"相对路径在多个 active Skill 中存在，请改用绝对路径：{choices}")
        return workspace_candidate

    def _read_roots(self, active_skills: list[dict[str, Any]]) -> list[Path]:
        roots = [self.workspace]
        for skill in active_skills:
            value = str(skill.get("root") or "").strip()
            if not value:
                continue
            root = Path(value).expanduser().resolve()
            if root not in roots:
                roots.append(root)
        return roots

    def _mcp_tool_annotations(self, tool: str) -> dict[str, Any]:
        server_id = ""
        tool_name = ""
        if tool.startswith("mcp__"):
            parts = tool.split("__", 2)
            if len(parts) == 3:
                server_id, tool_name = parts[1], parts[2]
        elif "." in tool:
            server_id, tool_name = tool.split(".", 1)
        if not server_id or not tool_name:
            return {}
        connection = getattr(self.mcp_registry, "connections", {}).get(server_id)
        for item in getattr(connection, "tools", []) if connection is not None else []:
            if str(item.get("name") or "") == tool_name:
                return dict(item.get("annotations") or {})
        return {}

    def _confirmation_reason(
        self,
        tool: str,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
    ) -> str:
        tool = self.TOOL_ALIASES.get(tool, tool)
        if self.permission_mode == "full":
            return ""
        # Legacy Skills may wrap local read-only tools in call_mcp. Apply the
        # same path resolution and boundary check as direct read tools.
        if tool == "call_mcp" and isinstance(arguments, dict):
            server = str(arguments.get("server") or "").strip()
            nested_tool = str(arguments.get("tool") or "").strip()
            nested_args = arguments.get("arguments") or {}
            if (
                server in {"naiba-chat", "comfyui"}
                and nested_tool in {"read_file", "list_directory", "search_files"}
                and isinstance(nested_args, dict)
            ):
                path = self._resolve_read_path(
                    nested_args.get("path"), active_skills, nested_tool != "read_file"
                )
                if not any(self._path_within(path, root) for root in self._read_roots(active_skills)):
                    return f"读取工作区外路径：{path}"
                return ""
        if tool in {"read_file", "list_directory", "search_files", "glob_files"}:
            # Read-only inspection is non-destructive; do not interrupt a
            # Skill workflow with one confirmation per file or chunk.
            path = self._resolve_read_path(arguments.get("path"), active_skills, tool != "read_file")
            if not any(self._path_within(path, root) for root in self._read_roots(active_skills)):
                return "读取工作区外路径：" + str(path)
            return ""
        if tool in {"write_file", "edit_file"}:
            path = self._resolve_tool_path(arguments.get("path"))
            if self.permission_mode == "auto" and self._path_within(path, self.workspace):
                return ""
            return f"写入文件：{path}"
        if tool in self.DANGEROUS_TOOLS:
            # Auto mode is the Harness-like execution mode: commands and
            # ordinary external actions run without one confirmation per
            # step. Confirm mode still asks before these operations.
            if self.permission_mode == "auto":
                return ""
            return self.DANGEROUS_TOOLS[tool]
        if tool.startswith("mcp__"):
            annotations = self._mcp_tool_annotations(tool)
            if bool(annotations.get("readOnlyHint")):
                return ""
            if self.permission_mode == "auto" and not bool(annotations.get("destructiveHint")):
                return ""
            return f"调用MCP工具：{tool}"
        if "." in tool:
            annotations = self._mcp_tool_annotations(tool)
            if bool(annotations.get("readOnlyHint")):
                return ""
            if self.permission_mode == "auto" and not bool(annotations.get("destructiveHint")):
                return ""
            return f"调用MCP工具：{tool}"
        return ""

    def execute(self, tool: str, arguments: dict[str, Any], active_skills: list[dict[str, Any]]) -> tuple[bool, str]:
        try:
            reason = self._confirmation_reason(tool, arguments, active_skills)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if reason:
            if self.permission_mode == "deny":
                return False, f"权限被拒绝：{reason}（工具：{tool}）"
            confirm_id = str(uuid.uuid4())
            with self._confirmation_lock:
                self.pending_confirmation[confirm_id] = {
                    "tool": tool,
                    "arguments": arguments,
                    "active_skills": active_skills,
                    "processing": False,
                }
            return False, (
                f"NEED_CONFIRM:{confirm_id}:{reason}:"
                f"{json.dumps(arguments, ensure_ascii=False)[:500]}"
            )
        return self._execute_unchecked(tool, arguments, active_skills)

    def _execute_unchecked(
        self,
        tool: str,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
    ) -> tuple[bool, str]:
        try:
            tool = self.TOOL_ALIASES.get(tool, tool)
            handler = getattr(self, f"_tool_{tool}", None)
            if not handler:
                if tool.startswith("mcp__"):
                    parts = tool.split("__", 2)
                    if len(parts) == 3 and self.mcp_registry.connection(parts[1]) is not None:
                        return self.mcp_registry.call(parts[1], parts[2], arguments)
                if "." in tool:
                    server_id, mcp_tool = tool.split(".", 1)
                    if self.mcp_registry.connection(server_id) is not None:
                        return self.mcp_registry.call(server_id, mcp_tool, arguments)
                return False, f"未知工具：{tool}"
            if tool == "run_skill_script":
                return True, handler(arguments, active_skills)
            if tool in {"read_file", "list_directory", "search_files", "glob_files"}:
                return True, handler(arguments, active_skills)
            if tool == "call_mcp":
                return handler(arguments, active_skills)
            return True, handler(arguments)
        except subprocess.TimeoutExpired:
            return False, f"执行超时（{arguments.get('timeout', self.command_timeout)} 秒）"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def confirm_execute(self, confirm_id: str) -> tuple[bool, str]:
        """确认并执行待确认的工具调用"""
        with self._confirmation_lock:
            pending = self.pending_confirmation.get(confirm_id)
            if not pending:
                return False, "确认ID无效或已过期"
            if pending.get("processing"):
                return False, "该操作正在执行"
            pending["processing"] = True
        result = self._execute_unchecked(
            pending["tool"], pending["arguments"], pending["active_skills"]
        )
        with self._confirmation_lock:
            self.pending_confirmation.pop(confirm_id, None)
            self.confirmation_results[confirm_id] = result
        return result

    def confirm_execute_async(self, confirm_id: str) -> tuple[bool, str]:
        """Approve immediately and execute the potentially long tool off-request."""
        with self._confirmation_lock:
            pending = self.pending_confirmation.get(confirm_id)
            if not pending:
                return False, "确认ID无效或已过期"
            if pending.get("processing"):
                return True, "工具已在执行"
            pending["processing"] = True

        def worker() -> None:
            result = self._execute_unchecked(
                pending["tool"], pending["arguments"], pending["active_skills"]
            )
            with self._confirmation_lock:
                self.pending_confirmation.pop(confirm_id, None)
                self.confirmation_results[confirm_id] = result

        threading.Thread(
            target=worker,
            name=f"tool-confirm-{confirm_id[:8]}",
            daemon=True,
        ).start()
        return True, "已确认，工具正在后台执行"

    def reject_execute(self, confirm_id: str) -> tuple[bool, str]:
        """拒绝待确认的工具调用"""
        with self._confirmation_lock:
            pending = self.pending_confirmation.get(confirm_id)
            if not pending:
                return False, "确认ID无效或已过期"
            if pending.get("processing"):
                return False, "操作已经开始，无法拒绝"
            self.pending_confirmation.pop(confirm_id, None)
            result = (False, f"用户拒绝执行：{pending['tool']}")
            self.confirmation_results[confirm_id] = result
        return result

    def wait_for_confirmation(
        self,
        confirm_id: str,
        timeout: float = 300,
        cancel_event: threading.Event | None = None,
    ) -> tuple[bool, str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                with self._confirmation_lock:
                    pending = self.pending_confirmation.get(confirm_id)
                    if pending and not pending.get("processing"):
                        self.pending_confirmation.pop(confirm_id, None)
                    self.confirmation_results.pop(confirm_id, None)
                raise TaskCancelled("任务已取消")
            with self._confirmation_lock:
                result = self.confirmation_results.pop(confirm_id, None)
                pending = confirm_id in self.pending_confirmation
            if result is not None:
                return result
            if not pending:
                return False, "确认请求已失效"
            time.sleep(0.1)
        with self._confirmation_lock:
            pending = self.pending_confirmation.get(confirm_id)
            if pending and not pending.get("processing"):
                self.pending_confirmation.pop(confirm_id, None)
        return False, "用户未在5分钟内确认，已自动拒绝"

    def _tool_read_file(self, args: dict[str, Any], active_skills: list[dict[str, Any]] | None = None) -> str:
        path = self._resolve_read_path(args.get("path"), active_skills)
        max_chars = min(max(int(args.get("max_chars", 30000)), 100), 100000)
        content = path.read_text(encoding="utf-8", errors="replace")
        # start_line（1 起始）用于跳过文件前部，读取大文件时可从指定行开始，
        # 避免一次性读入过多内容；缺省或非法时从头读取。
        if args.get("start_line") is not None:
            try:
                skip = max(0, int(args.get("start_line")) - 1)
            except (TypeError, ValueError):
                skip = 0
            if skip:
                content = "".join(content.splitlines(keepends=True)[skip:])
        return content[:max_chars]

    def _tool_write_file(self, args: dict[str, Any]) -> str:
        path = self._resolve_tool_path(args.get("path"))
        content = str(args.get("content") or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if args.get("append") else "w"
        with path.open(mode, encoding="utf-8", newline="") as handle:
            handle.write(content)
        return f"已写入 {path}（{len(content)} 字符）"

    def _tool_list_directory(self, args: dict[str, Any], active_skills: list[dict[str, Any]] | None = None) -> str:
        path = self._resolve_read_path(args.get("path"), active_skills, default_workspace=True)
        recursive = bool(args.get("recursive", False))
        limit = min(max(int(args.get("limit", 200)), 1), 1000)
        iterator = path.rglob("*") if recursive else path.iterdir()
        rows = []
        for item in iterator:
            relative = item.relative_to(path)
            rows.append(f"{'DIR ' if item.is_dir() else 'FILE'} {relative}")
            if len(rows) >= limit:
                rows.append(f"... 已达到 {limit} 条上限")
                break
        return "\n".join(rows) or "目录为空"

    @staticmethod
    def _expand_glob_braces(pattern: str) -> list[str]:
        """展开 glob 模式里的 {a,b,c} 花括号组（pathlib.glob 不支持花括号）。

        例如 "**/*.{png,jpg}" -> ["**/*.png", "**/*.jpg"]。仅做展开，不校验路径。
        """
        import re

        results = [pattern]
        changed = True
        while changed:
            changed = False
            nxt: list[str] = []
            for pat in results:
                m = re.search(r"\{([^{}]*)\}", pat)
                if not m:
                    nxt.append(pat)
                    continue
                changed = True
                for option in m.group(1).split(","):
                    nxt.append(pat[: m.start()] + option + pat[m.end():])
            results = nxt
        seen: set[str] = set()
        ordered: list[str] = []
        for pat in results:
            if pat not in seen:
                seen.add(pat)
                ordered.append(pat)
        return ordered

    def _tool_search_files(self, args: dict[str, Any], active_skills: list[dict[str, Any]] | None = None) -> str:
        root = self._resolve_read_path(args.get("path"), active_skills, default_workspace=True)
        query = str(args.get("query") or "")
        pattern = str(args.get("pattern") or "*")
        limit = min(max(int(args.get("limit", 100)), 1), 500)
        max_file_size = min(max(int(args.get("max_file_size", 5 * 1024 * 1024)), 1), 50 * 1024 * 1024)
        regex = bool(args.get("regex", False))
        ignore_case = bool(args.get("ignore_case", False))
        context_lines = min(max(int(args.get("context_lines", 0)), 0), 10)
        multiline = bool(args.get("multiline", False))
        if not query:
            raise ValueError("query 不能为空")
        if regex:
            import re

            flags = re.MULTILINE if multiline else 0
            if ignore_case:
                flags |= re.IGNORECASE
            try:
                compiled = re.compile(query, flags)
            except re.error as exc:
                raise ValueError(f"正则无效：{exc}") from exc
        matches: list[str] = []
        for pat in self._expand_glob_braces(pattern):
            if len(matches) >= limit:
                break
            for path in root.rglob(pat):
                if not path.is_file() or path.stat().st_size > max_file_size:
                    continue
                try:
                    content = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if not content:
                    continue
                found = self._search_one_file(
                    path, content, query, regex=regex,
                    ignore_case=ignore_case, context_lines=context_lines,
                    multiline=multiline, compiled=compiled if regex else None,
                )
                if found:
                    matches.append(found)
                    if len(matches) >= limit:
                        return "\n".join(matches)
        return "\n".join(matches) or "未找到匹配内容"

    @staticmethod
    def _search_one_file(
        path: Path,
        content: str,
        query: str,
        regex: bool,
        ignore_case: bool,
        context_lines: int,
        multiline: bool,
        compiled=None,
    ) -> str | None:
        """单文件搜索：兼容旧子串格式；正则支持多行与上下文。命中返回多行文本，未命中返回 None。"""
        # ---- 多行正则：跨行匹配，输出命中块与所在行范围 ----
        if regex and multiline:
            spans = [(m.start(), m.end()) for m in compiled.finditer(content)]
            if not spans:
                return None
            line_breaks = [index for index, char in enumerate(content) if char == "\n"]
            import bisect

            def _line_of(offset: int) -> int:
                return 1 + bisect.bisect_left(line_breaks, offset)

            blocks: list[str] = []
            for start, end in spans:
                start_line = _line_of(start)
                end_line = _line_of(end - 1) if end > start else start_line
                snippet = content[start:end].replace("\n", "\\n")
                snippet = snippet[:600] + ("…" if len(snippet) > 600 else "")
                blocks.append(f"  match {start_line}-{end_line} 行: {snippet}")
                if len(blocks) >= 20:
                    blocks.append("  ... 命中过多，已截断")
                    break
            return f"{path}: {len(spans)} 处命中\n" + "\n".join(blocks)
        # ---- 逐行匹配（子串或正则），支持上下文 ----
        lines = content.splitlines()
        hits: list[int] = []
        if regex:
            for index, line in enumerate(lines):
                if compiled.search(line):
                    hits.append(index)
        else:
            needle = query.lower()
            for index, line in enumerate(lines):
                if needle in line.lower():
                    hits.append(index)
        if not hits:
            return None
        if context_lines <= 0:
            # 兼容旧格式：path:行号: 内容（子串与正则一致，不破坏既有解析）
            rows = []
            for index in hits[:100]:
                rows.append(f"{path}:{index + 1}: {lines[index].strip()[:500]}")
            if len(hits) > 100:
                rows.append(f"{path}: ... 共 {len(hits)} 处命中，仅显示前 100 处")
            return "\n".join(rows)
        # 带上下文：命中行距不超过 2*context+1 的相邻命中合为一块，避免重复打印同一上下文
        blocks: list[str] = []
        shown = 0
        runs: list[list[int]] = []
        current: list[int] = []
        for index in hits:
            if current and index - current[-1] > context_lines * 2 + 1:
                runs.append(current)
                current = []
            current.append(index)
        if current:
            runs.append(current)
        for run in runs:
            first = max(0, run[0] - context_lines)
            last = min(len(lines) - 1, run[-1] + context_lines)
            body = [f"{path}:{line_no}: {lines[line_no]}" for line_no in range(first, last + 1)]
            blocks.append("\n".join(body))
            shown += len(run)
            if shown >= 100:
                blocks.append(f"{path}: ... 已显示 {shown} 处命中，剩余省略")
                break
        return "\n\n".join(blocks)

    def _tool_glob_files(self, args: dict[str, Any], active_skills: list[dict[str, Any]] | None = None) -> str:
        root = self._resolve_read_path(args.get("path"), active_skills, default_workspace=True)
        pattern = str(args.get("pattern") or "**/*")
        limit = min(max(int(args.get("limit", 200)), 1), 2000)
        rows: list[str] = []
        for pat in self._expand_glob_braces(pattern):
            if len(rows) >= limit:
                break
            for item in root.glob(pat):
                if item.is_file():
                    rows.append(str(item))
                    if len(rows) >= limit:
                        break
        return "\n".join(rows) or "未找到匹配文件"

    def _tool_edit_file(self, args: dict[str, Any]) -> str:
        path = self._resolve_tool_path(args.get("path"))
        old = str(args.get("old_text") or "")
        new = str(args.get("new_text") or "")
        if not old:
            raise ValueError("old_text 不能为空")
        text = path.read_text(encoding="utf-8", errors="replace")
        count = text.count(old)
        if count == 0:
            raise ValueError("old_text 未找到，文件未修改")
        replace_all = bool(args.get("all", False))
        if count > 1 and not replace_all:
            raise ValueError(f"old_text 匹配 {count} 次；请缩小片段或明确 all=true")
        replaced_count = count if replace_all else 1
        new_text = text.replace(old, new, -1 if replace_all else 1)
        path.write_text(new_text, encoding="utf-8")
        summary = f"已修改 {path}：替换 {replaced_count} 处"
        diff = self._render_unified_diff(path, text, new_text)
        return f"{summary}\n{diff}" if diff else summary

    @staticmethod
    def _render_unified_diff(path: Path, before: str, after: str, max_lines: int = 60) -> str:
        """用 difflib 生成紧凑 unified diff（改动前后各一行上下文），供模型自审。

        只输出前 max_lines 行，超出部分截断，避免撑爆上下文。
        """
        import difflib

        if before == after:
            return ""
        lines = list(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=str(path),
                tofile=str(path),
                n=1,
            )
        )
        head = ["\n改动 diff（- 删除行 / + 新增行）："]
        head.append("".join(lines[:max_lines]).rstrip("\n"))
        if len(lines) > max_lines:
            head.append(f"... diff 共 {len(lines)} 行，仅显示前 {max_lines} 行")
        return "\n".join(head)

    def _tool_pwsh(self, args: dict[str, Any]) -> str:
        command = str(args.get("command") or "").strip()
        if not command:
            raise ValueError("command 不能为空")
        cwd = self._resolve_tool_path(args.get("cwd"), default_workspace=True)
        timeout = min(max(int(args.get("timeout", self.command_timeout)), 1), 900)
        max_output = min(max(int(args.get("max_output", 50000)), 0), 200000)
        completed = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                POWERSHELL_UTF8_PREFIX + "\n" + command,
            ],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = (completed.stdout + ("\n" + completed.stderr if completed.stderr else "")).strip()
        return f"exit_code={completed.returncode}\n{output[:max_output]}"

    def _tool_run_skill_script(self, args: dict[str, Any], active_skills: list[dict[str, Any]]) -> str:
        skill_name = str(args.get("skill") or "")
        relative_script = str(args.get("script") or "")
        skill = next((item for item in active_skills if item["name"] == skill_name or item["id"] == skill_name), None)
        if not skill:
            raise ValueError(f"当前未激活技能：{skill_name}")
        root = Path(skill["root"]).resolve()
        script = (root / relative_script).resolve()
        try:
            script.relative_to(root)
        except ValueError as exc:
            raise ValueError("脚本路径越过技能目录") from exc
        if not script.is_file():
            raise FileNotFoundError(script)
        raw_args = args.get("args") or []
        if not isinstance(raw_args, list):
            raise ValueError("args 必须是数组")
        suffix = script.suffix.lower()
        if suffix == ".py":
            if getattr(sys, "frozen", False):
                # 冻结版下 sys.executable 是 naiba-chat.exe：直接执行脚本会二次启动
                # 主程序并触发实例锁，必须走隐藏入口（仅执行脚本、不初始化服务/锁）。
                command = [sys.executable, "--run-skill-script", str(script), *map(str, raw_args)]
            else:
                command = [self.python_executable, str(script), *map(str, raw_args)]
        elif suffix == ".ps1":
            invocation = "& " + " ".join(
                _powershell_literal(item) for item in [script, *map(str, raw_args)]
            )
            command = [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                POWERSHELL_UTF8_PREFIX + "\n" + invocation,
            ]
        elif suffix in {".js", ".mjs", ".cjs"}:
            command = ["node", str(script), *map(str, raw_args)]
        else:
            command = [str(script), *map(str, raw_args)]
        timeout = min(max(int(args.get("timeout", self.command_timeout)), 1), 900)
        completed = subprocess.run(
            command,
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = (completed.stdout + ("\n" + completed.stderr if completed.stderr else "")).strip()
        return f"exit_code={completed.returncode}\n{output[:50000]}"

    def _tool_http_request(self, args: dict[str, Any]) -> str:
        url = str(args.get("url") or "")
        method = str(args.get("method") or "GET").upper()
        headers = args.get("headers") or {}
        data = args.get("body")
        max_bytes = min(max(int(args.get("max_bytes", 100000)), 1024), 2_000_000)
        encoded = None
        if data is not None:
            if isinstance(data, (dict, list)):
                encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            else:
                encoded = str(data).encode("utf-8")
        request = urllib.request.Request(url, data=encoded, headers=headers, method=method)
        try:
            with net_io.open(request, timeout=min(int(args.get("timeout", 60)), 180)) as response:
                body = response.read(max_bytes).decode("utf-8", errors="replace")
                return f"HTTP {response.status}\n{body}"
        except urllib.error.HTTPError as exc:
            return f"HTTP {exc.code}\n{exc.read(max_bytes).decode('utf-8', errors='replace')}"

    def _tool_call_mcp(
        self, args: dict[str, Any], active_skills: list[dict[str, Any]] | None = None,
    ) -> tuple[bool, str]:
        server = str(args.get("server") or "")
        tool = str(args.get("tool") or "")
        arguments = args.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("MCP arguments 必须是对象")
        if not server or not tool:
            raise ValueError("server 和 tool 不能为空")
        # Older Skills emitted the local server name for read-only tools.
        if tool in {"read_file", "list_directory", "search_files"} and server in {"naiba-chat", "comfyui"}:
            handler = getattr(self, f"_tool_{tool}", None)
            if handler:
                return True, str(handler(arguments, active_skills))
        # Map the historical ComfyUI id to the official server registration.
        if server in {"naiba-chat", "comfyui"} and self.mcp_registry.connection("comfy-mcp") is not None:
            server = "comfy-mcp"
        # Compatibility for prompts written before the official server id was
        # standardized. The old legacy ids now point to comfy-mcp.
        if self.mcp_registry.connection(server) is None and server in {"naiba-chat", "comfyui", "comfyui-mcp"}:
            if self.mcp_registry.connection("comfy-mcp") is not None:
                server = "comfy-mcp"
        return self.mcp_registry.call(server, tool, arguments)

    def _tool_register_mcp(self, args: dict[str, Any]) -> str:
        if not self.mcp_register:
            raise RuntimeError("当前 NaibaChat 版本不支持自动注册 MCP")
        return json.dumps(self.mcp_register(args), ensure_ascii=False, indent=2)

    def mcp_tool_guide(self) -> str:
        return self.mcp_registry.tool_guide()




