"""capability 域工具 Provider：Skill 安装/解压/定位工具单一定义。

处理器整块复用 ``CapabilityRuntime.tool_handlers()``（绑定方法），本模块只做
schema × handler 绑定（正则注册时"名字碰巧对上"的可能性由绑定时显式校验消除）。
"""
from __future__ import annotations

import dataclasses
from typing import Any

from naiba.tools.registry import ToolSpec, build_capability_tool_specs


class CapabilityToolProvider:
    """capability 域：inspect_installed_skill / install_skill / unpack_skill_archive。"""

    def __init__(self, runtime: Any) -> None:
        self._handlers = dict(runtime.tool_handlers() or {})

    def tools(self) -> list[ToolSpec]:
        rows: list[ToolSpec] = []
        for spec in build_capability_tool_specs():
            handler = self._handlers.get(spec.name)
            if handler is None:
                raise LookupError(f"Capability tool_handlers 缺少声明工具：{spec.name}")
            rows.append(dataclasses.replace(spec, execute=handler, system=True))
        return rows
