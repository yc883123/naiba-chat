"""测试共用：组装态工具注册表与引擎接线（单轨 Phase 5 起）。

生产装配（app.py）与测试同步走 Provider 绑定；本模块提供轻量组装：
- ``assembled_registry(workspace, mcp_registry)``：build_tool_registry + CoreToolProvider（绑定
  11 个 core def 的 execute/policy）——系统域（job/capability/vision/search/comfyui）不在此列，
  按需由调用方补充桩 Provider；
- ``wired_executor(workspace, mode, mcp_registry)``：引擎 + def/别名解析器接线（与生产等价）。

不启动 App、不触达存储与网络。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from naiba.mcp import MCPRegistry
from naiba.tools.executor import ToolExecutor
from naiba.tools.providers.core import CoreToolProvider, ToolContext
from naiba.tools.registry import build_tool_registry


def run_context_for(workspace: Path, **extra: Any) -> dict[str, Any]:
    """构造只带工作区的运行上下文（回归"判定/执行必须按 Run 快照工作区"用）。"""
    context: dict[str, Any] = {"workspace_dir": str(Path(workspace).expanduser().resolve())}
    context.update(extra)
    return context


def assembled_registry(workspace: Path, mcp_registry: Any = None) -> Any:
    mcp = mcp_registry if mcp_registry is not None else MCPRegistry([])
    registry = build_tool_registry()
    registry.bind_mcp(mcp)
    registry.register_provider(
        CoreToolProvider(
            ToolContext(
                workspace=workspace,
                python_executable=sys.executable,
                command_timeout=60,
                mcp_registry=mcp,
                mcp_register=None,
            )
        )
    )
    return registry


def wired_executor(workspace: Path, mode: str = "confirm", mcp_registry: Any = None) -> ToolExecutor:
    mcp = mcp_registry if mcp_registry is not None else MCPRegistry([])
    registry = assembled_registry(workspace, mcp)
    executor = ToolExecutor(workspace, sys.executable, 60, mcp, permission_mode=mode)
    executor.set_def_resolver(registry.get)
    executor.set_alias_resolver(registry.resolve)
    return executor
