"""vision 域工具 Provider：2 个视觉工具单一定义（vision_analyze + vision_image_ops）。

处理器整块复用 ``VisionRouter.tool_handlers()``（绑定方法），本模块只做
schema × handler 绑定；每个已声明工具必须有 handler（否则报错，防静默缺失）。
"""
from __future__ import annotations

import dataclasses
from typing import Any

from naiba.tools.registry import ToolSpec, build_vision_tool_specs


class VisionToolProvider:
    """vision 域：vision_analyze（识图/装载）+ vision_image_ops（PIL 三合一）。"""

    def __init__(self, runtime: Any) -> None:
        self._handlers = dict(runtime.tool_handlers() or {})

    def tools(self) -> list[ToolSpec]:
        rows: list[ToolSpec] = []
        for spec in build_vision_tool_specs():
            handler = self._handlers.get(spec.name)
            if handler is None:
                raise LookupError(f"Vision tool_handlers 缺少声明工具：{spec.name}")
            rows.append(dataclasses.replace(spec, execute=handler, system=True))
        return rows
