"""ConfigStore：配置持久化与读写（模型、MCP、Agent、目录等）。

从 server.py 拆出。依赖 config_helpers / image_utils / agent_catalog / model_media 的纯函数。
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

import app_state
from agent_catalog import built_in_agent_ids, built_in_agents
from config_helpers import (
    LOCAL_REQUEST_FORMATS,
    ONLINE_REQUEST_FORMATS,
    VALID_LOCAL_BACKENDS,
    VALID_MODEL_KINDS,
    _infer_kind_for_request_format,
    default_config,
)
from image_utils import path_within, validate_skills_dir
from model_media import _context_window_source, _infer_context_window, _infer_supports_images

class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        defaults = default_config()
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    defaults.update(loaded)
            except (OSError, json.JSONDecodeError):
                pass
        # 嵌套默认值合并：用户配置若只写了部分子字段，补齐缺失键。
        for key in ("vision", "search"):
            merged = dict(default_config().get(key, {}))
            if isinstance(defaults.get(key), dict):
                merged.update(defaults[key])
            defaults[key] = merged
        # Build 74 changes the historical 120-second Run-wide vision budget
        # into a 180-second timeout for each individual visual request. Only
        # migrate the old default; preserve explicit custom timeout values.
        vision = defaults.get("vision")
        if isinstance(vision, dict) and vision.get("timeout_ms") == 120000:
            vision["timeout_ms"] = 180000
        # MCP 配置去重：重复 server id 只保留首个（PLAN4 §MCP）。
        servers = defaults.get("mcp_servers")
        if isinstance(servers, list):
            seen: dict[str, int] = {}
            deduped = []
            for server in servers:
                if not isinstance(server, dict):
                    continue
                sid = str(server.get("id") or "").strip()
                # The legacy custom ComfyUI bridge is retired. Never revive it
                # from a migrated per-user config or an older portable build.
                if sid == "comfyui":
                    continue
                if not sid or sid in seen:
                    if sid:
                        print(f"[config] Ignored duplicate MCP server id: {sid}")
                    continue
                seen[sid] = 1
                deduped.append(server)
            defaults["mcp_servers"] = deduped
        # Remove the retired bundled Skill from migrated skill roots. The
        # official first-party Skill is the only Comfy MCP integration.
        roots = defaults.get("skills_dirs")
        if isinstance(roots, list):
            defaults["skills_dirs"] = [
                item for item in roots
                if "comfyui-mcp" not in str(item).lower()
            ]
        self.data = defaults
        # Legacy builds persisted max_agent_steps; it is intentionally ignored.
        self.data.pop("max_agent_steps", None)
        self._migrate_default_agent_skills()
        self._migrate_legacy_tool_names()
        tools = self.data.get("agent_tools")
        # run_command 已并入 pwsh：历史默认集里保存的是 run_command（而非 pwsh）。
        # 先统一映射死工具名，避免升级后通用 Agent 静默丢失命令执行能力。
        if isinstance(tools, list):
            mapped = ["pwsh" if str(item) == "run_command" else item for item in tools]
            # MCP is an explicit external integration, never a default capability.
            # Remove the exact historical default pair while preserving a user's
            # separately selected MCP tools and configured server definitions.
            legacy_default = {
                "read_file", "write_file", "list_directory", "search_files",
                "run_skill_script", "http_request",
                "register_mcp", "call_mcp",
            }
            # 旧配置只要等同于「历史默认工具集」（含 run_command 或已为 pwsh 都算）
            # 就移除默认 MCP 入口；定制过的工具集保留原选择，仅做死工具名映射。
            if set(mapped) <= legacy_default | {"pwsh"}:
                self.data["agent_tools"] = [
                    item for item in mapped if item not in {"register_mcp", "call_mcp"}
                ]
            elif mapped != tools:
                self.data["agent_tools"] = mapped
        # 在线/本地模型配置分层：为旧 providers 补全 kind/local_backend，并生成 default_model_key。
        self._migrate_model_profiles()
        self.save()

    def _migrate_default_agent_skills(self) -> None:
        """Remove historical domain Skills from the general Agent default."""
        legacy = {"0a3afda21c5622e1", "e03778f862d10595"}
        agents = self.data.get("agents")
        if not isinstance(agents, list):
            return
        for agent in agents:
            if not isinstance(agent, dict) or str(agent.get("id") or "") != "general":
                continue
            skills = agent.get("skill_ids")
            if not isinstance(skills, list):
                agent["skill_ids"] = []
                continue
            agent["skill_ids"] = [str(item) for item in skills if str(item) not in legacy]

    def _migrate_legacy_tool_names(self) -> None:
        """run_command 已并入 pwsh：清理持久化 Agent 工具范围里的死工具名。"""
        agents = self.data.get("agents")
        if not isinstance(agents, list):
            return
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            scope = agent.get("tool_scope")
            if isinstance(scope, list):
                agent["tool_scope"] = [
                    "pwsh" if str(item) == "run_command" else item
                    for item in scope
                ]

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)

    def public(self) -> dict[str, Any]:
        with self.lock:
            result = {
                key: value
                for key, value in self.data.items()
                if key not in {
                    "access_token", "providers", "mcp_servers",
                    "temperature", "max_tokens", "context_size",
                }
            }
            result["resolved_workspace_dir"] = str(self.resolve_workspace_dir())
            result["resolved_data_dir"] = str(self.resolve_data_dir())
            return result

    def get_skills_dirs(self) -> list[str]:
        with self.lock:
            return list(self.data.get("skills_dirs", []))

    def get_hidden_skill_ids(self) -> list[str]:
        with self.lock:
            values = self.data.get("hidden_skill_ids", [])
            return [str(item) for item in values] if isinstance(values, list) else []

    def hide_skill(self, skill_id: str) -> list[str]:
        skill_id = str(skill_id or "").strip()
        if not skill_id:
            return self.get_hidden_skill_ids()
        with self.lock:
            hidden = self.data.setdefault("hidden_skill_ids", [])
            if skill_id not in hidden:
                hidden.append(skill_id)
                self.save()
            return list(hidden)

    def unhide_skill(self, skill_id: str) -> list[str]:
        """从 hidden_skill_ids 移除该 id 并持久化；与 hide_skill 对称。"""
        skill_id = str(skill_id or "").strip()
        with self.lock:
            hidden = self.data.setdefault("hidden_skill_ids", [])
            if skill_id in hidden:
                hidden.remove(skill_id)
                self.save()
            return list(hidden)

    def add_skills_dir(self, raw: str) -> str:
        raw = (raw or "").strip()
        if not raw:
            raise ValueError("目录路径不能为空")
        resolved = self._resolve_dir(raw)
        validate_skills_dir(resolved)
        with self.lock:
            dirs = self.data.setdefault("skills_dirs", [])
            if raw not in dirs:
                dirs.append(raw)
            self.save()
        return str(resolved)

    def remove_skills_dir(self, raw: str) -> list[str]:
        raw = (raw or "").strip()
        resolved = str(self._resolve_dir(raw)) if raw else ""
        with self.lock:
            dirs = self.data.setdefault("skills_dirs", [])
            self.data["skills_dirs"] = [
                item for item in dirs if item != raw and str(self._resolve_dir(item)) != resolved
            ]
            self.save()
            return list(self.data["skills_dirs"])

    def get_starter_prompts(self) -> list[dict[str, str]]:
        with self.lock:
            items = self.data.get("starter_prompts", [])
            if isinstance(items, list):
                return [dict(item) for item in items if isinstance(item, dict)]
            return []

    def add_starter_prompt(self, title: str, text: str) -> list[dict[str, str]]:
        title = " ".join(str(title or "").strip().split())[:40] or "自定义指令"
        text = str(text or "").strip()
        if not text:
            raise ValueError("指令内容不能为空")
        with self.lock:
            prompts = self.data.setdefault("starter_prompts", [])
            if not isinstance(prompts, list):
                prompts = []
                self.data["starter_prompts"] = prompts
            prompts.append({"title": title, "text": text})
            self.save()
        return self.get_starter_prompts()

    def remove_starter_prompt(self, index: int) -> list[dict[str, str]]:
        with self.lock:
            prompts = self.data.setdefault("starter_prompts", [])
            if isinstance(prompts, list) and 0 <= int(index) < len(prompts):
                prompts.pop(int(index))
                self.save()
        return self.get_starter_prompts()

    def update_starter_prompt(self, index: int, title: str, text: str) -> list[dict[str, str]]:
        title = " ".join(str(title or "").strip().split())[:40] or "自定义指令"
        text = str(text or "").strip()
        if not text:
            raise ValueError("指令内容不能为空")
        with self.lock:
            prompts = self.data.setdefault("starter_prompts", [])
            if isinstance(prompts, list) and 0 <= int(index) < len(prompts):
                prompts[int(index)] = {"title": title, "text": text}
                self.save()
        return self.get_starter_prompts()

    def _resolve_dir(self, raw: str) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (app_state.APP_DIR / path).resolve()
        return path.resolve()

    def resolve_workspace_dir(self, raw: str | None = None) -> Path:
        """解析工作区目录：相对路径以 EXE 所在目录为基准（不受启动目录影响）。"""
        raw = (raw if raw is not None else self.data.get("workspace_dir", "workspace") or "workspace").strip()
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (app_state.EXE_DIR / path).resolve()
        return path.resolve()

    def resolve_data_dir(self, raw: str | None = None) -> Path:
        """Resolve persistent data storage; relative paths are relative to app_state.APP_DIR."""
        value = raw if raw is not None else self.data.get("data_dir", "data")
        path = Path(str(value or "data")).expanduser()
        if not path.is_absolute():
            path = app_state.APP_DIR / path
        return path.resolve()

    def resolve_managed_skills_dir(self, raw: str | None = None) -> Path:
        """持久化 Skills 目录（单一事实来源）：位于数据目录内的 ``skills`` 文件夹。

        默认落在 ``resolve_data_dir() / "skills"``，使 Skills 随数据目录离开
        C 盘 app_state.APP_DIR，不再写死为 ``app_state.APP_DIR/skills``，也不放在数据目录同级。
        """
        return (self.resolve_data_dir(raw) / "skills").resolve()

    def skills_dirs_resolved(self) -> list[Path]:
        """返回解析后的 Skills 扫描目录，旧 ``app_state.APP_DIR/skills`` 重定向到托管目录。

        托管目录（managed）始终排在最前作为唯一持久化入口；随后是用户自定义目录。
        过滤去重，跳过解析失败或不安全的项。
        """
        managed = self.resolve_managed_skills_dir()
        legacy_managed = (app_state.APP_DIR / "skills").resolve()
        result: list[Path] = [managed]
        for raw in self.data.get("skills_dirs", []):
            try:
                resolved = self._resolve_dir(str(raw))
            except (OSError, ValueError):
                continue
            if resolved == legacy_managed:
                resolved = managed
            if resolved in result:
                continue
            try:
                validate_skills_dir(resolved)
            except ValueError:
                continue
            result.append(resolved)
        return result

    def validate_data_dir(self, resolved: Path) -> None:
        resolved = resolved.resolve()
        if resolved.parent == resolved:
            raise ValueError("不能把磁盘根目录作为数据目录")
        system_roots = [Path(os.environ.get("SystemRoot", r"C:\Windows"))]
        for env_name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            value = os.environ.get(env_name)
            if value:
                system_roots.append(Path(value))
        for root in system_roots:
            root = root.resolve()
            if resolved == root or path_within(resolved, root):
                raise ValueError(f"不允许使用系统目录作为数据目录：{root}")
        if resolved == app_state.PUBLIC_DIR.resolve() or resolved == app_state.EXE_DIR.resolve():
            raise ValueError("不能把程序目录作为数据目录，请选择独立目录")

    def ensure_data_dir_writable(self, resolved: Path) -> None:
        self.validate_data_dir(resolved)
        resolved.mkdir(parents=True, exist_ok=True)
        probe = resolved / ".naiba_data_write_test"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise ValueError(f"数据目录不可写：{resolved}（{exc}）")

    def validate_workspace_dir(self, resolved: Path) -> None:
        """拒绝磁盘根目录、系统目录、程序数据目录等过宽或危险路径。"""
        resolved = resolved.resolve()
        if resolved.parent == resolved:
            raise ValueError("不能把磁盘根目录作为工作区")
        system_roots = [Path(os.environ.get("SystemRoot", r"C:\Windows"))]
        for env_name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            value = os.environ.get(env_name)
            if value:
                system_roots.append(Path(value))
        for root in system_roots:
            root = root.resolve()
            if resolved == root or path_within(resolved, root):
                raise ValueError(f"不允许使用系统目录作为工作区：{root}")
        forbidden_exact = {
            Path.home().resolve(),
            app_state.APP_DIR,
            app_state.DATA_DIR.resolve(),
            app_state.PUBLIC_DIR.resolve(),
        }
        if resolved in forbidden_exact:
            raise ValueError("不能把程序数据目录或用户主目录作为工作区，请使用其子目录")

    def ensure_workspace_writable(self, resolved: Path) -> None:
        """创建工作区目录并验证可读写性；不允许则抛出。"""
        self.validate_workspace_dir(resolved)
        resolved.mkdir(parents=True, exist_ok=True)
        probe = resolved / ".naiba_write_test"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.read_text(encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise ValueError(f"工作区目录不可读写：{resolved}（{exc}）")

    def public_providers(self) -> list[dict[str, Any]]:
        with self.lock:
            return [
                {
                    **provider,
                    "api_key": "",
                    "has_api_key": bool(provider.get("api_key")),
                    "context_window": _infer_context_window(provider),
                    "context_window_source": _context_window_source(provider),
                }
                for provider in self.data.get("providers", [])
            ]

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "host",
            "provider_id",
            "temperature",
            "max_tokens",
            "context_size",
            "agent_system_prompt",
            "permission_mode",
            "agent_tools",
            "command_timeout",
            "access_token",
            "workspace_dir",
            "data_dir",
            "imaging",
            "vision",
            "search",
            "workspaces",
        }
        with self.lock:
            for key in allowed:
                if key in values:
                    if key == "host":
                        host = str(values[key] or "").strip()
                        if host not in {"127.0.0.1", "0.0.0.0"}:
                            raise ValueError("host 只能是 127.0.0.1 或 0.0.0.0")
                        self.data[key] = host
                    elif key == "access_token":
                        token = str(values[key]).strip()
                        if not token:
                            raise ValueError("访问口令不能为空")
                        if len(token) < 4:
                            raise ValueError("访问口令至少 4 位")
                        self.data[key] = token
                    elif key == "agent_system_prompt":
                        self.data[key] = str(values[key])[:12000]
                    elif key == "permission_mode":
                        mode = str(values[key] or "confirm").strip().lower()
                        if mode not in {"confirm", "auto", "full"}:
                            raise ValueError("权限模式必须是 confirm、auto 或 full")
                        self.data[key] = mode
                    elif key == "agent_tools":
                        valid_tools = {
                            "read_file", "write_file", "list_directory", "search_files",
                            "pwsh", "run_skill_script", "http_request",
                        }
                        requested = values[key] if isinstance(values[key], list) else []
                        self.data[key] = [tool for tool in requested if tool in valid_tools]
                    elif key == "workspace_dir":
                        raw = str(values[key] or "").strip()
                        if not raw:
                            # 恢复默认：EXE 所在目录下的 workspace。
                            raw = "workspace"
                        resolved = self.resolve_workspace_dir(raw)
                        self.ensure_workspace_writable(resolved)
                        self.data[key] = raw
                    elif key == "data_dir":
                        raw = str(values[key] or "").strip() or "data"
                        resolved = self.resolve_data_dir(raw)
                        self.ensure_data_dir_writable(resolved)
                        self.data[key] = raw
                    elif key == "context_size":
                        self.data[key] = self._positive_context_size(values[key], "context_size")
                    elif key in ("vision", "search", "imaging"):
                        incoming = values[key]
                        if not isinstance(incoming, dict):
                            raise ValueError(f"{key} 必须是对象")
                        # 合并到现有子配置，避免丢失其他子字段。
                        merged = dict(self.data.get(key, {}))
                        for sub_key, sub_value in incoming.items():
                            merged[str(sub_key)] = sub_value
                        if key == "imaging":
                            merged["image_upload_original"] = bool(merged.get("image_upload_original", False))
                            for field in ("image_max_pixels", "thumbnail_max_pixels"):
                                try:
                                    merged[field] = max(1, int(merged.get(field) or 0))
                                except (TypeError, ValueError):
                                    raise ValueError(f"{field} 必须是正整数") from None
                        self.data[key] = merged
                    else:
                        self.data[key] = values[key]
            self.save()
            result = self.public()
            # Keep the legacy response field for older clients that still
            # validate context_size. It is excluded from bootstrap settings
            # and is never used to build model requests.
            if "context_size" in values:
                result["context_size"] = self.data["context_size"]
            return result

    def upsert_mcp_server(self, values: dict[str, Any]) -> dict[str, Any]:
        server_id = str(values.get("id") or "").strip()
        command = str(values.get("command") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", server_id):
            raise ValueError("MCP 服务 ID 只能包含字母、数字、下划线或连字符")
        if not command:
            raise ValueError("MCP command 不能为空")
        args = values.get("args") or []
        env = values.get("env") or {}
        if not isinstance(args, list) or not isinstance(env, dict):
            raise ValueError("MCP args 必须是数组，env 必须是对象")
        payload = {
            "id": server_id,
            "command": command,
            "args": [str(item) for item in args],
            "env": {str(key): str(value) for key, value in env.items()},
            "enabled": bool(values.get("enabled", True)),
        }
        with self.lock:
            servers = self.data.setdefault("mcp_servers", [])
            index = next((i for i, item in enumerate(servers) if item.get("id") == server_id), None)
            if index is None:
                servers.append(payload)
            else:
                servers[index] = payload
            self.save()
        return payload

    def delete_mcp_server(self, server_id: str) -> bool:
        """Remove a registered MCP server from persistent config."""
        server_id = str(server_id or "").strip()
        with self.lock:
            servers = self.data.get("mcp_servers", [])
            before = len(servers)
            self.data["mcp_servers"] = [item for item in servers if item.get("id") != server_id]
            if len(self.data["mcp_servers"]) != before:
                self.save()
                return True
            return False

    def upsert_provider(self, values: dict[str, Any]) -> dict[str, Any]:
        """兼容旧接口，同时尊重显式 online/local 类型。"""
        request_format = str(values.get("request_format") or "openai_chat").strip().lower()
        payload = dict(values)
        kind = str(values.get("kind") or "").strip().lower()
        payload["kind"] = kind if kind in VALID_MODEL_KINDS else _infer_kind_for_request_format(request_format)
        if payload["kind"] == "local":
            payload["local_backend"] = request_format
        return self.upsert_model_profile(payload)

    def delete_provider(self, provider_id: str) -> bool:
        """兼容别名：按 id 删除（不区分 online/local）。"""
        with self.lock:
            providers = self.data.setdefault("providers", [])
            before = len(providers)
            self.data["providers"] = [item for item in providers if item.get("id") != provider_id]
            removed = len(self.data["providers"]) < before
            default = self.data.get("default_model_key") or ""
            if default.endswith(f":{provider_id}"):
                remaining = self.data.get("providers", [])
                self.data["default_model_key"] = (
                    f"{remaining[0].get('kind', 'online')}:{remaining[0].get('id')}" if remaining else ""
                )
            if self.data.get("provider_id") == provider_id:
                self.data["provider_id"] = ""
            vision = self.data.get("vision")
            if isinstance(vision, dict) and str(vision.get("provider_model_key") or "").endswith(f":{provider_id}"):
                vision["provider_model_key"] = ""
            self.save()
            return removed

    def provider_secret(self, provider_id: str) -> str | None:
        with self.lock:
            provider = next(
                (item for item in self.data.get("providers", []) if item.get("id") == provider_id),
                None,
            )
            return str(provider.get("api_key") or "") if provider else None

    # ---- 在线 / 本地模型配置统一层 ----

    def _migrate_model_profiles(self) -> None:
        """启动时把旧 providers 分层为 online/local，并生成 default_model_key。

        不把旧的 local_model / model_mode 伪造成本地 API 配置。
        """
        providers = self.data.setdefault("providers", [])
        for provider in providers:
            if provider.get("kind") not in VALID_MODEL_KINDS:
                request_format = str(provider.get("request_format") or "openai_chat").strip().lower()
                kind = _infer_kind_for_request_format(request_format)
                provider["kind"] = kind
                if kind == "local":
                    provider["local_backend"] = request_format
                else:
                    provider.pop("local_backend", None)
            # 旧配置补全思维强度，默认 auto（不发送协议字段）。
            effort = str(provider.get("reasoning_effort") or "auto").strip().lower()
            if effort not in {"auto", "off", "low", "medium", "high"}:
                effort = "auto"
            provider["reasoning_effort"] = effort
            if provider.get("context_window") in (None, "") and provider.get("context_size") not in (None, ""):
                try:
                    provider["context_window"] = self._positive_context_size(
                        provider.get("context_size"), "context_window"
                    )
                except ValueError:
                    pass
            provider.pop("context_size", None)
        # 计算 default_model_key：旧 provider_id 指向的条目决定前缀。
        default_key = str(self.data.get("default_model_key") or "").strip()
        if not default_key:
            provider_id = str(self.data.get("provider_id") or "").strip()
            if provider_id:
                target = next(
                    (item for item in providers if item.get("id") == provider_id), None
                )
                if target:
                    default_key = f"{target.get('kind', 'online')}:{provider_id}"
        # 规范化 default_model_key，确保指向现存条目。
        if default_key:
            kind, _, model_id = default_key.partition(":")
            if kind not in VALID_MODEL_KINDS or not any(
                item.get("id") == model_id and item.get("kind") == kind for item in providers
            ):
                default_key = ""
        if not default_key and providers:
            first = providers[0]
            default_key = f"{first.get('kind', 'online')}:{first.get('id')}"
        self.data["default_model_key"] = default_key

    def default_model_key(self) -> str:
        with self.lock:
            return str(self.data.get("default_model_key") or "")

    def set_default_model_key(self, model_key: str) -> str:
        with self.lock:
            key = self._normalize_model_key(model_key)
            kind, _, model_id = key.partition(":")
            provider = next(
                (
                    item
                    for item in self.data.get("providers", [])
                    if item.get("id") == model_id and item.get("kind") == kind
                ),
                None,
            )
            if not provider:
                raise ValueError("模型配置不存在")
            self.data["default_model_key"] = key
            self.data["provider_id"] = model_id  # 兼容旧字段
            self.save()
            return key

    @staticmethod
    def _normalize_model_key(model_key: str) -> str:
        model_key = str(model_key or "").strip()
        if not model_key:
            return ""
        if ":" not in model_key:
            return f"online:{model_key}"
        return model_key

    def model_profiles(self, kind: str | None = None) -> list[dict[str, Any]]:
        """返回所有模型配置（脱敏），并附带 model_key 与是否默认。"""
        with self.lock:
            default = self.data.get("default_model_key") or ""
            result = []
            for provider in self.data.get("providers", []):
                entry_kind = provider.get("kind", "online")
                key = f"{entry_kind}:{provider.get('id')}"
                entry = dict(provider)
                entry["model_key"] = key
                entry["is_default"] = key == default
                entry["api_key"] = ""
                entry["has_api_key"] = bool(provider.get("api_key"))
                explicit_images = provider.get("supports_images")
                entry["supports_images_explicit"] = (
                    explicit_images if isinstance(explicit_images, bool) else None
                )
                entry["supports_images"] = _infer_supports_images(provider)
                entry["context_window"] = _infer_context_window(provider)
                entry["context_window_source"] = _context_window_source(provider)
                if provider.get("context_window"):
                    entry["context_size"] = provider.get("context_window")
                result.append(entry)
            if kind:
                result = [item for item in result if item.get("kind") == kind]
            return result

    def upsert_model_profile(self, values: dict[str, Any]) -> dict[str, Any]:
        """统一保存在线 API 或本地模型配置。"""
        model_id = str(values.get("id") or uuid.uuid4().hex[:12]).strip()
        kind = str(values.get("kind") or "online").strip().lower()
        if kind not in VALID_MODEL_KINDS:
            raise ValueError("模型类型必须是 online 或 local")
        if kind == "local":
            local_backend = str(
                values.get("local_backend") or values.get("request_format") or ""
            ).strip().lower()
            if local_backend not in VALID_LOCAL_BACKENDS:
                raise ValueError("本地后端必须是 ollama、LM Studio、llama.cpp 或 Unsloth")
            request_format = local_backend
        else:
            request_format = str(values.get("request_format") or "openai_chat").strip().lower()
            if request_format not in ONLINE_REQUEST_FORMATS:
                raise ValueError("不支持的在线请求格式")
            local_backend = ""
        with self.lock:
            providers = self.data.setdefault("providers", [])
            # 以 id 为主键：更新时就地切换 kind，避免同一 id 跨类别产生重复条目。
            existing = next(
                (item for item in providers if item.get("id") == model_id),
                None,
            )
            payload = {
                "id": model_id,
                "kind": kind,
                "name": str(values.get("name") or ("本地模型" if kind == "local" else "在线模型")).strip(),
                "base_url": str(values.get("base_url") or "").strip().rstrip("/"),
                "model": str(values.get("model") or "").strip(),
                "api_key": str(values.get("api_key") or "").strip(),
                "request_format": request_format,
            }
            raw_effort = str(values.get("reasoning_effort") or "auto").strip().lower()
            if raw_effort not in {"auto", "off", "low", "medium", "high"}:
                raise ValueError("思维强度必须是 auto / off / low / medium / high 之一")
            payload["reasoning_effort"] = raw_effort
            optional_fields = {
                "context_window": self._positive_context_size,
                "max_output_tokens": self._positive_context_size,
            }
            for field, parser in optional_fields.items():
                raw_value = values.get(field)
                if field == "context_window" and raw_value in (None, ""):
                    raw_value = values.get("context_size")
                if raw_value not in (None, ""):
                    payload[field] = parser(
                        raw_value,
                        "context_size" if field == "context_window" and "context_size" in values else field,
                    )
            raw_temperature = values.get("temperature")
            if raw_temperature not in (None, ""):
                if isinstance(raw_temperature, bool):
                    raise ValueError("temperature 必须是 0 到 2 之间的数字")
                try:
                    temperature = float(raw_temperature)
                except (TypeError, ValueError):
                    raise ValueError("temperature 必须是 0 到 2 之间的数字") from None
                if temperature < 0 or temperature > 2:
                    raise ValueError("temperature 必须是 0 到 2 之间的数字")
                payload["temperature"] = temperature
            clear_supports_images = False
            if "supports_images" in values:
                raw_supports_images = values.get("supports_images")
                if raw_supports_images is None:
                    clear_supports_images = True
                elif isinstance(raw_supports_images, bool):
                    payload["supports_images"] = raw_supports_images
                else:
                    raise ValueError("supports_images 必须是布尔值或 null")
            if kind == "local":
                payload["local_backend"] = local_backend
            else:
                payload.pop("local_backend", None)
            if not payload["base_url"] or not payload["model"]:
                raise ValueError("API/服务地址和模型名称不能为空")
            if existing:
                # 空 API Key 表示保留已有 Key（不覆盖、不清除）。
                if not payload["api_key"]:
                    payload["api_key"] = existing.get("api_key", "")
                for field in ("context_window", "max_output_tokens", "temperature"):
                    if field not in payload:
                        existing.pop(field, None)
                if clear_supports_images:
                    existing.pop("supports_images", None)
                existing.update(payload)
                stored = existing
            else:
                providers.append(payload)
                stored = payload
            if not self.data.get("default_model_key"):
                self.data["default_model_key"] = f"{kind}:{model_id}"
            self.save()
        explicit_images = stored.get("supports_images")
        result = {
            **stored,
            "model_key": f"{kind}:{model_id}",
            "api_key": "",
            "has_api_key": bool(stored["api_key"]),
            "is_default": (self.data.get("default_model_key") == f"{kind}:{model_id}"),
            "supports_images_explicit": (
                explicit_images if isinstance(explicit_images, bool) else None
            ),
            "supports_images": _infer_supports_images(stored),
            "context_window": _infer_context_window(stored),
            "context_window_source": _context_window_source(stored),
        }
        if stored.get("context_window"):
            result["context_size"] = stored["context_window"]
        return result

    def delete_model_profile(self, model_key: str) -> bool:
        with self.lock:
            key = self._normalize_model_key(model_key)
            kind, _, model_id = key.partition(":")
            providers = self.data.setdefault("providers", [])
            before = len(providers)
            self.data["providers"] = [
                item
                for item in providers
                if not (item.get("id") == model_id and item.get("kind") == kind)
            ]
            removed = len(self.data["providers"]) < before
            if self.data.get("default_model_key") == key:
                remaining = self.data.get("providers", [])
                self.data["default_model_key"] = (
                    f"{remaining[0].get('kind', 'online')}:{remaining[0].get('id')}" if remaining else ""
                )
            if self.data.get("provider_id") == model_id:
                self.data["provider_id"] = ""
            vision = self.data.get("vision")
            if isinstance(vision, dict) and vision.get("provider_model_key") == key:
                vision["provider_model_key"] = ""
            self.save()
            return removed

    def profile(self, selection: str = "") -> dict[str, Any]:
        """按 model_key 解析完整模型配置（含 api_key）。

        selection 可为 online:<id> / local:<id>；缺省时回退 default_model_key。
        """
        with self.lock:
            key = self._normalize_model_key(selection) or self.data.get("default_model_key") or ""
            if not key:
                raise ValueError("未选择模型配置")
            kind, _, model_id = key.partition(":")
            if kind not in VALID_MODEL_KINDS:
                raise ValueError(f"不支持的模型类型：{kind}")
            provider = next(
                (
                    item
                    for item in self.data.get("providers", [])
                    if item.get("id") == model_id and item.get("kind") == kind
                ),
                None,
            )
            if not provider:
                raise ValueError(f"找不到模型配置：{key}")
            result = {
                "kind": provider.get("kind", kind),
                **provider,
                "supports_images_explicit": (
                    provider.get("supports_images")
                    if isinstance(provider.get("supports_images"), bool)
                    else None
                ),
                "supports_images": _infer_supports_images(provider),
                "context_window": _infer_context_window(provider),
                "context_window_source": _context_window_source(provider),
            }
            if provider.get("context_window"):
                result["context_size"] = provider.get("context_window")
            return result

    def generation_options(self, selection: str = "") -> dict[str, Any]:
        with self.lock:
            key = self._normalize_model_key(selection) or str(self.data.get("default_model_key") or "")
            if not key:
                return {
                    "context_size": self._positive_context_size(
                        self.data.get("context_size", 8192), "context_size"
                    )
                }
            try:
                profile = self.profile(key)
            except ValueError:
                return {}
            options: dict[str, Any] = {}
            if profile.get("temperature") not in (None, ""):
                options["temperature"] = float(profile["temperature"])
            if profile.get("max_output_tokens") not in (None, ""):
                options["max_tokens"] = int(profile["max_output_tokens"])
            return options

    @staticmethod
    def _positive_context_size(value: Any, field: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{field} 必须是正整数")
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{field} 必须是正整数") from None
        if parsed <= 0 or (isinstance(value, float) and not value.is_integer()):
            raise ValueError(f"{field} 必须是正整数")
        return parsed

    # ---- Agent 管理 ----

    def public_agents(self) -> list[dict[str, Any]]:
        with self.lock:
            custom = [dict(agent) for agent in self.data.get("agents", [])]
            # 内置 Agent 默认全开启，且允许用户自定义；若用户已编辑过某个内置 Agent，
            # 其覆盖定义保存在 self.data['agents']（built_in=True），此时以覆盖版为准，
            # 不再追加默认内置定义，避免同一个 Agent 出现两次。
            overridden_ids = {
                str(agent.get("id") or "") for agent in custom if agent.get("built_in")
            }
            built_in = [
                dict(agent) for agent in built_in_agents()
                if agent.get("id") not in overridden_ids
            ]
            return custom + built_in

    def default_agent_id(self) -> str:
        with self.lock:
            agents = [*self.data.get("agents", []), *built_in_agents()]
            configured = str(self.data.get("default_agent_id") or "").strip()
            if configured and any(agent.get("id") == configured for agent in agents):
                return configured
            if agents:
                return str(agents[0].get("id") or "general")
            return "general"

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        agent_id = str(agent_id or "").strip()
        with self.lock:
            agent = next(
                (item for item in self.data.get("agents", []) if item.get("id") == agent_id),
                None,
            )
            if agent is None:
                agent = next(
                    (item for item in built_in_agents() if item.get("id") == agent_id),
                    None,
                )
            return dict(agent) if agent else None

    def upsert_agent(self, values: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(values.get("id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", agent_id):
            raise ValueError("Agent ID 只能包含字母、数字、下划线或连字符")
        # 内置 Agent 默认“全开启”，且允许用户自定义（如裁剪 tool_scope）。
        # 编辑仍保留 built_in 标记，使其不可删除；未内建的新 ID 视为自定义 Agent。
        is_built_in = agent_id in built_in_agent_ids()
        name = str(values.get("name") or "").strip()
        if not name:
            raise ValueError("Agent 名称不能为空")
        system_prompt = str(values.get("system_prompt") or "")[:12000]
        raw_skills = values.get("skill_ids") or []
        if not isinstance(raw_skills, list):
            raise ValueError("skill_ids 必须是数组")
        skill_ids = list(dict.fromkeys(str(item) for item in raw_skills if str(item).strip()))
        raw_scope = values.get("tool_scope")
        if raw_scope is not None and not isinstance(raw_scope, list):
            raise ValueError("tool_scope 必须是数组")
        tool_scope = (
            list(dict.fromkeys(str(item) for item in raw_scope if str(item).strip()))
            if isinstance(raw_scope, list) else []
        )
        payload = {
            "id": agent_id,
            "name": name[:80],
            "system_prompt": system_prompt,
            "skill_ids": skill_ids,
            "tool_scope": tool_scope,
        }
        if is_built_in:
            payload["built_in"] = True
        with self.lock:
            agents = self.data.setdefault("agents", [])
            index = next((i for i, item in enumerate(agents) if item.get("id") == agent_id), None)
            if index is None:
                agents.append(payload)
            else:
                agents[index] = payload
            if not self.get_agent(str(self.data.get("default_agent_id") or "")):
                self.data["default_agent_id"] = agents[0].get("id", "general") if agents else "general"
            self.save()
        return payload

    def delete_agent(self, agent_id: str) -> bool:
        agent_id = str(agent_id or "").strip()
        if agent_id in built_in_agent_ids():
            # 内置 Agent 不可删除：静默视为成功，避免前端报错。
            return False
        with self.lock:
            agents = self.data.setdefault("agents", [])
            before = len(agents)
            self.data["agents"] = [item for item in agents if item.get("id") != agent_id]
            if len(self.data["agents"]) == before:
                return False
            if self.data.get("default_agent_id") == agent_id:
                remaining = self.data["agents"]
                self.data["default_agent_id"] = (
                    next((item.get("id") for item in remaining if item.get("id") == "general"), None)
                    or (remaining[0].get("id") if remaining else "general")
                )
            self.save()
            return True
