"""内置 Agent 与工具目录（Agent 编辑页的可选工具集）。

从 server.py 拆出的纯函数与常量，无运行时依赖。
"""
from __future__ import annotations

from typing import Any

# 内置 Agent（PLAN4 §Agent 与权限）：默认“全开启”，用户可自定义其 tool_scope。
# tool_scope 定义该 Agent 允许使用的工具集合；运行流中再与对话权限（allowed_tools）取交集。
# 内置 Agent 使用当前对话选择的模型。
# 全部可选工具：内置 Agent 的 tool_scope 默认“全开启”，用户可再按需裁剪。
_BUILT_IN_SCOPE_ALL = (
    "read_file", "list_directory", "search_files", "glob_files",
    "write_file", "edit_file", "pwsh", "run_skill_script",
    "http_request", "web_search", "register_mcp", "call_mcp",
    "run_in_background", "job_output", "job_status", "job_wait", "job_kill", "subagent",
    "todo_write", "artifact_report",
    "comfyui_prepare_workflow", "comfyui_batch",
    "install_skill",
    "vision_describe", "vision_ground", "vision_detect", "vision_ocr", "vision_colors",
    "vision_crop", "vision_pixel_diff", "vision_read_folder",
)


def built_in_agents() -> list[dict[str, Any]]:
    """返回内置 Agent 定义清单（含 tool_scope）。每次调用返回新副本，防止被外部篡改。"""
    return [
        {
            "id": "dsh-standard",
            "name": "dsh-standard（全能）",
            "system_prompt": (
                "你是 naiba-chat 的全能内置 Agent，拥有完整工具、Skill、联网搜索与子任务能力。MCP 仅在用户显式配置外部服务并授权工具时可用，不属于默认能力。"
                "对任何领域的多步任务执行通用闭环：盘点能力与输入，补齐可恢复缺口，执行并收集后台结果，"
                "验证产物，失败时依据证据修正后重试；涉及文件改动先说明范围。"
            ),
            "skill_ids": [],
            "tool_scope": list(_BUILT_IN_SCOPE_ALL),
            "built_in": True,
        },
        {
            "id": "dsh-code",
            "name": "dsh-code（编程）",
            "system_prompt": (
                "你是专注编程的内置 Agent，适合多步编码、测试与批量修改。"
                "优先用读取/编辑/搜索/命令工具完成任务；复杂任务可拆给子 Agent。"
            ),
            "skill_ids": [],
            "tool_scope": list(_BUILT_IN_SCOPE_ALL),
            "built_in": True,
        },
        {
            "id": "dsh-minimal",
            "name": "dsh-minimal（极简编码）",
            "system_prompt": (
                "你是编码 Agent。默认拥有全部工具与扩展能力（联网搜索、视觉、子 Agent、MCP 入口；MCP 仅在用户显式配置并授权后可用）。"
                "按需选择恰当工具完成任务，不必局限于某几类。"
            ),
            "skill_ids": [],
            "tool_scope": list(_BUILT_IN_SCOPE_ALL),
            "built_in": True,
        },
        {
            "id": "dsh-cordis",
            "name": "dsh-cordis（创作工坊）",
            "system_prompt": (
                "你是创作工坊 Agent，用于生成与维护自定义 Agent、Skill、提示词与工作流。"
                "擅长阅读/编写技能目录与脚本，必要时用子 Agent 拆分复杂创作任务。"
            ),
            "skill_ids": [],
            "tool_scope": list(_BUILT_IN_SCOPE_ALL),
            "built_in": True,
        },
    ]


def built_in_agent_ids() -> set[str]:
    return {agent["id"] for agent in built_in_agents()}


# ---- 工具目录（Agent 编辑页的可选工具集）----
# 按职责分组；每个工具的 model_target 标注它是给文本模型（走视觉车道）还是给多模态
# 视觉模型（vision_read_folder 直接看图）用的；default_selected 决定新建 Agent 的默认勾选。
_ALIAS_MAIN = {
    "read": "read_file", "write": "write_file", "edit": "edit_file",
    "glob": "glob_files", "grep": "search_files",
}
_TOOL_GROUP = {
    "read_file": "文件读取/搜索", "list_directory": "文件读取/搜索", "search_files": "文件读取/搜索",
    "glob_files": "文件读取/搜索",
    "write_file": "文件写入/编辑", "edit_file": "文件写入/编辑",
    "pwsh": "命令执行",
    "run_skill_script": "Skill 脚本",
    "http_request": "网络", "web_search": "网络",
    "register_mcp": "MCP", "call_mcp": "MCP",
    "run_in_background": "后台/Job/子任务", "job_output": "后台/Job/子任务", "job_status": "后台/Job/子任务",
    "job_wait": "后台/Job/子任务", "job_kill": "后台/Job/子任务", "subagent": "后台/Job/子任务",
    "todo_write": "后台/Job/子任务", "artifact_report": "后台/Job/子任务",
    "comfyui_prepare_workflow": "ComfyUI", "comfyui_batch": "ComfyUI",
    "install_skill": "能力/Skill 管理", "unpack_skill_archive": "能力/Skill 管理", "inspect_installed_skill": "能力/Skill 管理",
    "vision_describe": "视觉（文本模型）", "vision_ground": "视觉（文本模型）", "vision_detect": "视觉（文本模型）",
    "vision_ocr": "视觉（文本模型）", "vision_colors": "视觉（文本模型）",
    "vision_crop": "视觉（文本模型）", "vision_pixel_diff": "视觉（文本模型）",
    "vision_read_folder": "视觉（视觉模型）",
}
_MODEL_TARGET = {
    "vision_read_folder": "vision",
    "vision_describe": "text", "vision_ground": "text", "vision_detect": "text",
    "vision_ocr": "text", "vision_colors": "text", "vision_crop": "text", "vision_pixel_diff": "text",
}
# 新建 Agent 的默认勾选：常用基础工具 + 让视觉模型看图的 vision_read_folder；MCP/ComfyUI/后台Job/
# 能力Skill 网关默认不选，用户可自行开启并明确其成本。
_DEFAULT_SELECTED_TOOLS = frozenset({
    "read_file", "write_file", "list_directory", "search_files", "glob_files", "edit_file",
    "pwsh", "run_skill_script", "http_request", "web_search", "vision_read_folder",
})


def tool_catalog_entries(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从 tool_registry schemas 构建 Agent 编辑页的工具目录（不显示 5 个别名）。"""
    known = {str(spec.get("name") or ""): spec for spec in schemas if isinstance(spec, dict)}
    entries: list[dict[str, Any]] = []
    for name in known:
        if name in _ALIAS_MAIN:
            continue  # 别名与主工具等价，不单独显示
        description = str(known[name].get("description") or "")
        # 只取描述第一行作为短说明
        first_line = description.splitlines()[0] if description else ""
        entries.append({
            "name": name,
            "description": first_line[:120],
            "group": _TOOL_GROUP.get(name, "其他"),
            "model_target": _MODEL_TARGET.get(name, "any"),
            "default_selected": name in _DEFAULT_SELECTED_TOOLS,
            "alias_of": _ALIAS_MAIN.get(name),
        })
    # 端到端顺序：把前端呈现顺序稳定化，避免逐轮随机
    order = (
        "read_file", "write_file", "list_directory", "search_files", "glob_files", "edit_file",
        "pwsh", "run_skill_script", "http_request", "web_search",
        "register_mcp", "call_mcp",
        "run_in_background", "job_output", "job_status", "job_wait", "job_kill", "subagent",
        "todo_write", "artifact_report",
        "comfyui_prepare_workflow", "comfyui_batch",
        "install_skill", "unpack_skill_archive", "inspect_installed_skill",
        "vision_read_folder", "vision_describe", "vision_ground", "vision_detect", "vision_ocr",
        "vision_colors", "vision_crop", "vision_pixel_diff",
    )
    index = {name: i for i, name in enumerate(order)}
    entries.sort(key=lambda item: (index.get(item["name"], 999), item["name"]))
    return entries
