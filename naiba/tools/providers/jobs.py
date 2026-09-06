"""job/subagent 域工具 Provider：任务工具「schema + 执行函数」单一定义。

处理器来源（整块复用，不做改写型手术）：
- ``naiba.subagent.job_tool_handler_factory``：run_in_background/job_output/job_status/job_wait/job_kill；
- ``naiba.subagent.subagent_handler_factory``：subagent；
- 本模块函数：todo_write / artifact_report（自 app.py 处理器原样抽取，self→app 参数）。

依赖经构造参数注入（AppContext Protocol），不摸全局。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from functools import partial
from pathlib import Path
from typing import Any

from naiba.core.contracts import AppContext
from naiba.subagent import job_tool_handler_factory, subagent_handler_factory
from naiba.tools.registry import ToolSpec, build_job_tool_specs


def _todo_write_handler(
    app: AppContext, args: dict[str, Any], _skills: Any, run_context: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    run_id = str((run_context or {}).get("run_id") or (run_context or {}).get("job_id") or "")
    if not run_id:
        return False, "无法确定当前运行，不能保存任务清单"
    raw = (args or {}).get("todos")
    if not isinstance(raw, list) or len(raw) > 100:
        return False, "todos 必须是最多 100 项的数组"
    todos: list[dict[str, str]] = []
    active = 0
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            return False, f"第 {index} 项不是对象"
        content = str(item.get("content") or "").strip()
        status = str(item.get("status") or "pending")
        if not content or status not in {"pending", "in_progress", "completed"}:
            return False, f"第 {index} 项缺少 content 或 status 无效"
        active += int(status == "in_progress")
        todos.append({"id": str(item.get("id") or index), "content": content[:1000], "status": status})
    if active > 1:
        return False, "同时最多只能有一个 in_progress 任务"
    app.storage.append_run_event(run_id, {"type": "todo_state", "todos": todos})
    return True, json.dumps({"saved": True, "todos": todos}, ensure_ascii=False)


def _artifact_report_handler(
    app: AppContext, args: dict[str, Any], _skills: Any, _run_context: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    paths = (args or {}).get("paths")
    if not isinstance(paths, list) or not paths or len(paths) > 200:
        return False, "paths 必须是 1 到 200 个文件路径"
    require_nonempty = bool((args or {}).get("require_nonempty", True))
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for raw in paths:
        path = Path(str(raw or "")).expanduser().resolve()
        try:
            if not path.is_file():
                raise FileNotFoundError(path)
            size = path.stat().st_size
            if require_nonempty and size <= 0:
                raise ValueError("文件为空")
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            rows.append({"path": str(path), "size": size, "sha256": digest.hexdigest()})
        except (OSError, ValueError) as exc:
            errors.append({"path": str(path), "error": str(exc)})
    result = {"status": "verified" if rows and not errors else ("partial" if rows else "failed"), "label": str((args or {}).get("label") or ""), "artifacts": rows, "errors": errors}
    return (not errors), json.dumps(result, ensure_ascii=False)


class JobToolProvider:
    """job/subagent 域：8 个任务工具（含 subagent/todo_write/artifact_report）单一定义。"""

    def __init__(self, app: AppContext) -> None:
        handlers = dict(job_tool_handler_factory(app))
        handlers["subagent"] = subagent_handler_factory(app)
        handlers["todo_write"] = partial(_todo_write_handler, app)
        handlers["artifact_report"] = partial(_artifact_report_handler, app)
        self._handlers = handlers

    def tools(self) -> list[ToolSpec]:
        rows: list[ToolSpec] = []
        for spec in build_job_tool_specs():
            handler = self._handlers.get(spec.name)
            if handler is None:
                continue
            rows.append(dataclasses.replace(spec, execute=handler, system=True))
        return rows
