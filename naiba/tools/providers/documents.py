# -*- coding: utf-8 -*-
"""documents 域工具 Provider：read_pdf / pdf_render_pages / pdf_zoom_region 单一定义。

实现函数签名与 core 域一致（ctx 构造注入、显式参数）；缓存目录按"当前数据目录"
动态获取（data_dir_getter 注入 app.paths.data_dir，防 rebind 漂移——教训 17 同构：
上下文/路径必须显式传递，不闭包捕获装配期可变状态）。

权限策略：
- read_pdf：与 read_file 同构（_make_read_policy：工作区/Skill 根内免确认，越界必确认）；
- pdf_render_pages / pdf_zoom_region：只写宿主管控缓存目录（服务层 path_within 校验），
  无确认（permission="auto"）；缓存产物受 /api/imaging/clean 与统计覆盖。

错误语义：业务校验失败抛 ValueError（引擎/调用方转为"失败+明确文本"给模型）。
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Callable

from naiba import pdf as pdf_svc
from naiba.tools.providers.core import ToolContext, _make_read_policy
from naiba.tools.registry import ToolSpec, build_document_tool_specs


def _no_confirm_policy(
    tool: str,
    arguments: dict[str, Any],
    active_skills: list[dict[str, Any]],
    permission_mode: str,
    run_context: dict[str, Any] | None,
    workspace: Path | None = None,
) -> str:
    """渲染/放大只写宿主管控缓存目录（服务层 path_within 校验），无需用户确认。"""
    return ""


def _resolve_any_path(ctx: ToolContext, raw: Any, active_skills: list[dict[str, Any]]) -> Path:
    """与 core 同构的路径解析（相对路径按工作区/Skill 根），允许任意绝对路径。"""
    from naiba.tools.providers.core import _resolve_read_path

    return _resolve_read_path(ctx, raw, active_skills)


def _read_pdf_impl(
    ctx: ToolContext, args: dict[str, Any], active_skills: list[dict[str, Any]], data_dir: Path
) -> str:
    path = _resolve_any_path(ctx, args.get("path"), active_skills)
    start_raw = args.get("start_page", 1)
    try:
        start_page = 1 if start_raw in (None, "") else int(start_raw)
    except (TypeError, ValueError):
        raise ValueError("start_page 必须是整数")
    end_raw = args.get("end_page")
    try:
        end_page = None if end_raw in (None, "") else int(end_raw)
    except (TypeError, ValueError):
        raise ValueError("end_page 必须是整数")
    result = pdf_svc.extract_pdf_text(path, start_page, end_page)
    if not result.get("has_text_layer"):
        return str(result.get("hint") or "该 PDF 没有可提取的文本层。")
    text = str(result.get("text") or "")
    lines = [text]
    if result.get("truncated"):
        total = int(result.get("total_pages") or 0)
        resume = int(result.get("resume_start_page") or 0)
        note = (
            f"（已达单次读取上限：共 {total} 页，已提取第 "
            f"{result.get('range_start')}-{result.get('range_end')} 页"
            + (f"；如需继续请用 start_page={resume} 重读）" if resume else "）"))
        lines.append(note)
    return "\n".join(lines)


def _render_pages_impl(
    ctx: ToolContext, args: dict[str, Any], active_skills: list[dict[str, Any]], data_dir: Path
) -> str:
    path = _resolve_any_path(ctx, args.get("path"), active_skills)
    result = pdf_svc.render_pdf_pages(path, data_dir, pages=args.get("pages"))
    return json.dumps(
        {
            "total_pages": result["total_pages"],
            "rendered": result["pages"],
            "next_step": "对返回的页图路径调用 vision_analyze 识别内容；细节看不清时用 pdf_zoom_region 局部放大。",
        },
        ensure_ascii=False,
    )


def _zoom_region_impl(
    ctx: ToolContext, args: dict[str, Any], active_skills: list[dict[str, Any]], data_dir: Path
) -> str:
    path = _resolve_any_path(ctx, args.get("path"), active_skills)
    try:
        page = int(args.get("page", 0))
    except (TypeError, ValueError):
        raise ValueError("page 必须是整数")
    scale_raw = args.get("scale", 3)
    try:
        scale = 3 if scale_raw in (None, "") else int(scale_raw)
    except (TypeError, ValueError):
        raise ValueError("scale 必须是 1-6 的整数")
    result = pdf_svc.render_pdf_region(path, data_dir, page, args.get("region"), scale=scale)
    return json.dumps(
        {**result, "next_step": "对返回的局部高清图路径调用 vision_analyze 精读内容。"},
        ensure_ascii=False,
    )


_IMPLS: dict[str, Callable[..., str]] = {
    "read_pdf": _read_pdf_impl,
    "pdf_render_pages": _render_pages_impl,
    "pdf_zoom_region": _zoom_region_impl,
}


class DocumentToolProvider:
    """documents 域：3 个 PDF 工具单一定义（schema × 实现函数 + def 级策略）。"""

    def __init__(self, context: ToolContext, data_dir_getter: Callable[[], Path]) -> None:
        self._context = context
        self._data_dir = data_dir_getter

    def _make_execute(self, impl: Callable[..., str]) -> Callable[..., tuple[bool, str]]:
        """工厂：为单个工具创建 execute 绑定（循环内直接捕获会共享最后一个 impl）。"""

        def _execute(
            arguments: dict[str, Any],
            active_skills: list[dict[str, Any]],
            _run_context: dict[str, Any] | None = None,
        ) -> tuple[bool, str]:
            try:
                result = impl(self._context, arguments, active_skills, self._data_dir())
                return True, result
            except ValueError as exc:
                return False, str(exc)
            except OSError as exc:
                return False, f"文件操作失败：{exc}"
            except Exception as exc:  # noqa: BLE001 - 工具边界：不吞，转明确失败文本给模型
                return False, f"PDF 处理失败：{type(exc).__name__}: {exc}"

        return _execute

    def tools(self) -> list[ToolSpec]:
        rows: list[ToolSpec] = []
        for spec in build_document_tool_specs():
            impl = _IMPLS[spec.name]
            policy = _make_read_policy(self._context) if spec.name == "read_pdf" else _no_confirm_policy
            rows.append(dataclasses.replace(spec, execute=self._make_execute(impl), policy=policy))
        return rows
