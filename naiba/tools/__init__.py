"""tools 层：统一工具系统。

- ``registry``：单一定义声明表（ToolSpec）+ 单一插槽分发 + 别名查询层归一；
- ``executor``：ToolExecutor 引擎（权限确认状态机 + def 级政策评估）；
- ``providers``：按域工具定义（core/jobs/capability/vision/search/comfyui）。
"""
from naiba.tools.executor import ToolExecutor  # noqa: F401  (re-export)
from naiba.tools.registry import ToolRegistry, ToolSpec, build_tool_registry  # noqa: F401  (re-export)

__all__ = ["ToolExecutor", "ToolRegistry", "ToolSpec", "build_tool_registry"]
