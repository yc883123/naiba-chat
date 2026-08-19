"""Generic capability discovery and safe local Skill installation."""
from __future__ import annotations

import json
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
        _run_context: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        requested_tools = [str(item) for item in arguments.get("tools") or []]
        requested_skills = [str(item) for item in arguments.get("skills") or []]
        requested_mcp = [str(item) for item in arguments.get("mcp_servers") or []]
        executables = [str(item) for item in arguments.get("executables") or []]
        paths = [str(item) for item in arguments.get("paths") or []]

        schemas = self.app.tool_registry.schemas()
        tools = {
            str(spec.get("name") or ""): str(spec.get("description") or "")
            for spec in schemas if spec.get("name")
        }
        skills = self.app.catalog.scan()
        skill_names = {
            str(skill.get("name") or "").lower(): skill for skill in skills
        }
        mcp = self._mcp_summary(self.app.mcp.states())
        mcp_by_id = {row["id"].lower(): row for row in mcp}

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
            "tools": [{"name": name, "description": description} for name, description in tools.items()],
            "skills": [
                {
                    "id": str(skill.get("id") or ""),
                    "name": str(skill.get("name") or ""),
                    "description": str(skill.get("description") or "")[:500],
                    "requires_mcp": bool(skill.get("requires_mcp")),
                }
                for skill in skills
            ],
            "mcp_servers": mcp,
        }
        return True, json.dumps(result, ensure_ascii=False)

    def install_skill(
        self,
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        _run_context: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
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
            "install_skill": self.install_skill,
        }
