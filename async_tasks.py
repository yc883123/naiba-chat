"""兼容 shim：Run 执行引擎已迁至 naiba.run.manager（外部旧导入路径保持不变）。"""

from __future__ import annotations

from naiba.run.manager import ActiveRunError, ConversationRunManager, _search_sources, _merge_usage_summary

__all__ = ["ActiveRunError", "ConversationRunManager", "_search_sources", "_merge_usage_summary"]
