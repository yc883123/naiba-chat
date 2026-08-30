"""Generic capability discovery and safe local Skill installation."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from skill_runtime import validate_and_install_skill, validate_and_extract_archive


class CapabilityRuntime:
    """Expose the runtime's actual capabilities to the agent loop."""

    def __init__(self, app: Any) -> None:
        self.app = app

    @staticmethod
    def _mcp_summary(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for state in states:
            tools = state.get("tools") if isinstance(state.get("tools"), list) else []
            rows.append({
                "id": str(state.get("id") or ""),
                "status": str(state.get("status") or ""),
                "connected": bool(state.get("connected")),
                "error": str(state.get("error") or ""),
                "tools": [str(tool.get("name") or "") for tool in tools if isinstance(tool, dict)],
            })
        return rows

    def inventory(
        self,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        run_context: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        query = str(arguments.get("query") or "").strip()
        requested_tools = [str(item) for item in arguments.get("tools") or []]
        requested_skills = [str(item) for item in arguments.get("skills") or []]
        requested_mcp = [str(item) for item in arguments.get("mcp_servers") or []]
        executables = [str(item) for item in arguments.get("executables") or []]
        paths = [str(item) for item in arguments.get("paths") or []]

        schemas = self.app.tool_registry.schemas()
        allowed_tools = {
            str(item) for item in (run_context or {}).get("allowed_tools") or [] if str(item)
        }
        if allowed_tools:
            schemas = [spec for spec in schemas if str(spec.get("name") or "") in allowed_tools]
        tools = {
            str(spec.get("name") or ""): str(spec.get("description") or "")
            for spec in schemas if spec.get("name")
        }
        skills = self.app.catalog.scan()
        policy = (run_context or {}).get("skill_policy") or {}
        if str(policy.get("mode") or "") == "exclusive":
            allowed_ids = {str(item) for item in policy.get("skill_ids") or []}
            skills = [skill for skill in skills if str(skill.get("id") or "") in allowed_ids]
        skill_names = {
            str(skill.get("name") or "").lower(): skill for skill in skills
        }
        mcp = self._mcp_summary(self.app.mcp.states())
        mcp_by_id = {row["id"].lower(): row for row in mcp}

        def relevance(value: str) -> int:
            if not query:
                return 1
            needle = re.sub(r"\s+", "", query.lower())
            haystack = re.sub(r"\s+", "", value.lower())
            score = 0
            for word in re.findall(r"[a-z0-9_.-]{3,}", query.lower()):
                if word in haystack:
                    score += 12
            for chunk in re.findall(r"[\u3400-\u9fff]{2,}", needle):
                for size in (4, 3, 2):
                    for index in range(max(0, len(chunk) - size + 1)):
                        if chunk[index:index + size] in haystack:
                            score += size
            return score

        requested_tool_keys = {name.lower() for name in requested_tools}
        tool_rows = [
            {
                "name": str(spec.get("name") or ""),
                "description": str(spec.get("description") or ""),
                "parameters": spec.get("parameters") or {"type": "object", "properties": {}},
            }
            for spec in schemas
            if not query
            or str(spec.get("name") or "").lower() in requested_tool_keys
            or relevance(f"{spec.get('name') or ''} {spec.get('description') or ''}") >= 3
        ]
        tool_rows.sort(
            key=lambda item: (
                -relevance(f"{item['name']} {item['description']}"),
                item["name"],
            )
        )
        tool_rows = tool_rows[:12]

        requested_skill_keys = {name.lower() for name in requested_skills}
        routing_message = str((run_context or {}).get("routing_message") or query).lower()
        mcp_intent = "mcp" in routing_message
        skill_rows = [
            {
                "id": str(skill.get("id") or ""),
                "name": str(skill.get("name") or ""),
                "description": str(skill.get("description") or "")[:500],
                "requires_mcp": bool(skill.get("requires_mcp")),
            }
            for skill in skills
            if (
                not skill.get("requires_mcp")
                or mcp_intent
            )
            and (
                not query
                or str(skill.get("id") or "").lower() in requested_skill_keys
                or str(skill.get("name") or "").lower() in requested_skill_keys
                or relevance(
                    f"{skill.get('name') or ''} {skill.get('description') or ''}"
                ) > 0
            )
        ]
        skill_rows.sort(
            key=lambda item: (
                -relevance(f"{item['name']} {item['description']}"),
                item["name"],
            )
        )
        skill_rows = skill_rows[:8]

        result = {
            "summary": {
                "tool_count": len(tools),
                "skill_count": len(skills),
                "mcp_server_count": len(mcp),
                "active_skills": [str(skill.get("name") or "") for skill in active_skills],
            },
            "checks": {
                "tools": {name: name in tools for name in requested_tools},
                "skills": {
                    name: name.lower() in skill_names for name in requested_skills
                },
                "mcp_servers": {
                    name: bool(mcp_by_id.get(name.lower(), {}).get("connected"))
                    for name in requested_mcp
                },
                "executables": {name: shutil.which(name) or "" for name in executables},
                "paths": {name: Path(name).expanduser().exists() for name in paths},
            },
            "tools": tool_rows,
            "skills": skill_rows,
            "mcp_servers": mcp,
            "next_step": (
                "调用已列出的工具时，优先使用接口工具；若该工具没有作为接口工具出现，"
                "输出兼容 JSON 动作："
                '{"type":"tool","tool":"工具名","arguments":{...}}。'
                "只有 skills 数组确实列出所需 Skill 时，才可使用其中的准确 ID 或完整名称；不得猜测 Skill 名称。"
            ),
        }
        return True, json.dumps(result, ensure_ascii=False)

    def install_skill(
        self,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        run_context: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        # 放宽：exclusive 模式下也允许安装（中途导入 Skill），供后续引用/使用。
        source = str(arguments.get("source_path") or "").strip()
        if not source:
            return False, "缺少 source_path"
        destination_raw = str(arguments.get("destination") or "").strip()
        if destination_raw:
            destination = self.app.config._resolve_dir(destination_raw)
        else:
            # 缺省装到托管目录（数据目录的上级 /skills），而不是遗留 APP_DIR/skills。
            destination = self.app.config.resolve_managed_skills_dir()
        result = validate_and_install_skill(
            source,
            destination,
            str(arguments.get("name") or "").strip() or None,
        )
        if not result.get("success"):
            return False, str(result.get("error") or "Skill 安装失败")

        # 把实际安装目标目录（绝对路径）固化进配置，供后续持久扫描。
        self.app.config.add_skills_dir(str(destination))
        # Register the resolved destination. Relative names must resolve against
        # the app's configured skill root, not the process working directory.
        self.app.catalog.add_directory(destination)
        installed_path = Path(str(result.get("path") or "")).expanduser().resolve()
        installed = next(
            (
                skill for skill in self.app.catalog.scan()
                if Path(str(skill.get("path") or "")).expanduser().resolve() == installed_path
            ),
            None,
        )
        if installed is None:
            installed = next(
                (
                    skill for skill in self.app.catalog.scan()
                    if str(skill.get("name") or "").lower()
                    == str(result.get("name") or "").lower()
                    and Path(str(skill.get("root") or "")).expanduser().resolve()
                    == installed_path.parent.resolve()
                ),
                None,
            )
        if installed and not any(
            Path(str(skill.get("path") or "")).expanduser().resolve()
            == installed_path
            for skill in active_skills
        ):
            active_skills.append(installed)
        return True, json.dumps(result, ensure_ascii=False)

    def unpack_skill_archive(
        self,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        run_context: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """校验并解压一个 Skill zip 到工作区专属子目录（由后端代码做强校验）。

        AI 拿到解压后的文件夹路径后，再用 install_skill 安装。
        """
        archive_path = str(arguments.get("archive_path") or "").strip()
        if not archive_path:
            return False, "缺少 archive_path"
        name = str(arguments.get("name") or "").strip() or None
        workspace = getattr((run_context or {}).get("executor"), "workspace", None) or ""
        if not workspace:
            return False, "无法确定工作区目录"
        incoming = Path(workspace).expanduser().resolve() / ".skill_incoming"
        result = validate_and_extract_archive(archive_path, incoming, name)
        if not result.get("success"):
            return False, str(result.get("error") or "解压校验失败")
        return True, json.dumps(result, ensure_ascii=False)

    def tool_handlers(self) -> dict[str, Any]:
        return {
            "capability_inventory": self.inventory,
            "install_skill": self.install_skill,
            "unpack_skill_archive": self.unpack_skill_archive,
        }
