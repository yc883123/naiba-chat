"""core 域工具 Provider：11 个核心工具「schema + 实现函数」单一定义。

实现函数自 ``ToolExecutor._tool_*`` 原样抽取（self → ctx，字节语义等价；Phase 3 双轨对拍：
旧方法保留至 Phase 5，本模块为权威实现），共用的路径解析/规则函数随迁。

执行统一签名：``fn(ctx, arguments, active_skills=None)``（返回值与旧方法一致：
多数返回 str，``call_mcp`` 返回 ``(success, result)``）；绑定为 ToolSpec.execute 时
统一归一为 ``(arguments, active_skills, run_context) -> (success, result)``。
"""
from __future__ import annotations

import dataclasses
import fnmatch
import json
import os
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
    # 宿主数据目录的动态 getter（None=无托管根）：读取策略把其下的 uploads/generated
    # 视为可信根（用户上传附件/宿主托管产物免确认）。动态获取防 rebind 漂移。
    data_dir_getter: Callable[[], Path] | None = None


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


def _read_roots(
    workspace: Path,
    active_skills: list[dict[str, Any]],
    data_dir: Path | None = None,
) -> list[Path]:
    """读取类工具的可信根：会话工作区 + active Skill 根 + 宿主托管缓存目录
    （uploads/generated——用户上传附件与宿主产物是"用户放进来的"，不属越界）。"""
    roots = [workspace]
    if data_dir is not None:
        for rel in ("uploads", "generated"):
            try:
                root = (data_dir / rel).resolve()
            except OSError:
                continue
            if root not in roots:
                roots.append(root)
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

# 二进制嗅探长度与判定：NUL 强判定；不可打印控制字符（非 \t\n\r）比例 >5% 辅助
# （GBK/UTF-8 中文文本无 NUL、无控制字符，不会被误伤；PDF/ZIP/图片几乎必含 NUL）。
_BINARY_SNIFF_BYTES = 8192


def _looks_binary(head: bytes) -> bool:
    if not head:
        return False
    if b"\x00" in head:
        return True
    ctrl = sum(1 for byte in head if byte < 0x20 and byte not in (0x09, 0x0A, 0x0D))
    return ctrl / len(head) > 0.05


def _tool_read_file(ctx: ToolContext, args: dict[str, Any], active_skills: list[dict[str, Any]] | None = None) -> str:
    """按行读取文本文件：行数不设默认上限，字符预算 30000 截断并返回可续读提示。

    预算：max_chars（默认/硬上限 30000 字符）唯一强制上限——内容不超预算时无论多少行
    都全量返回；模型显式传入 max_lines 时保留行数限制（兼容旧调用）。单行超过字符预算
    时按字符截断该行。截断时尾部标记实际返回的行区间与续读起点（start_line），模型无需猜测。

    二进制防护：UTF-8 replace 直读会把 PDF/ZIP/图片的二进制灌成乱码进模型上下文——
    先嗅探（NUL + 控制字符比例），命中则明确报错并按文件类型给出工具引导。
    """
    path = _resolve_read_path(ctx, args.get("path"), active_skills)
    try:
        with path.open("rb") as handle:
            head = handle.read(_BINARY_SNIFF_BYTES)
    except OSError:
        raise
    if _looks_binary(head):
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return (
                "该文件是 PDF 二进制文档，无法按文本读取。"
                "提取文本层请调用 read_pdf；若是扫描版（无文本层），"
                "请先用 pdf_render_pages 渲染页图，再对页图路径调用 vision_analyze 识别内容。"
            )
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            return "该文件是图片，无法按文本读取；需要看图请调用 vision_analyze 并传入图片路径。"
        return (
            "该文件为二进制格式，无法按文本读取。"
            "如确需读取内容，可用 pwsh 提取（如 txt/无文本层文件的解码），"
            "或让用户转换为文本/图片后再处理。"
        )
    try:
        # max_chars 对模型隐藏（schema 不含该参数）：执行层仍兼容旧调用，但硬上限
        # 30000 字符，防止任何入口把单次读取撑到超出预算（浪费上下文与 token）。
        max_chars = min(max(int(args.get("max_chars", 30000)), 100), 30000)
    except (TypeError, ValueError):
        max_chars = 30000
    # max_lines 缺省不限：内容 ≤ 30000 字符时全量返回（用户实测：>50 行但 <30000 字符
    # 的文件被行数上限截断，模型被迫多次续读）。显式传参时仍生效（1..5000）。
    try:
        max_lines_raw = args.get("max_lines")
        max_lines = None if max_lines_raw in (None, "") else min(max(int(max_lines_raw), 1), 5000)
    except (TypeError, ValueError):
        max_lines = None
    try:
        start_line = max(1, int(args.get("start_line") or 1))
    except (TypeError, ValueError):
        start_line = 1
    end_line = None
    if args.get("end_line") is not None:
        try:
            end_line = int(args.get("end_line"))
        except (TypeError, ValueError):
            end_line = None
    with_numbers = bool(args.get("with_line_numbers", False))

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    total_lines = len(lines)
    if total_lines == 0:
        return "（文件为空）"
    if start_line > total_lines:
        return f"（文件共 {total_lines} 行，start_line={start_line} 已超出范围，请在 1-{total_lines} 之间选取）"
    limit_idx = total_lines
    if end_line is not None:
        if end_line < start_line:
            return f"（end_line={end_line} 小于 start_line={start_line}，无法读取）"
        limit_idx = min(total_lines, end_line)

    chosen: list[str] = []
    chars = 0
    truncated = False
    cut_line_no = 0
    for idx in range(start_line - 1, limit_idx):
        if max_lines is not None and len(chosen) >= max_lines:
            truncated = True
            break
        remaining = max_chars - chars
        if remaining <= 0:
            truncated = True
            break
        line = lines[idx]
        if len(line) > remaining:
            # 字符预算触达：当前行按余量截断（去掉行尾换行，避免半行+标记粘连）
            chosen.append(line[:remaining].rstrip("\n"))
            truncated = True
            cut_line_no = idx + 1
            break
        chosen.append(line)
        chars += len(line)

    if with_numbers:
        text = "".join(f"{start_line + i}: {line}" for i, line in enumerate(chosen))
    else:
        text = "".join(chosen)
    if not truncated:
        return text
    last_displayed = cut_line_no if cut_line_no else start_line + len(chosen) - 1
    resume = cut_line_no if cut_line_no else last_displayed + 1
    details = [
        f"…（已达读取上限：文件共 {total_lines} 行，已返回第 {start_line}-{last_displayed} 行",
    ]
    if cut_line_no:
        details.append(f"；第 {cut_line_no} 行按字符预算截断")
    details.append(f"；如需继续请用 start_line={resume} 重读）")
    return text + "\n" + "".join(details)


def _tool_write_file(ctx: ToolContext, args: dict[str, Any], active_skills: list[dict[str, Any]] | None = None) -> str:
    path = _resolve_tool_path(ctx, args.get("path"))
    content = str(args.get("content") or "")
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.get("append") else "w"
    with path.open(mode, encoding="utf-8", newline="") as handle:
        handle.write(content)
    return f"已写入 {path}（{len(content)} 字符）"


def _tool_list_directory(ctx: ToolContext, args: dict[str, Any], active_skills: list[dict[str, Any]] | None = None) -> str:
    """列出目录内容（glob 单入口：原 glob_files 已并入，pattern/files_only 等价）。

    确定性输出：按绝对路径字符串排序（名称序）；超限附续枚举提示（start_after=上一批末条路径）。
    """
    path = _resolve_read_path(ctx, args.get("path"), active_skills, default_workspace=True)
    recursive = bool(args.get("recursive", False))
    limit = min(max(int(args.get("limit", 200)), 1), 1000)
    start_after = str(args.get("start_after") or "").strip()
    pattern = str(args.get("pattern") or "*").strip() or "*"
    files_only = bool(args.get("files_only", False))

    if recursive:
        candidates = list(path.rglob("*"))
        matched = [
            item for item in candidates
            if any(fnmatch.fnmatch(str(item.relative_to(path)), pat)
                   for pat in _expand_glob_braces(pattern))
        ]
    else:
        matched = [
            item for item in path.iterdir()
            if any(fnmatch.fnmatch(item.name, pat)
                   for pat in _expand_glob_braces(pattern))
        ]
    ordered = sorted(matched, key=str)
    filtered = [item for item in ordered if not start_after or str(item) > start_after]
    rows = []
    for item in filtered[:limit]:
        if files_only and item.is_dir():
            continue
        rows.append(f"{'DIR ' if item.is_dir() else 'FILE'} {item}")
    text = "\n".join(rows) or "未找到匹配文件"
    if len(filtered) > limit and rows:
        last = rows[-1].split(" ", 1)[-1]
        text += (
            f"\n…（共 {len(filtered)} 项，已列出前 {limit} 项，按名称排序；"
            f"如需更多请用 start_after=「{last}」重试）"
        )
    return text


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
) -> tuple[str | None, int]:
    """单文件搜索：兼容旧子串格式；正则支持多行与上下文。命中返回（文本，命中数），未命中 (None, 0)。

    子串模式与正则模式统一受 ignore_case 控制（默认区分大小写——语义不再随模式突变）。
    """
    # ---- 多行正则：跨行匹配，输出命中块与所在行范围 ----
    if regex and multiline:
        spans = [(m.start(), m.end()) for m in compiled.finditer(content)]
        if not spans:
            return None, 0
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
        return f"{path}: {len(spans)} 处命中\n" + "\n".join(blocks), len(spans)
    # ---- 逐行匹配（子串或正则），支持上下文 ----
    lines = content.splitlines()
    hits: list[int] = []
    if regex:
        for index, line in enumerate(lines):
            if compiled.search(line):
                hits.append(index)
    else:
        needle = query if not ignore_case else query.lower()
        for index, line in enumerate(lines):
            candidate = line if not ignore_case else line.lower()
            if needle in candidate:
                hits.append(index)
    if not hits:
        return None, 0
    if context_lines <= 0:
        # 兼容旧格式：path:行号: 内容（子串与正则一致，不破坏既有解析）
        rows = []
        for index in hits[:100]:
            rows.append(f"{path}:{index + 1}: {lines[index].strip()[:500]}")
        if len(hits) > 100:
            rows.append(f"{path}: ... 共 {len(hits)} 处命中，仅显示前 100 处")
        return "\n".join(rows), len(hits)
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
    return "\n\n".join(blocks), len(hits)


def _tool_search_files(ctx: ToolContext, args: dict[str, Any], active_skills: list[dict[str, Any]] | None = None) -> str:
    root = _resolve_read_path(ctx, args.get("path"), active_skills, default_workspace=True)
    if not root.exists():
        raise ValueError(f"路径不存在：{root}")
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

    def _search_single_file(path: Path) -> tuple[str | None, int]:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None, 0
        if not content:
            return None, 0
        return _search_one_file(
            path, content, query, regex=regex,
            ignore_case=ignore_case, context_lines=context_lines,
            multiline=multiline, compiled=compiled if regex else None,
        )

    # path 指向单个文件时：直接在该文件内搜索（pattern 仅目录模式下生效）。
    # 此前把文件路径静默当"空目录"处理，导致"未找到匹配内容"的误导性空结果、
    # 模型误以为文件被跳过/编码问题而反复试错。
    if root.is_file():
        found, hits = _search_single_file(root)
        if found:
            return found + (f"\n共 {hits} 处命中。" if hits else "")
        return "未找到匹配内容"

    matches: list[str] = []
    total_hits = 0
    skipped_large = 0
    for pat in _expand_glob_braces(pattern):
        if len(matches) >= limit:
            break
        for path in root.rglob(pat):
            if not path.is_file():
                continue
            if path.stat().st_size > max_file_size:
                skipped_large += 1
                continue
            found, hits = _search_single_file(path)
            if found:
                matches.append(found)
                total_hits += hits
                if len(matches) >= limit:
                    break
    summary = []
    if total_hits:
        summary.append(f"共 {total_hits} 处命中，已显示前 {len(matches)} 组")
    if skipped_large:
        summary.append(f"已跳过 {skipped_large} 个超过 {max_file_size} 字节的文件")
    body = "\n".join(matches) or ("未找到匹配内容" if not summary else "未找到匹配内容（详情见汇总）")
    return body + (("\n" + "；".join(summary) + "。") if summary else "")


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
    # Windows 管道下 Python 子进程默认用 locale 编码（GBK）输出 stdout/stderr，
    # 父进程按 UTF-8 解码会得到乱码（实测：中文路径参数/报错信息变 �）。强制子进程
    # UTF-8 运行时（PYTHONIOENCODING+PYTHONUTF8），输出与 argv 均为 UTF-8，解码匹配。
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    completed = subprocess.run(
        command,
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
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

_READ_FAMILY = {"read_file", "list_directory", "search_files"}
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
        # 只读检查非破坏性：工作区内/宿主托管缓存（用户上传附件与产物）免确认（任意模式），
        # 其余越界必确认（不因 auto 放行）。
        # 工作区必须是"当前运行（会话级）工作区"（引擎传入），不得用装配期配置。
        ws = workspace if workspace is not None else ctx.workspace
        data_dir = ctx.data_dir_getter() if ctx.data_dir_getter is not None else None
        path = _resolve_read_path(ctx, arguments.get("path"), active_skills, tool != "read_file", ws)
        if not any(path_within(path, root) for root in _read_roots(ws, active_skills, data_dir)):
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
    "grep": _tool_search_files,
}
_ALIAS_CANONICAL: dict[str, str] = {
    "read": "read_file",
    "write": "write_file",
    "edit": "edit_file",
    "grep": "search_files",
}


def _make_str_execute(ctx: ToolContext, fn: Callable[..., Any], name: str = "") -> Any:
    def execute(arguments: dict[str, Any], active_skills: list[dict[str, Any]], run_context: dict[str, Any] | None = None) -> tuple[bool, str]:
        result = fn(ctx, arguments, active_skills)
        return _result_success(name, result), result
    return execute


def _result_success(name: str, result: str) -> bool:
    """工具执行成败判定（与工具自身返回值一致的语义层）：

    - pwsh / run_skill_script：非零退出码 = 失败（原为恒 True，模型会把失败当成功）；
    - http_request：HTTP >= 400 = 失败（原为恒 True，404/500 被当成功）；
    - 其余工具：执行函数返回即成功（写入/查询类工具自身在结果文本中声明失败）。
    """
    text = str(result or "")
    if name in {"pwsh", "run_skill_script"}:
        match = re.match(r"^exit_code=(-?\d+)", text)
        if match:
            return int(match.group(1)) == 0
        return True
    if name == "http_request":
        match = re.match(r"^HTTP (\d{3})", text)
        if match:
            return int(match.group(1)) < 400
        return True
    return True


class CoreToolProvider:
    """core 域 Provider：10 个核心工具（不含已删除的 call_mcp）的单一定义（schema + 实现函数绑定）。"""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    def tools(self) -> list[ToolSpec]:
        results: list[ToolSpec] = []
        for spec in build_core_tool_specs():
            if spec.name in _STR_TOOL_FNS:
                execute = _make_str_execute(self._context, _STR_TOOL_FNS[spec.name], spec.name)
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
                    execute=_make_str_execute(self._context, _ALIAS_IMPLS[spec.name], spec.name),
                    policy=_make_core_policy(self._context, canonical),
                )
            )
        return results
