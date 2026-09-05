"""兼容 shim：技能实现已迁至 naiba/skills/*（agent/catalog/install/policy/context）与
naiba/tools/executor（工具执行）；本模块仅保留旧导入路径（2026-09-06 收口）。"""

from __future__ import annotations

from typing import Any, Callable

from naiba.core.exceptions import TaskCancelled
from naiba.tools.executor import ToolExecutor
from naiba.skills.agent import SKILL_CONTENT_WARN_CHARS, SKILL_PROMPT_HEADER, SkillAgent
from naiba.skills.catalog import SkillCatalog, _frontmatter_value, _skill_display_name
from naiba.skills.context import DEFAULT_CONTEXT_WINDOW
from naiba.skills.install import (
    _zip_has_skill_md,
    delete_skill,
    remove_skill_references,
    validate_and_extract_archive,
    validate_and_install_skill,
)
from naiba.skills.policy import SKILL_POLICY_MODES, normalize_skill_policy

# 兼容别名：旧消费者（run/chat、run/manager、subagent 等）经本 shim 引用。
EventCallback = Callable[[dict[str, Any]], None]

__all__ = [
    "EventCallback",
    "SKILL_CONTENT_WARN_CHARS",
    "SKILL_POLICY_MODES",
    "SKILL_PROMPT_HEADER",
    "DEFAULT_CONTEXT_WINDOW",
    "SkillAgent",
    "SkillCatalog",
    "TaskCancelled",
    "ToolExecutor",
    "_frontmatter_value",
    "_skill_display_name",
    "_zip_has_skill_md",
    "delete_skill",
    "normalize_skill_policy",
    "remove_skill_references",
    "validate_and_extract_archive",
    "validate_and_install_skill",
]
