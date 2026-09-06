"""core 域工具 Provider：11 个核心工具「schema + 实现函数」单一定义。

实现函数自 ``ToolExecutor._tool_*`` 原样抽取（self → ctx，字节语义等价；Phase 3 双轨对拍：
旧方法保留至 Phase 5，本模块为权威实现），共用的路径解析/规则函数随迁。

执行统一签名：``fn(ctx, arguments, active_skills=None)``（返回值与旧方法一致：
多数返回 str，``call_mcp`` 返回 ``(success, result)``）；绑定为 ToolSpec.execute 时
统一归一为 ``(arguments, active_skills, run_context) -> (success, result)``。
"""
from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from naiba import net as net_io
from naiba.core.paths import path_within
from naiba.tools.registry import ToolSpec, build_core_tool_specs, build_harness_alias_specs

POWERSHELL_UTF8_PREFIX = (
    "$utf8 = [System.Text.UTF8Encoding]::new($false); "
    "[Console]::InputEncoding = $utf8; [Console]::OutputEncoding = $utf8; $OutputEncoding = $utf8;"
)


def _powershell_literal(value: Any) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@dataclass
class ToolContext:
    """core 域工具的执行上下文（构造注入，不摸 APP 全局）。"""

    workspace: Path
    python_executable: str
    command_timeout: int
    mcp_registry: Any
    mcp_register: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---- 路径解析（自 ToolExecutor 原样抽取；workspace 参数采用当前运行工作区，缺省回退 ctx。防止装配期配置漂移） ----

def _resolve_tool_path(
    ctx: ToolContext, raw: Any, default_workspace: bool = False, workspace: Path | None = None,
) -> Path:
    ws = workspace if workspace is not None else ctx.workspace
    value = str(raw or "").strip()
    if not value and default_workspace:
        return ws
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ws / path
    return path.resolve()


def _read_roots(workspace: Path, active_skills: list[dict[str, Any]]) -> list[Path]:
    roots = [workspace]
    for skill in active_skills:
        value = str(skill.get("root") or "").strip()
        if not value:
            continue
        root = Path(value).expanduser().resolve()
        if root not in roots:
            roots.append(root)
    return roots


def _resolve_read_path(
    ctx: ToolContext,
    raw: Any,
    active_skills: list[dict[str, Any]] | None = None,
    default_workspace: bool = False,
    workspace: Path | None = None,
) -> Path:
    """Resolve relative paths against an active Skill before workspace."""
    ws = workspace if workspace is not None else ctx.workspace
    value = str(raw or "").strip()
    if not value and default_workspace:
        return ws
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    workspace_candidate = (ws / path).resolve()
    if workspace_candidate.exists():
        return workspace_candidate
    matches: list[Path] = []
    for root in _read_roots(ws, active_skills or [])[1:]:
        candidate = (root / path).resolve()
        if path_within(candidate, root) and candidate.exists():
            matches.append(candidate)
    unique = list(dict.fromkeys(matches))
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        choices = "、".join(str(item) for item in unique[:4])
        raise ValueError(f"相对路径在多个 active Skill 中存在，请改用绝对路径：{choices}")
    return workspace_candidate


# ---- 实现函数（自 ToolExecutor._tool_* 原样抽取） ----

def _tool_read_file(ctx: ToolContext, args: dict[str, Any], active_skills: list[dict[str, Any]] | None = None) -> str:
    path = _resolve_read_path(ctx, args.get("path"), active_skills)
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


def _tool_write_file(ctx: ToolContext, args: dict[str, Any], active_skills: list[dict[str, Any]] | None = None) -> str:
    path = _resolve_tool_path(ctx, args.get("path"))
    content = str(args.get("content") or "")
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.get("append") else "w"
    with path.open(mode, encoding="utf-8", newline="") as handle:
        handle.write(content)
    return f"已写入 {path}（{len(content)} 字符）"


def _tool_list_directory(ctx: ToolContext, args: dict[str, Any], active_skills: list[dict[str, Any]] | None = None) -> str:
    path = _resolve_read_path(ctx, args.get("path"), active_skills, default_workspace=True)
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


def _expand_glob_braces(pattern: str) -> list[str]:
    """展开 glob 模式里的 {a,b,c} 花括号组（pathlib.glob 不支持花括号）。

    例如 "**/*.{png,jpg}" -> ["**/*.png", "**/*.jpg"]。仅做展开，不校验路径。
    """
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


def _tool_search_files(ctx: ToolContext, args: dict[str, Any], active_skills: list[dict[str, Any]] | None = None) -> str:
    root = _resolve_read_path(ctx, args.get("path"), active_skills, default_workspace=True)
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
    compiled = None
    if regex:
        flags = re.MULTILINE if multiline else 0
        if ignore_case:
            flags |= re.IGNORECASE
        try:
            compiled = re.compile(query, flags)
        except re.error as exc:
            raise ValueError(f"正则无效：{exc}") from exc
    matches: list[str] = []
    for pat in _expand_glob_braces(pattern):
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
            found = _search_one_file(
                path, content, query, regex=regex,
                ignore_case=ignore_case, context_lines=context_lines,
                multiline=multiline, compiled=compiled if regex else None,
            )
            if found:
                matches.append(found)
                if len(matches) >= limit:
                    return "\n".join(matches)
    return "\n".join(matches) or "未找到匹配内容"


def _tool_glob_files(ctx: ToolContext, args: dict[str, Any], active_skills: list[dict[str, Any]] | None = None) -> str:
    root = _resolve_read_path(ctx, args.get("path"), active_skills, default_workspace=True)
    pattern = str(args.get("pattern") or "**/*")
    limit = min(max(int(args.get("limit", 200)), 1), 2000)
    rows: list[str] = []
    for pat in _expand_glob_braces(pattern):
        if len(rows) >= limit:
            break
        for item in root.glob(pat):
            if item.is_file():
                rows.append(str(item))
                if len(rows) >= limit:
                    break
    return "\n".join(rows) or "未找到匹配文件"


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


def _tool_edit_file(ctx: ToolContext, args: dict[str, Any], active_skills: list[dict[str, Any]] | None = None) -> str:
    path = _resolve_tool_path(ctx, args.get("path"))
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
    diff = _render_unified_diff(path, text, new_text)
    return f"{summary}\n{diff}" if diff else summary


def _tool_pwsh(ctx: ToolContext, args: dict[str, Any], active_skills: list[dict[str, Any]] | None = None) -> str:
    command = str(args.get("command") or "").strip()
    if not command:
        raise ValueError("command 不能为空")
    cwd = _resolve_tool_path(ctx, args.get("cwd"), default_workspace=True)
    timeout = min(max(int(args.get("timeout", ctx.command_timeout)), 1), 900)
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


def _tool_run_skill_script(ctx: ToolContext, args: dict[str, Any], active_skills: list[dict[str, Any]]) -> str:
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
            command = [ctx.python_executable, str(script), *map(str, raw_args)]
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
    timeout = min(max(int(args.get("timeout", ctx.command_timeout)), 1), 900)
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


def _tool_http_request(ctx: ToolContext, args: dict[str, Any], active_skills: list[dict[str, Any]] | None = None) -> str:
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


def _tool_register_mcp(ctx: ToolContext, args: dict[str, Any], active_skills: list[dict[str, Any]] | None = None) -> str:
    if not ctx.mcp_register:
        raise RuntimeError("当前 NaibaChat 版本不支持自动注册 MCP")
    return json.dumps(ctx.mcp_register(args), ensure_ascii=False, indent=2)


# ---- 权限策略（Phase 5：引擎内建确认逻辑收敛到 def 级 policy） ----

_READ_FAMILY = {"read_file", "list_directory", "search_files", "glob_files"}
_CORE_CONFIRM_REASONS = {
    "pwsh": "执行 PowerShell 命令",
    "run_skill_script": "运行技能脚本",
    "register_mcp": "注册MCP服务",
}


def _http_request_policy(
    tool: str,
    arguments: dict[str, Any],
    active_skills: list[dict[str, Any]],
    permission_mode: str,
    run_context: dict[str, Any] | None,
    workspace: Path | None = None,
) -> str:
    """http_request 按 method 判定副作用（行为优化 Phase 2）：GET/HEAD 免确认；其余 auto 放行。"""
    method = str((arguments or {}).get("method") or "GET").upper()
    if method in {"GET", "HEAD"}:
        return ""
    if permission_mode == "auto":
        return ""
    return "发送HTTP请求"


def _make_read_policy(ctx: ToolContext) -> Any:
    def policy(
        tool: str,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        permission_mode: str,
        run_context: dict[str, Any] | None,
        workspace: Path | None = None,
    ) -> str:
        # 只读检查非破坏性：工作区内免确认（任意模式），越界必确认（不因 auto 放行）。
        # 工作区必须是"当前运行（会话级）工作区"（引擎传入），不得用装配期配置。
        ws = workspace if workspace is not None else ctx.workspace
        path = _resolve_read_path(ctx, arguments.get("path"), active_skills, tool != "read_file", ws)
        if not any(path_within(path, root) for root in _read_roots(ws, active_skills)):
            return "读取工作区外路径：" + str(path)
        return ""
    return policy


def _make_write_policy(ctx: ToolContext) -> Any:
    def policy(
        tool: str,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        permission_mode: str,
        run_context: dict[str, Any] | None,
        workspace: Path | None = None,
    ) -> str:
        ws = workspace if workspace is not None else ctx.workspace
        path = _resolve_tool_path(ctx, arguments.get("path"), workspace=ws)
        if permission_mode == "auto" and path_within(path, ws):
            return ""
        return f"写入文件：{path}"
    return policy


def _make_dangerous_policy(reason: str) -> Any:
    def policy(
        tool: str,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        permission_mode: str,
        run_context: dict[str, Any] | None,
        workspace: Path | None = None,
    ) -> str:
        if permission_mode == "auto":
            return ""
        return reason
    return policy


def _make_core_policy(ctx: ToolContext, name: str) -> Any:
    """core 工具 def 级权限策略；None 表示走引擎默认（不应发生，防御性）。"""
    if name in _READ_FAMILY:
        return _make_read_policy(ctx)
    if name in {"write_file", "edit_file"}:
        return _make_write_policy(ctx)
    if name == "http_request":
        return _http_request_policy
    reason = _CORE_CONFIRM_REASONS.get(name)
    if reason:
        return _make_dangerous_policy(reason)
    return None


# ---- bind 绑定 ----

_STR_TOOL_FNS: dict[str, Callable[..., Any]] = {
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "list_directory": _tool_list_directory,
    "search_files": _tool_search_files,
    "glob_files": _tool_glob_files,
    "edit_file": _tool_edit_file,
    "pwsh": _tool_pwsh,
    "run_skill_script": _tool_run_skill_script,
    "http_request": _tool_http_request,
    "register_mcp": _tool_register_mcp,
}

# Harness 兼容别名的实现与策略（别名 def 与 canonical 同实现、同策略；查询层 resolve 归一）
_ALIAS_IMPLS: dict[str, Callable[..., Any]] = {
    "read": _tool_read_file,
    "write": _tool_write_file,
    "edit": _tool_edit_file,
    "glob": _tool_glob_files,
    "grep": _tool_search_files,
}
_ALIAS_CANONICAL: dict[str, str] = {
    "read": "read_file",
    "write": "write_file",
    "edit": "edit_file",
    "glob": "glob_files",
    "grep": "search_files",
}


def _make_str_execute(ctx: ToolContext, fn: Callable[..., Any]) -> Any:
    def execute(arguments: dict[str, Any], active_skills: list[dict[str, Any]], run_context: dict[str, Any] | None = None) -> tuple[bool, str]:
        return True, fn(ctx, arguments, active_skills)
    return execute


class CoreToolProvider:
    """core 域 Provider：10 个核心工具（不含已删除的 call_mcp）的单一定义（schema + 实现函数绑定）。"""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    def tools(self) -> list[ToolSpec]:
        results: list[ToolSpec] = []
        for spec in build_core_tool_specs():
            if spec.name in _STR_TOOL_FNS:
                execute = _make_str_execute(self._context, _STR_TOOL_FNS[spec.name])
            else:
                continue
            results.append(
                dataclasses.replace(
                    spec,
                    execute=execute,
                    policy=_make_core_policy(self._context, spec.name),
                )
            )
        # Harness 兼容别名 def：与 canonical 同实现、同策略（单一定义，别名只存在于查询层归一）
        for spec in build_harness_alias_specs():
            canonical = _ALIAS_CANONICAL[spec.name]
            results.append(
                dataclasses.replace(
                    spec,
                    execute=_make_str_execute(self._context, _ALIAS_IMPLS[spec.name]),
                    policy=_make_core_policy(self._context, canonical),
                )
            )
        return results
