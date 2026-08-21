"""Generic capability discovery and safe local Skill installation."""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from skill_runtime import validate_and_install_skill


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
        skill_rows = [
            {
                "id": str(skill.get("id") or ""),
                "name": str(skill.get("name") or ""),
                "description": str(skill.get("description") or "")[:500],
                "requires_mcp": bool(skill.get("requires_mcp")),
            }
            for skill in skills
            if not query
            or str(skill.get("id") or "").lower() in requested_skill_keys
            or str(skill.get("name") or "").lower() in requested_skill_keys
            or relevance(
                f"{skill.get('name') or ''} {skill.get('description') or ''}"
            ) > 0
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
                "需要 Skill 时先调用 activate_skill。"
            ),
        }
        return True, json.dumps(result, ensure_ascii=False)

    def activate_skill(
        self,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        run_context: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        requested = [str(item).strip() for item in arguments.get("skills") or [] if str(item).strip()]
        if not requested:
            return False, "至少需要一个 Skill ID 或名称"
        policy = (run_context or {}).get("skill_policy") or {}
        skills = self.app.catalog.scan()
        if str(policy.get("mode") or "") == "exclusive":
            allowed_ids = {str(item) for item in policy.get("skill_ids") or []}
            skills = [skill for skill in skills if str(skill.get("id") or "") in allowed_ids]
        by_key: dict[str, dict[str, Any]] = {}
        for skill in skills:
            by_key[str(skill.get("id") or "").lower()] = skill
            by_key[str(skill.get("name") or "").lower()] = skill
        selected: list[dict[str, Any]] = []
        missing: list[str] = []
        for key in requested[:4]:
            skill = by_key.get(key.lower())
            if skill is None:
                missing.append(key)
            elif skill not in selected:
                selected.append(skill)
        if missing:
            return False, "未找到或不允许激活 Skill：" + "、".join(missing)
        active_ids = {str(skill.get("id") or "") for skill in active_skills}
        activated: list[dict[str, str]] = []
        for skill in selected:
            skill_id = str(skill.get("id") or "")
            if skill_id not in active_ids:
                active_skills.append(skill)
                active_ids.add(skill_id)
            activated.append({"id": skill_id, "name": str(skill.get("name") or "")})
        if isinstance(run_context, dict):
            run_context["skills_changed"] = True
        return True, json.dumps({"activated": activated}, ensure_ascii=False)

    def install_skill(
        self,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        run_context: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        policy = (run_context or {}).get("skill_policy") or {}
        if str(policy.get("mode") or "") == "exclusive":
            return False, "exclusive 模式禁止安装或激活白名单外 Skill"
        source = str(arguments.get("source_path") or "").strip()
        if not source:
            return False, "缺少 source_path"
        configured = self.app.config.get_skills_dirs()
        destination_raw = str(arguments.get("destination") or "").strip()
        if not destination_raw:
            destination_raw = str(configured[0] if configured else "skills")
        destination = self.app.config._resolve_dir(destination_raw)
        result = validate_and_install_skill(
            source,
            destination,
            str(arguments.get("name") or "").strip() or None,
        )
        if not result.get("success"):
            return False, str(result.get("error") or "Skill 安装失败")

        self.app.config.add_skills_dir(destination_raw)
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

    def tool_handlers(self) -> dict[str, Any]:
        return {
            "capability_inventory": self.inventory,
            "activate_skill": self.activate_skill,
            "install_skill": self.install_skill,
        }
