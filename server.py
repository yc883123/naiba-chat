from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import ipaddress
import io
import re
import secrets
import shutil
import sqlite3
import socket
import sys
import tempfile
import threading
import time
import traceback
import zipfile
import urllib.parse
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from mcp_runtime import MCPRegistry
from model_runtime import ModelRuntime
from plan_runtime import CraftToolExecutor, PlanManager, ReadOnlyToolExecutor, resolve_mode_tools
from skill_runtime import (
    SkillAgent,
    SkillCatalog,
    TaskCancelled,
    ToolExecutor,
    _zip_has_skill_md,
    delete_skill,
    remove_skill_references,
)
from async_tasks import ActiveRunError, ConversationRunManager
from storage import ChatStorage
from updater import UpdateManager

import app_state
from app_state import (
    APP,
    APP_DIR,
    CONFIG_PATH,
    DATA_DIR,
    EXE_DIR,
    LOCK_PATH,
    PUBLIC_DIR,
    RESOURCE_DIR,
    STATUS_PATH,
)
from config_helpers import (
    VALID_MODEL_KINDS,
    _copy_legacy_data,
    _database_has_conversations,
    _infer_kind_for_request_format,
    _merge_data_tree,
    _sync_bundled_skills,
    migrate_legacy_data,
)
from agent_catalog import built_in_agent_ids, tool_catalog_entries
from image_utils import (
    STATIC_ASSET_VERSION,
    _MEDIA_MIME_FALLBACK,
    _clean_uploads_cache,
    _process_uploaded_image,
    _thumb_webp_path,
    _uploads_total_bytes,
    encode_image_for_model,
    path_within,
    validate_skills_dir,
)
from model_media import (
    _cache_debug_enabled,
    _detect_choice_groups,
    _image_intent,
    build_model_history,
    extract_attachments,
)
from config_store import ConfigStore


def _is_usable_lan_ipv4(address: str) -> bool:
    """Return whether an address can be presented as a LAN entry point."""
    try:
        parsed = ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError:
        return False
    # Do not offer loopback, APIPA, benchmarking, multicast, or unspecified
    # adapter addresses as phone-access URLs.
    return (
        parsed.is_private
        and not parsed.is_loopback
        and not parsed.is_link_local
        and parsed not in ipaddress.IPv4Network("198.18.0.0/15")
        and not parsed.is_multicast
        and not parsed.is_unspecified
    )


def get_lan_ip() -> str | None:
    """Find the LAN address selected by the current default IPv4 route.

    Connecting a UDP socket does not send traffic, but lets the OS choose the
    outbound interface. This makes the default-route address win over VPNs and
    virtual adapters. Hostname addresses are only a fallback for offline LANs.
    """
    candidates: list[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        candidates.append(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()
    try:
        candidates.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    return next((address for address in dict.fromkeys(candidates) if _is_usable_lan_ipv4(address)), None)


def network_access_status(host: str, port: int) -> dict[str, Any]:
    """Describe the active listener and the only LAN URL safe to present."""
    normalized_host = str(host or "").strip()
    local_url = f"http://127.0.0.1:{port}"
    try:
        bound_ip = ipaddress.ip_address(normalized_host)
    except ValueError:
        bound_ip = None

    lan_enabled = normalized_host in {"0.0.0.0", "::"}
    if isinstance(bound_ip, ipaddress.IPv4Address) and normalized_host != "0.0.0.0":
        lan_enabled = _is_usable_lan_ipv4(str(bound_ip))

    if not lan_enabled:
        return {
            "lan_enabled": False,
            "lan_url": "",
            "lan_reason": "当前服务仅允许本机访问。启用手机访问后重启即可使用。",
            "local_url": local_url,
        }

    lan_ip = str(bound_ip) if isinstance(bound_ip, ipaddress.IPv4Address) and _is_usable_lan_ipv4(str(bound_ip)) else get_lan_ip()
    if not lan_ip:
        return {
            "lan_enabled": False,
            "lan_url": "",
            "lan_reason": "未检测到可用于局域网访问的 IPv4 地址。请连接网络后重试。",
            "local_url": local_url,
        }
    return {
        "lan_enabled": True,
        "lan_url": f"http://{lan_ip}:{port}",
        "lan_reason": "",
        "local_url": local_url,
    }


class NaibaChatApp:
    def __init__(self):
        global DATA_DIR, STATUS_PATH, LOCK_PATH
        # 冻结版首次启动：从 EXE 相邻旧目录迁移配置与数据到 %LOCALAPPDATA%\NaibaChat。
        initial_data_dir = DATA_DIR.resolve()
        self.data_migration = migrate_legacy_data()
        self.config = ConfigStore(CONFIG_PATH)
        self.listener_host = str(self.config.data.get("host", "0.0.0.0"))
        # ConfigStore may reveal a custom data directory after the legacy
        # bootstrap migration has already run. Rebind all runtime globals and
        # carry over the bootstrap directory so the same process reads the
        # directory the user configured.
        configured_data_dir = self.config.resolve_data_dir().resolve()
        if configured_data_dir != initial_data_dir:
            try:
                if (
                    initial_data_dir.is_dir()
                    and not path_within(configured_data_dir, initial_data_dir)
                    and not path_within(initial_data_dir, configured_data_dir)
                ):
                    self.data_migration["data"] = (
                        _merge_data_tree(initial_data_dir, configured_data_dir)
                        or bool(self.data_migration.get("data"))
                    )
            except OSError as exc:
                print(f"Data directory switch migration failed: {exc}")
            DATA_DIR = configured_data_dir
            STATUS_PATH = DATA_DIR / "server.json"
            LOCK_PATH = DATA_DIR / "server.lock"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # 数据目录可能已由配置重定向；同步到 app_state，供拆出的子模块读取同一份值。
        app_state.DATA_DIR = DATA_DIR
        app_state.STATUS_PATH = STATUS_PATH
        app_state.LOCK_PATH = LOCK_PATH
        app_state.APP = self
        self.storage = ChatStorage(DATA_DIR / "chat.db")
        self.models = ModelRuntime()
        # Keep packaged Skills in the persistent managed directory as well.
        # A one-file executable extracts bundled assets to a temporary folder;
        # without this copy, replacing the executable can make a Skill that
        # existed in the previous build disappear from the user's catalog.
        bundled_skills = (RESOURCE_DIR / "skills").resolve()
        # 托管 Skills 目录位于数据目录内（DATA_DIR/skills），不再固定于 C 盘 APP_DIR。
        managed_skills = self.config.resolve_managed_skills_dir()
        # 启动幂等合并：把旧版「数据目录同级 skills」与旧版「APP_DIR/skills」中的
        # 用户自定义 Skill 合并进新托管目录（目标已有文件优先，不删除旧目录）。
        # 内置 Skills 由下方 _sync_bundled_skills 单独同步，这里跳过以免覆盖打包版本。
        for legacy_src in (
            (DATA_DIR.parent / "skills").resolve(),
            (APP_DIR / "skills").resolve(),
        ):
            if legacy_src.is_dir() and legacy_src != managed_skills:
                try:
                    _merge_data_tree(legacy_src, managed_skills)
                except OSError as exc:
                    print(f"Merging legacy Skills failed: {exc}")
        if bundled_skills.is_dir() and bundled_skills != managed_skills:
            try:
                _sync_bundled_skills(bundled_skills, managed_skills)
            except OSError as exc:
                print(f"Persisting bundled Skills failed: {exc}")

        skills_dirs: list[str] = []
        if bundled_skills.is_dir():
            skills_dirs.append(str(bundled_skills))
        if managed_skills.is_dir() and managed_skills != bundled_skills:
            skills_dirs.append(str(managed_skills))
        # 旧配置默认 `skills_dirs: ["skills"]` 解析为 APP_DIR/skills；重定向到新托管目录，
        # 保证旧配置/旧 Skill 不丢且不再写回 C 盘。
        legacy_managed = (APP_DIR / "skills").resolve()
        for raw in self.config.data.get("skills_dirs", []):
            try:
                resolved = self.config._resolve_dir(str(raw))
                if resolved == legacy_managed:
                    resolved = managed_skills
                validate_skills_dir(resolved)
                if str(resolved) not in skills_dirs:
                    skills_dirs.append(str(resolved))
            except ValueError:
                print(f"已忽略不安全的 Skill 目录：{raw}")
        self.catalog = SkillCatalog(
            [Path(path) for path in skills_dirs],
            base_dir=APP_DIR,
            hidden_ids=self.config.get_hidden_skill_ids(),
        )
        self.mcp = MCPRegistry(self.config.data.get("mcp_servers", []))
        self.executor = ToolExecutor(
            self.config.resolve_workspace_dir(),
            sys.executable,
            int(self.config.data.get("command_timeout", 120)),
            self.mcp,
            permission_mode=self.config.data.get("permission_mode", "confirm"),
            mcp_register=self.register_mcp_server,
        )
        self.plans = PlanManager(self)
        self.runs = ConversationRunManager(self)
        self.tasks = self.runs
        # Harness 级统一工具系统与 Job Registry
        from tool_registry import build_tool_registry
        from job_registry import JobRegistry

        self.tool_registry = build_tool_registry()
        self.tool_registry.bind_executor(self.executor)
        self.tool_registry.bind_mcp(self.mcp)
        self.jobs = JobRegistry(self)
        # MCP 生命周期：工具发现后注册到统一工具表，断开/注销时清理
        self.mcp.on_tools_discovered = self.tool_registry.register_mcp_tools
        self.mcp.on_tools_deregistered = self.tool_registry.deregister_mcp_tools
        self.mcp.register_tools_into(self.tool_registry)
        # MCP uses demand-driven lifecycle.  Configured services stay stopped
        # until a run explicitly needs them or calls an MCP tool.
        from subagent import (
            subagent_handler_factory,
            job_tool_handler_factory,
            run_subagent_agent,
        )
        self.jobs.agent_runner = lambda jid, spec, cancel, emit: run_subagent_agent(
            self, jid, spec, cancel, emit
        )
        self.tool_registry.register_system_handler("subagent", subagent_handler_factory(self))
        for _name, _handler in job_tool_handler_factory(self).items():
            self.tool_registry.register_system_handler(_name, _handler)
        self.tool_registry.register_system_handler("todo_write", self._todo_write_handler)
        self.tool_registry.register_system_handler("artifact_report", self._artifact_report_handler)
        self.tool_registry.register_system_handler("comfyui_prepare_workflow", self._comfyui_prepare_workflow_handler)
        self.tool_registry.register_system_handler("comfyui_batch", self._comfyui_batch_handler)
        from capability_runtime import CapabilityRuntime

        self.capabilities = CapabilityRuntime(self)
        for _name, _handler in self.capabilities.tool_handlers().items():
            self.tool_registry.register_system_handler(_name, _handler)
        # 视觉运行时（Phase 1-3）：注册 7 个视觉工具处理器。文本大脑看不到图时自动路由。
        from vision_runtime import VisionRouter

        self.vision = VisionRouter(self)
        for _vname, _vhandler in self.vision.tool_handlers().items():
            self.tool_registry.register_system_handler(_vname, _vhandler)
        # 联网搜索运行时（PLAN4 §联网搜索）：搜索开关开启且 provider 可用时才被加入 allowed_tools。
        from web_search_runtime import WebSearchRuntime

        self.web_search = WebSearchRuntime(self)
        self.tool_registry.register_system_handler("web_search", self._web_search_handler)
        # Storage marks in-flight runs as interrupted on startup. Deterministic
        # jobs that explicitly opted into resume are safely re-created from
        # their durable checkpoint and persisted parameters.
        self.jobs.resume_interrupted()
        self.updater = UpdateManager(APP_DIR, DATA_DIR)
        self.update_restart_callback = None
        # 后台自动连接所有已启用 MCP 服务；对启动时未连上的做周期重试，
        # 保证 MCP 工具在会话固化工具集之前就绪（否则新会话烘焙不到它们）。
        self._start_mcp_background()

    def stop(self) -> None:
        self.runs.shutdown()
        self.plans.shutdown()
        self.jobs.shutdown()
        self.mcp.stop()

    def _web_search_handler(self, args: dict[str, Any], _skills: Any, _ctx: Any) -> tuple[bool, str]:
        query = str(args.get("query") or args.get("q") or "")
        max_results = args.get("max_results")
        return self.web_search.search(query, int(max_results) if isinstance(max_results, (int, float)) else None)

    def _todo_write_handler(
        self,
        args: dict[str, Any],
        _skills: Any,
        run_context: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        run_id = str((run_context or {}).get("run_id") or (run_context or {}).get("job_id") or "")
        if not run_id:
            return False, "无法确定当前运行，不能保存任务清单"
        raw = (args or {}).get("todos")
        if not isinstance(raw, list) or len(raw) > 100:
            return False, "todos 必须是最多 100 项的数组"
        todos: list[dict[str, str]] = []
        active = 0
        for index, item in enumerate(raw, 1):
            if not isinstance(item, dict):
                return False, f"第 {index} 项不是对象"
            content = str(item.get("content") or "").strip()
            status = str(item.get("status") or "pending")
            if not content or status not in {"pending", "in_progress", "completed"}:
                return False, f"第 {index} 项缺少 content 或 status 无效"
            active += int(status == "in_progress")
            todos.append({"id": str(item.get("id") or index), "content": content[:1000], "status": status})
        if active > 1:
            return False, "同时最多只能有一个 in_progress 任务"
        self.storage.append_run_event(run_id, {"type": "todo_state", "todos": todos})
        return True, json.dumps({"saved": True, "todos": todos}, ensure_ascii=False)

    def _artifact_report_handler(
        self,
        args: dict[str, Any],
        _skills: Any,
        _run_context: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        paths = (args or {}).get("paths")
        if not isinstance(paths, list) or not paths or len(paths) > 200:
            return False, "paths 必须是 1 到 200 个文件路径"
        require_nonempty = bool((args or {}).get("require_nonempty", True))
        rows: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for raw in paths:
            path = Path(str(raw or "")).expanduser().resolve()
            try:
                if not path.is_file():
                    raise FileNotFoundError(path)
                size = path.stat().st_size
                if require_nonempty and size <= 0:
                    raise ValueError("文件为空")
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                rows.append({"path": str(path), "size": size, "sha256": digest.hexdigest()})
            except (OSError, ValueError) as exc:
                errors.append({"path": str(path), "error": str(exc)})
        result = {"status": "verified" if rows and not errors else ("partial" if rows else "failed"), "label": str((args or {}).get("label") or ""), "artifacts": rows, "errors": errors}
        return (not errors), json.dumps(result, ensure_ascii=False)

    def _comfyui_batch_handler(
        self,
        args: dict[str, Any],
        _skills: Any,
        run_context: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """Submit a batch of API-format workflows through the durable JobRegistry."""
        from job_registry import JobSpec

        ctx = run_context or {}
        conversation_id = str(ctx.get("conversation_id") or "")
        if not conversation_id:
            return False, "无法确定当前对话，不能创建 ComfyUI Job"
        values = args or {}
        workflows = values.get("workflows")
        if isinstance(workflows, str):
            try:
                workflows = json.loads(workflows)
            except json.JSONDecodeError as exc:
                return False, f"workflows 字符串不是合法 JSON：{exc}"
        workflow_paths = values.get("workflow_paths")
        if workflows is None and isinstance(workflow_paths, list) and workflow_paths:
            workflows = []
            for raw_path in workflow_paths:
                try:
                    workflow = self._load_comfyui_workflow(str(raw_path))
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    return False, f"工作流文件读取失败：{exc}"
                workflows.append(workflow)
        if workflows is None:
            one = values.get("workflow")
            shots = values.get("shots", 1)
            if not isinstance(one, dict):
                return False, "需要 workflows 数组，或提供 workflow 对象"
            try:
                count = max(1, min(int(shots), 200))
            except (TypeError, ValueError):
                return False, "shots 必须是正整数"
            workflows = [one for _ in range(count)]
        if not isinstance(workflows, list) or not workflows or not all(isinstance(item, dict) for item in workflows):
            return False, "workflows 必须是非空的 API 工作流对象数组"
        if len(workflows) > 200:
            return False, "单次最多提交 200 个工作流"
        try:
            workflows = [self._normalize_comfyui_runtime_workflow(item) for item in workflows]
        except ValueError as exc:
            return False, str(exc)
        owner = str(ctx.get("owner_session_id") or conversation_id)
        spec = JobSpec(
            kind="comfyui",
            conversation_id=conversation_id,
            params={
                "comfyui_url": str(values.get("comfyui_url") or "http://127.0.0.1:8188"),
                "workflows": workflows,
                "wait_timeout": max(1, min(int(values.get("timeout", 7200)), 86400)),
            },
            label="ComfyUI 批量生成",
            parent_job_id=str(ctx.get("run_id") or ctx.get("job_id") or "") or None,
            owner_session_id=owner,
            resumable=True,
        )
        job_id = self.jobs.start(spec, owner=owner)
        if bool(values.get("wait")):
            snapshot = self.jobs.wait(job_id, float(spec.params["wait_timeout"]), owner=owner)
            return True, json.dumps(snapshot or {"id": job_id}, ensure_ascii=False)
        return True, json.dumps({"job_id": job_id, "status": "queued", "total": len(workflows)}, ensure_ascii=False)

    @staticmethod
    def _normalize_comfyui_runtime_workflow(value: Any) -> dict[str, Any]:
        """Normalize an API workflow and replace invalid negative random seeds."""
        workflow = NaibaChatApp._normalize_comfyui_workflow(value)
        normalized = json.loads(json.dumps(workflow, ensure_ascii=False))
        for node in normalized.values():
            inputs = node.get("inputs") if isinstance(node, dict) else None
            if not isinstance(inputs, dict):
                continue
            for key in ("seed", "noise_seed"):
                raw = inputs.get(key)
                if isinstance(raw, (int, float)) and raw < 0:
                    inputs[key] = secrets.randbelow(2 ** 63)
        return normalized

    @staticmethod
    def _load_comfyui_workflow(raw_path: str) -> dict[str, Any]:
        path_text = str(raw_path or "").strip()
        if not path_text:
            raise ValueError("工作流路径为空")
        path = Path(path_text).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.suffix.lower() != ".json":
            raise ValueError("工作流文件必须是 .json")
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("工作流文件超过 20 MB")
        value = json.loads(path.read_text(encoding="utf-8"))
        return NaibaChatApp._normalize_comfyui_workflow(value)

    @staticmethod
    def _normalize_comfyui_workflow(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("工作流 JSON 必须是对象")
        # Accept the common {prompt: {...}} wrapper produced by API clients.
        candidate = value.get("prompt") if isinstance(value.get("prompt"), dict) else value
        # UI exports contain a nodes array and links; they are not POST /prompt payloads.
        if isinstance(candidate.get("nodes"), list) or isinstance(candidate.get("links"), list):
            raise ValueError("检测到 ComfyUI UI JSON，请先导出 API 格式工作流")
        if not candidate:
            raise ValueError("工作流为空")
        invalid = [key for key, node in candidate.items() if not isinstance(node, dict)]
        if invalid:
            raise ValueError(f"API 工作流节点值必须是对象：{', '.join(map(str, invalid[:5]))}")
        return candidate

    def _comfyui_prepare_workflow_handler(
        self,
        args: dict[str, Any],
        _skills: Any,
        _run_context: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        values = args or {}
        try:
            if isinstance(values.get("workflow"), dict):
                raw = values["workflow"]
            else:
                raw = json.loads(Path(str(values.get("path") or "")).expanduser().resolve().read_text(encoding="utf-8"))
            normalized = self._normalize_comfyui_workflow(raw)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            text = json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False)
            return False, text
        nodes = []
        for node_id, node in list(normalized.items())[:2000]:
            inputs = node.get("inputs") if isinstance(node, dict) else {}
            nodes.append({
                "id": str(node_id),
                "class_type": str(node.get("class_type") or ""),
                "input_count": len(inputs) if isinstance(inputs, dict) else 0,
            })
        result: dict[str, Any] = {
            "valid": True,
            "format": "api",
            "node_count": len(normalized),
            "nodes": nodes,
            "has_output_node": any(str(item.get("class_type") or "").lower().startswith(("save", "video", "preview")) for item in nodes),
        }
        if bool(values.get("include_workflow")):
            result["workflow"] = normalized
        return True, json.dumps(result, ensure_ascii=False)

    def register_mcp_server(self, values: dict[str, Any]) -> dict[str, Any]:
        config = self.config.upsert_mcp_server(values)
        return {"saved": True, "server": self.mcp.upsert(config)}

    def remove_mcp_server(self, server_id: str) -> dict[str, Any]:
        """Persistently delete a registered MCP server and its live connection."""
        if self.config.delete_mcp_server(server_id):
            self.mcp.remove(server_id)
        else:
            self.mcp.remove(server_id)  # 配置里可能已缺，仍尝试清理运行态
        return {"removed": True, "server_id": server_id}

    def pick_workspace_directory(self, initial: str = "") -> dict[str, Any]:
        """Open a Windows native folder picker; return an empty path if cancelled."""
        initial_path = str(initial or self.config.resolve_workspace_dir())
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(
                parent=root,
                initialdir=initial_path if Path(initial_path).is_dir() else str(EXE_DIR),
                title="选择 NaibaChat 工作区目录", mustexist=False,
            )
            root.destroy()
        except Exception as exc:
            raise RuntimeError(f"无法打开 Windows 原生目录选择器：{exc}") from exc
        selected = str(selected or "").strip()
        if not selected:
            return {"cancelled": True, "path": ""}
        resolved = self.config.resolve_workspace_dir(selected)
        self.config.ensure_workspace_writable(resolved)
        return {"cancelled": False, "path": selected, "resolved": str(resolved)}

    def browse_workspace(self, raw: str = "") -> dict[str, Any]:
        """Return a shallow, read-only project tree limited to the workspace root."""
        root = self.config.resolve_workspace_dir()
        self.config.ensure_workspace_writable(root)
        target = (Path(raw).expanduser() if str(raw or "").strip() else root).resolve()
        if not path_within(target, root):
            raise ValueError("浏览路径必须位于当前工作区内")
        if not target.exists() or not target.is_dir():
            raise ValueError("工作区目录不存在")
        entries = []
        try:
            children = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        except OSError as exc:
            raise ValueError(f"无法读取工作区目录：{exc}") from exc
        for child in children[:500]:
            if child.name in {".git", ".naiba_write_test"}:
                continue
            try:
                is_dir = child.is_dir()
                entries.append({
                    "name": child.name,
                    "path": str(child),
                    "kind": "directory" if is_dir else "file",
                    "size": None if is_dir else child.stat().st_size,
                })
            except OSError:
                continue
        return {"root": str(root), "path": str(target), "parent": str(target.parent) if target != root else "", "entries": entries}

    def _start_mcp_background(self) -> None:
        """应用启动后在后台连接所有已启用 MCP 服务，并保持到退出。

        在独立线程里做（不阻塞启动），并对启动时未连上的服务周期重试，
        让"naiba-chat 先启动、MCP/ComfyUI 稍后才上线"的场景也能自动补连。
        """
        def _worker() -> None:
            try:
                self.mcp.start()  # 置 _persistent=True 并启动所有连接
            except Exception as exc:  # 单个服务启动失败不应中断其他服务
                print(f"MCP 后台启动部分失败：{exc}")
            self.mcp.retry_unconnected_until_stopped()
        threading.Thread(target=_worker, name="naiba-mcp-background", daemon=True).start()

    def test_mcp_server(self, server_id: str) -> dict[str, Any]:
        """返回指定 MCP 的 stdio 状态，并对 ComfyUI 额外探测 HTTP 可达性。"""
        with self.mcp._lock:
            connection = self.mcp.connections.get(server_id)
        if not connection:
            raise ValueError(f"未注册的 MCP 服务：{server_id}")
        state = connection.state()
        if server_id == "comfy-mcp":
            address = (connection.env or {}).get("COMFYUI_SERVER_ADDRESS") or "http://127.0.0.1:8188"
            try:
                import urllib.request

                req = urllib.request.Request(f"{address.rstrip('/')}/", method="HEAD")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    state["comfyui_reachable"] = 200 <= resp.status < 500
            except Exception as exc:
                state["comfyui_reachable"] = False
                state["comfyui_error"] = str(exc)
        return state

    def reconnect_mcp_server(self, server_id: str) -> dict[str, Any]:
        """强制重连指定 MCP 服务：先停止再启动，返回最新状态。"""
        with self.mcp._lock:
            connection = self.mcp.connections.get(server_id)
        if not connection:
            raise ValueError(f"未注册的 MCP 服务：{server_id}")
        connection.stop()
        connection.start(timeout=20)
        return connection.state()

    def import_legacy_data(self, source: Path) -> dict[str, Any]:
        """从用户指定的旧数据目录导入 config.json、data/ 与 Skills（保留目标已存在数据）。"""
        source = Path(source)
        legacy_config = source / "config.json"
        legacy_data = source / "data"
        if not legacy_config.is_file() and not legacy_data.is_dir():
            raise ValueError("旧数据目录中没有 config.json 或 data/")
        report = _copy_legacy_data(source)
        if report["config"]:
            # 重新加载配置，使导入的模型/MCP 立即生效。
            self.config = ConfigStore(CONFIG_PATH)
        return report

    def bootstrap(self) -> dict[str, Any]:
        access = network_access_status(
            getattr(self, "listener_host", str(self.config.data.get("host", "0.0.0.0"))),
            int(self.config.data["port"]),
        )
        return {
            "settings": self.config.public(),
            "providers": self.config.public_providers(),
            "model_profiles": self.config.model_profiles(),
            "default_model_key": self.config.default_model_key(),
            "skills": self.catalog.scan(),
            "mcp_servers": self.mcp.states(),
            "agents": self.config.public_agents(),
            "default_agent_id": self.config.default_agent_id(),
            "workspaces": self.config.data.get("workspaces", []),
            "image_cache_bytes": _uploads_total_bytes(),
            **access,
            "lan_restart_required": str(self.config.data.get("host", "0.0.0.0")) != self.listener_host,
            "update": self.updater.status(),
            "data_location": {
                "is_frozen": bool(getattr(sys, "frozen", False)),
                "data_dir": str(DATA_DIR),
                "config_path": str(CONFIG_PATH),
                "exe_dir": str(EXE_DIR),
                "migration": self.data_migration,
            },
            "data_migration": self.migration_health(),
            "resolved_workspace_dir": str(self.config.resolve_workspace_dir()),
        }

    def list_skill_dirs(self) -> dict[str, Any]:
        configured = self.config.get_skills_dirs()
        managed = self.config.resolve_managed_skills_dir()
        legacy_managed = (APP_DIR / "skills").resolve()
        resolved = []
        for raw in configured:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = (APP_DIR / path).resolve()
            path = path.resolve()
            # 旧默认 `skills` 解析为 APP_DIR/skills，重定向到新的托管目录。
            if path == legacy_managed:
                path = managed
            resolved.append(str(path))
        # 托管目录始终作为唯一持久化入口，即便未显式配置也纳入返回。
        if str(managed) not in resolved:
            resolved.insert(0, str(managed))
        return {"configured": configured, "resolved": resolved}

    # ---- 数据与迁移（PLAN7 §数据与迁移） ----
    def migration_health(self) -> dict[str, Any]:
        """返回数据库版本、健康状态、已应用迁移与备份位置。"""
        integrity = self.storage.check_integrity()
        version = self.storage.get_user_version()
        return {
            "db_version": version,
            "data_dir": str(self.storage.data_dir),
            "configured_data_dir": str(self.config.resolve_data_dir()),
            "restart_required": self.config.resolve_data_dir() != self.storage.data_dir.resolve(),
            "healthy": bool(integrity.get("ok")),
            "integrity_details": integrity.get("details", []),
            "applied_versions": [version],
            "backup_location": str(DATA_DIR / "backups"),
            "resolved_skills_dirs": [str(path) for path in self.config.skills_dirs_resolved()],
        }

    def migration_backup(self) -> dict[str, Any]:
        """迁移前备份数据库及其 WAL/SHM  siblings。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = DATA_DIR / "backups" / f"migration-{stamp}"
        return self.storage.backup_for_migration(backup_dir)

    def migration_run(self) -> dict[str, Any]:
        """执行待应用的数据迁移；执行前禁止存在活动 Run。"""
        if self.storage.list_background_tasks(active_only=True):
            return {"ok": False, "error": "存在活动的 Run，请先等待其完成或取消后再执行迁移"}
        try:
            self.storage.apply_pending_migrations()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"迁移失败：{exc}", **self.migration_health()}
        return {"ok": True, **self.migration_health()}

    def migration_move_data(self, body: dict[str, Any]) -> dict[str, Any]:
        """Copy the current data directory to a new location and switch on restart."""
        if self.storage.list_background_tasks(active_only=True):
            return {"ok": False, "error": "存在活动任务，请先等待完成或取消后再迁移数据"}
        target = self.config.resolve_data_dir(str(body.get("data_dir") or "data"))
        self.config.ensure_data_dir_writable(target)
        source = self.storage.data_dir.resolve()
        if target == source:
            return {"ok": True, "message": "数据目录未改变", **self.migration_health()}
        if path_within(target, source) or path_within(source, target):
            return {"ok": False, "error": "目标数据目录不能是当前数据目录的父目录或子目录"}
        existing_db = target / "chat.db"
        if existing_db.exists() and _database_has_conversations(existing_db):
            return {"ok": False, "error": f"目标目录已有对话数据：{target}"}
        # Skill 托管目录跟随数据目录：源为当前实际运行数据目录（storage.data_dir）
        # 内的 skills（或旧版同级 skills、旧版 C 盘 APP_DIR/skills），目标为目标 data_dir 内 skills。
        old_managed = (source / "skills").resolve()
        old_sibling_managed = (source.parent / "skills").resolve()
        new_managed = (target / "skills").resolve()
        legacy_managed = (APP_DIR / "skills").resolve()
        try:
            with self.storage._connect() as db:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # 结构迁移：先确保当前库为最新 schema，复制后新目录即携带最新结构。
            self.storage.apply_pending_migrations()
            target.mkdir(parents=True, exist_ok=True)
            for item in source.iterdir():
                if item.name in {"server.lock", "backups"}:
                    continue
                if item.is_dir():
                    _merge_data_tree(item, target / item.name)
                elif not (target / item.name).exists():
                    shutil.copy2(item, target / item.name)
            # 搬迁托管 Skills 目录（旧 managed / 旧同级 managed / 旧 APP_DIR/skills → 新 managed）。
            old_managed_sources = []
            for candidate in (old_managed, old_sibling_managed, legacy_managed):
                if candidate.is_dir() and candidate not in old_managed_sources:
                    old_managed_sources.append(candidate)
            if old_managed_sources and new_managed != old_managed:
                new_managed.mkdir(parents=True, exist_ok=True)
                for old_src in old_managed_sources:
                    if old_src.is_dir():
                        _merge_data_tree(old_src, new_managed)
            # 改写 skills_dirs：把指向旧 managed / 旧同级 managed / 旧 APP_DIR/skills 的项改写为新绝对路径。
            old_refs = {str(old_managed), str(old_sibling_managed), str(legacy_managed)}
            with self.config.lock:
                dirs = self.config.data.setdefault("skills_dirs", [])
                rewritten = []
                for item in list(dirs):
                    resolved = str(self.config._resolve_dir(str(item)))
                    if resolved in old_refs:
                        if str(new_managed) not in rewritten:
                            rewritten.append(str(new_managed))
                    else:
                        rewritten.append(item)
                self.config.data["skills_dirs"] = rewritten
                # 若新托管目录不在 skills_dirs 中，追加以保证重启后被扫描/可安装。
                if str(new_managed) not in rewritten:
                    rewritten.append(str(new_managed))
                    self.config.data["skills_dirs"] = rewritten
                self.config.save()
            self.config.update_settings({"data_dir": str(target)})
        except (OSError, sqlite3.Error, ValueError) as exc:
            return {"ok": False, "error": f"迁移数据失败：{exc}"}
        return {
            "ok": True,
            "message": "数据与 Skills 已复制到新目录，请重启后生效",
            "target_data_dir": str(target),
            "target_skills_dir": str(new_managed),
            "restart_required": True,
            **self.migration_health(),
        }

    def migration_merge(self, body: dict[str, Any]) -> dict[str, Any]:
        """手动迁移：从用户指定的旧数据目录合并配置与对话（当前数据优先）。"""
        source = Path(str(body.get("source") or "").strip()).expanduser().resolve()
        if not source.is_dir():
            return {"ok": False, "error": "旧数据目录不存在"}
        try:
            report = self.import_legacy_data(source)
            self.storage.apply_pending_migrations()
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc), **self.migration_health()}
        return {"ok": True, "report": report, **self.migration_health()}


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "naiba-chat/1.0"

    def handle_one_request(self) -> None:
        """处理单个请求；任何未捕获异常都返回 500 JSON，而不是静默断连。

        默认 ``BaseHTTPRequestHandler.handle_one_request`` 在异常时只关闭连接、不写回包，
        浏览器会看到 ``net::ERR_EMPTY_RESPONSE``，难以定位。这里在异常时回一个 500 JSON。
        """
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            mname = "do_" + self.command
            if not hasattr(self, mname):
                self.send_error(HTTPStatus.NOT_IMPLEMENTED, "Unsupported method '%s'" % self.command)
                return
            method = getattr(self, mname)
            method()
            self.wfile.flush()
        except TimeoutError:
            self.log_error("Request timed out")
            self.close_connection = True
            return
        except socket.timeout:
            self.log_error("Request timed out")
            self.close_connection = True
            return
        except Exception as exc:  # noqa: BLE001
            self.log_error("Request handler error: %s", exc)
            self.close_connection = True
            try:
                # headers_sent 在首次 send_response 前不存在，用 getattr 兜底，否则会再抛
                # AttributeError 被吞掉、导致依然不回包（net::ERR_EMPTY_RESPONSE）。
                if not getattr(self.wfile, "closed", True) and not getattr(self, "headers_sent", False):
                    body = json.dumps({"error": f"服务器内部错误：{exc}"}, ensure_ascii=False).encode("utf-8")
                    self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
            except Exception:  # noqa: BLE001
                pass

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {self.client_address[0]} {format_string % args}")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            self._json({"status": "ok", "mcp": APP.mcp.states()})
            return
        if path.startswith("/api/") and not self._authorized(parsed):
            self._json({"error": "访问口令无效"}, HTTPStatus.UNAUTHORIZED)
            return
        if path == "/api/bootstrap":
            self._json(APP.bootstrap())
        elif path == "/api/update":
            self._json(APP.updater.status())
        elif path == "/api/agents":
            self._json({"agents": APP.config.public_agents(), "default_agent_id": APP.config.default_agent_id()})
        elif path == "/api/tasks":
            query = urllib.parse.parse_qs(parsed.query)
            conversation_id = query.get("conversation_id", [""])[0]
            active_only = query.get("active_only", ["0"])[0] == "1"
            self._json({"tasks": APP.tasks.list(conversation_id, active_only)})
        elif path == "/api/runs":
            query = urllib.parse.parse_qs(parsed.query)
            conversation_id = query.get("conversation_id", [""])[0]
            active_only = query.get("active_only", ["0"])[0] == "1"
            self._json({"runs": APP.runs.list(conversation_id, active_only)})
        elif path.startswith("/api/runs/") and path.endswith("/events"):
            run_id = path.split("/")[-2]
            query = urllib.parse.parse_qs(parsed.query)
            try:
                after = max(0, int(query.get("after", ["0"])[0]))
            except ValueError:
                self._json({"error": "after 必须是整数"}, HTTPStatus.BAD_REQUEST)
                return
            self._stream_run(run_id, after)
        elif path.startswith("/api/runs/"):
            run_id = path.rsplit("/", 1)[-1]
            run = APP.runs.get(run_id)
            self._json(run or {"error": "运行不存在"}, HTTPStatus.OK if run else HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/tasks/"):
            task_id = path.rsplit("/", 1)[-1]
            task = APP.storage.get_background_task(task_id)
            self._json(task or {"error": "任务不存在"}, HTTPStatus.OK if task else HTTPStatus.NOT_FOUND)
        # ---- Harness Job Registry 接口 ----
        elif path == "/api/jobs":
            query = urllib.parse.parse_qs(parsed.query)
            conversation_id = query.get("conversation_id", [""])[0]
            active_only = query.get("active_only", ["0"])[0] == "1"
            jobs = APP.jobs.list(owner=conversation_id or None, active_only=active_only)
            self._json({"jobs": jobs})
        elif path.startswith("/api/jobs/") and path.endswith("/events"):
            job_id = path.split("/")[-2]
            query = urllib.parse.parse_qs(parsed.query)
            conversation_id = query.get("conversation_id", [""])[0]
            try:
                after = max(0, int(query.get("after", ["0"])[0]))
            except ValueError:
                self._json({"error": "after 必须是整数"}, HTTPStatus.BAD_REQUEST)
                return
            self._json(APP.jobs.read(job_id, after, owner=conversation_id or None))
        elif path.startswith("/api/jobs/") and path.endswith("/status"):
            job_id = path.split("/")[-2]
            query = urllib.parse.parse_qs(parsed.query)
            conversation_id = query.get("conversation_id", [""])[0]
            job = APP.jobs.get(job_id, owner=conversation_id or None)
            self._json(job or {"error": "Job 不存在"}, HTTPStatus.OK if job else HTTPStatus.NOT_FOUND)
        elif path == "/api/tools":
            self._json({"tools": APP.tool_registry.schemas()})
        elif path == "/api/tool_catalog":
            self._json({"tools": tool_catalog_entries(APP.tool_registry.schemas())})
        elif path == "/api/mcp":
            self._json({"servers": APP.mcp.states()})
        elif path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            query = urllib.parse.parse_qs(parsed.query)
            conversation_id = query.get("conversation_id", [""])[0]
            job = APP.jobs.get(job_id, owner=conversation_id or None)
            self._json(job or {"error": "Job 不存在"}, HTTPStatus.OK if job else HTTPStatus.NOT_FOUND)
        elif path == "/api/conversations":
            query = urllib.parse.parse_qs(parsed.query)
            mode = query.get("mode", [None])[0]
            self._json({"conversations": APP.storage.list_conversations(mode=mode)})
        elif path == "/api/workspaces":
            self._json({"workspaces": APP.config.data.get("workspaces", [])})
        elif path == "/api/starter-prompts":
            self._json({"prompts": APP.config.get_starter_prompts()})
        elif path.startswith("/api/conversations/"):
            conversation_id = path.rsplit("/", 1)[-1]
            conversation = APP.storage.get_conversation(conversation_id)
            if conversation and conversation.get("messages"):
                last_message = conversation["messages"][-1]
                if last_message.get("role") == "assistant":
                    metadata = last_message.setdefault("metadata", {})
                    choice_groups = _detect_choice_groups(str(last_message.get("content") or ""))
                    metadata["choice_groups"] = choice_groups
                    metadata["choices"] = choice_groups[0]["choices"] if choice_groups else []
            self._json(conversation or {"error": "对话不存在"}, HTTPStatus.OK if conversation else HTTPStatus.NOT_FOUND)
        elif path == "/api/plans":
            query = urllib.parse.parse_qs(parsed.query)
            conversation_id = query.get("conversation_id", [""])[0]
            if not conversation_id:
                self._json({"error": "conversation_id 不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"plans": APP.storage.list_plans(conversation_id)})
        elif path.startswith("/api/plans/"):
            plan_id = path.rsplit("/", 1)[-1]
            plan = APP.storage.get_plan(plan_id)
            self._json(plan or {"error": "计划不存在"}, HTTPStatus.OK if plan else HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/providers/") and path.endswith("/secret"):
            provider_id = path.split("/")[-2]
            api_key = APP.config.provider_secret(provider_id)
            self._json(
                {"api_key": api_key} if api_key is not None else {"error": "供应商不存在"},
                HTTPStatus.OK if api_key is not None else HTTPStatus.NOT_FOUND,
            )
        elif path == "/api/model-profiles":
            query = urllib.parse.parse_qs(parsed.query)
            kind = query.get("kind", [None])[0]
            self._json({"profiles": APP.config.model_profiles(kind)})
        elif path == "/api/file":
            query = urllib.parse.parse_qs(parsed.query)
            self._serve_local_file(query.get("path", [""])[0])
        elif path == "/api/workspace/browse":
            try:
                query = urllib.parse.parse_qs(parsed.query)
                self._json(APP.browse_workspace(query.get("path", [""])[0]))
            except (OSError, ValueError, RuntimeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/install/dirs":
            self._json(APP.list_skill_dirs())
        elif path == "/api/mcp/status/light":
            self._json({"servers": APP.mcp.lightweight_status()})
        elif path == "/api/migration/health":
            self._json(APP.migration_health())
        elif path.startswith("/api/"):
            self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        else:
            self._serve_static(path)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._read_json(max_size=130 * 1024 * 1024)
        if body is None:
            return
        if path == "/api/auth":
            if self._is_local_request():
                self._json({"ok": True, "local": True})
                return
            valid = secrets.compare_digest(str(body.get("token") or ""), str(APP.config.data["access_token"]))
            self._json({"ok": valid}, HTTPStatus.OK if valid else HTTPStatus.UNAUTHORIZED)
            return
        if not self._authorized(parsed):
            self._json({"error": "访问口令无效"}, HTTPStatus.UNAUTHORIZED)
            return

        if path == "/api/conversations":
            title = str(body.get("title") or "新对话")
            provider_id = str(body.get("provider_id") or APP.config.data.get("provider_id") or "")
            model_key = str(body.get("model_key") or "")
            raw_agent_id = body.get("agent_id")
            agent_id = str(raw_agent_id or APP.config.default_agent_id())
            if raw_agent_id is not None and not APP.config.get_agent(agent_id):
                self._json({"error": "Agent 不存在"}, HTTPStatus.BAD_REQUEST)
                return
            interaction_mode = "craft"
            permission_mode = str(body.get("permission_mode") or "auto")
            web_search_enabled = body.get("web_search_enabled", False)
            deep_reasoning_enabled = body.get("deep_reasoning_enabled", False)
            reasoning_effort = body.get("reasoning_effort")
            workspace_dir = body.get("workspace_dir")
            workspace_group = body.get("workspace_group")
            # Only Plan is user-selectable; all other values mean ordinary mode.
            if permission_mode not in ("confirm", "auto", "full"):
                self._json({"error": "permission_mode 必须是 confirm / auto / full"}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(web_search_enabled, bool):
                self._json({"error": "web_search_enabled 必须是布尔值"}, HTTPStatus.BAD_REQUEST)
                return
            if not isinstance(deep_reasoning_enabled, bool):
                self._json({"error": "deep_reasoning_enabled 必须是布尔值"}, HTTPStatus.BAD_REQUEST)
                return
            if reasoning_effort is not None and str(reasoning_effort).lower() not in {"off", "low", "medium", "high", "auto"}:
                self._json({"error": "reasoning_effort 无效"}, HTTPStatus.BAD_REQUEST)
                return
            if workspace_dir is not None and not isinstance(workspace_dir, str):
                self._json({"error": "workspace_dir 必须是文本"}, HTTPStatus.BAD_REQUEST)
                return
            if workspace_group is not None and not isinstance(workspace_group, str):
                self._json({"error": "workspace_group 必须是文本"}, HTTPStatus.BAD_REQUEST)
                return
            if workspace_dir is not None and str(workspace_dir).strip():
                try:
                    resolved_workspace = APP.config.resolve_workspace_dir(str(workspace_dir).strip())
                    APP.config.ensure_workspace_writable(resolved_workspace)
                except (OSError, ValueError) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
            self._json(
                APP.storage.create_conversation(
                    title=title, provider_id=provider_id, agent_id=agent_id,
                    interaction_mode=interaction_mode, model_key=model_key,
                    permission_mode=permission_mode,
                    web_search_enabled=web_search_enabled,
                    deep_reasoning_enabled=deep_reasoning_enabled,
                    reasoning_effort=str(reasoning_effort or ("medium" if deep_reasoning_enabled else "auto")),
                    workspace_dir=str(workspace_dir or ""),
                    workspace_group=str(workspace_group or ""),
                ),
                HTTPStatus.CREATED,
            )
        elif path.startswith("/api/conversations/") and path.endswith("/branch"):
            conversation_id = path.split("/")[-2]
            message_id = str(body.get("message_id") or "")
            if not conversation_id or not message_id:
                self._json({"error": "conversation_id 和 message_id 不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                result = APP.storage.branch_conversation(conversation_id, message_id)
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json(result, HTTPStatus.CREATED)
        elif path.startswith("/api/conversations/") and path.endswith("/tools"):
            conversation_id = path.split("/")[-2]
            tools = body.get("tools") or []
            if not isinstance(tools, list) or not tools:
                self._json({"error": "tools 必须是非空数组"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                result = APP.runs.enable_conversation_tools(
                    conversation_id, [str(item) for item in tools if str(item).strip()]
                )
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json(result, HTTPStatus.OK)
        elif path.startswith("/api/conversations/") and path.endswith("/settings"):
            conversation_id = path.split("/")[-2]
            title = body.get("title")
            system_prompt = body.get("system_prompt")
            stream_enabled = body.get("stream_enabled")
            provider_id = body.get("provider_id")
            agent_id = body.get("agent_id")
            model_key = body.get("model_key")
            if title is not None and not isinstance(title, str):
                self._json({"error": "title 必须是文本"}, HTTPStatus.BAD_REQUEST)
                return
            if title is not None and len(title.strip()) > 120:
                self._json({"error": "对话名称不能超过 120 个字符"}, HTTPStatus.BAD_REQUEST)
                return
            if system_prompt is not None and not isinstance(system_prompt, str):
                self._json({"error": "system_prompt 必须是文本"}, HTTPStatus.BAD_REQUEST)
                return
            if stream_enabled is not None and not isinstance(stream_enabled, bool):
                self._json({"error": "stream_enabled 必须是布尔值"}, HTTPStatus.BAD_REQUEST)
                return
            if provider_id is not None and not isinstance(provider_id, str):
                self._json({"error": "provider_id 必须是文本"}, HTTPStatus.BAD_REQUEST)
                return
            if agent_id is not None and not isinstance(agent_id, str):
                self._json({"error": "agent_id 必须是文本"}, HTTPStatus.BAD_REQUEST)
                return
            if agent_id is not None and not APP.config.get_agent(str(agent_id)):
                self._json({"error": "Agent 不存在"}, HTTPStatus.BAD_REQUEST)
                return
            # 会话已固化启用工具集（会话启动时写死）后，不允许会话内切换 Agent，否则工具集
            # 会随之变化、破坏前缀缓存。需要切换 Agent 时请新开对话。
            if agent_id is not None:
                try:
                    conv_row = APP.storage.get_conversation(conversation_id)
                except Exception:  # noqa: BLE001 - 读取失败不应中断整个请求
                    conv_row = None
                current_agent_id = str((conv_row or {}).get("agent_id") or "")
                if agent_id != current_agent_id and (conv_row or {}).get("enabled_tool_ids"):
                    self._json(
                        {"error": "该会话已固化启用工具集，暂不支持会话内切换 Agent；请新开对话后再切换"},
                        HTTPStatus.CONFLICT,
                    )
                    return
            if model_key is not None and not isinstance(model_key, str):
                self._json({"error": "model_key 必须是文本"}, HTTPStatus.BAD_REQUEST)
                return
            interaction_mode = body.get("interaction_mode")
            if interaction_mode is not None:
                if not isinstance(interaction_mode, str):
                    self._json({"error": "interaction_mode 必须是文本"}, HTTPStatus.BAD_REQUEST)
                    return
                normalized_interaction_mode = interaction_mode.strip().lower()
                if normalized_interaction_mode not in {"plan", "craft", "ask"}:
                    self._json({"error": "interaction_mode 必须是 plan 或普通模式"}, HTTPStatus.BAD_REQUEST)
                    return
                interaction_mode = "craft"
            permission_mode = body.get("permission_mode")
            if permission_mode is not None:
                if not isinstance(permission_mode, str) or permission_mode not in ("confirm", "auto", "full"):
                    self._json({"error": "permission_mode 必须是 confirm / auto / full"}, HTTPStatus.BAD_REQUEST)
                    return
            web_search_enabled = body.get("web_search_enabled")
            if web_search_enabled is not None and not isinstance(web_search_enabled, bool):
                self._json({"error": "web_search_enabled 必须是布尔值"}, HTTPStatus.BAD_REQUEST)
                return
            deep_reasoning_enabled = body.get("deep_reasoning_enabled")
            reasoning_effort = body.get("reasoning_effort")
            workspace_dir = body.get("workspace_dir")
            workspace_group = body.get("workspace_group")
            if deep_reasoning_enabled is not None and not isinstance(deep_reasoning_enabled, bool):
                self._json({"error": "deep_reasoning_enabled 必须是布尔值"}, HTTPStatus.BAD_REQUEST)
                return
            if reasoning_effort is not None and str(reasoning_effort).lower() not in {"off", "low", "medium", "high", "auto"}:
                self._json({"error": "reasoning_effort 无效"}, HTTPStatus.BAD_REQUEST)
                return
            if workspace_dir is not None and not isinstance(workspace_dir, str):
                self._json({"error": "workspace_dir 必须是文本"}, HTTPStatus.BAD_REQUEST)
                return
            if workspace_group is not None and not isinstance(workspace_group, str):
                self._json({"error": "workspace_group 必须是文本"}, HTTPStatus.BAD_REQUEST)
                return
            lightweight_mode = body.get("lightweight_mode")
            if lightweight_mode is not None and not isinstance(lightweight_mode, bool):
                self._json({"error": "lightweight_mode 必须是布尔值"}, HTTPStatus.BAD_REQUEST)
                return
            lightweight_disabled_features = body.get("lightweight_disabled_features")
            if lightweight_disabled_features is not None and (
                not isinstance(lightweight_disabled_features, list)
                or not all(isinstance(item, str) for item in lightweight_disabled_features)
            ):
                self._json({"error": "lightweight_disabled_features 必须是字符串数组"}, HTTPStatus.BAD_REQUEST)
                return
            updated = APP.storage.update_conversation_settings(
                conversation_id,
                title=title,
                system_prompt=system_prompt,
                stream_enabled=stream_enabled,
                provider_id=provider_id,
                agent_id=agent_id,
                interaction_mode=interaction_mode,
                model_key=model_key,
                permission_mode=permission_mode,
                web_search_enabled=web_search_enabled,
                deep_reasoning_enabled=deep_reasoning_enabled,
                reasoning_effort=reasoning_effort,
                workspace_dir=workspace_dir,
                workspace_group=workspace_group,
                lightweight_mode=lightweight_mode,
                lightweight_disabled_features=lightweight_disabled_features,
            )
            self._json(updated or {"error": "对话不存在"}, HTTPStatus.OK if updated else HTTPStatus.NOT_FOUND)
        elif path == "/api/workspaces":
            name = str(body.get("name") or "").strip()
            raw_dir = str(body.get("dir") or "").strip()
            if not name:
                self._json({"error": "工作区名称不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            if not raw_dir:
                self._json({"error": "工作区目录不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                resolved_dir = APP.config.resolve_workspace_dir(raw_dir)
                APP.config.ensure_workspace_writable(resolved_dir)
            except (OSError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            workspaces = list(APP.config.data.get("workspaces", []))
            if any(str(ws.get("name") or "").strip() == name for ws in workspaces):
                self._json({"error": "工作区名称已存在"}, HTTPStatus.BAD_REQUEST)
                return
            workspaces.append({"name": name, "dir": raw_dir})
            try:
                APP.config.update_settings({"workspaces": workspaces})
            except (ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"workspaces": APP.config.data.get("workspaces", [])}, HTTPStatus.OK)
        elif path == "/api/workspaces/delete":
            name = str(body.get("name") or "").strip()
            raw_dir = str(body.get("dir") or "").strip()
            workspaces = list(APP.config.data.get("workspaces", []))
            new_list = [
                ws for ws in workspaces
                if not (
                    str(ws.get("name") or "").strip() == name
                    or (raw_dir and str(ws.get("dir") or "").strip() == raw_dir)
                )
            ]
            removed_names = [
                str(ws.get("name") or "").strip()
                for ws in workspaces
                if ws not in new_list
            ]
            if not removed_names:
                # 注册表中没有匹配项：若调用方仍给了名称，允许归档该名称下的对话（处理遗留分组）。
                if not name:
                    self._json({"error": "工作区不存在"}, HTTPStatus.NOT_FOUND)
                    return
                removed_names = [name]
            APP.config.update_settings({"workspaces": new_list})
            # 已删除工作区下的对话归档到「未分组」，避免残留分组。
            for ws_name in removed_names:
                if ws_name:
                    APP.storage.clear_workspace_group(ws_name)
            self._json({"workspaces": APP.config.data.get("workspaces", [])}, HTTPStatus.OK)
        elif path == "/api/agents":
            try:
                self._json(APP.config.upsert_agent(body))
            except (ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/providers":
            try:
                self._json(APP.config.upsert_provider(body))
            except Exception as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/providers/test":
            self._test_provider(body)
        elif path == "/api/providers/models":
            self._provider_models(body)
        elif path == "/api/providers/unload":
            self._unload_provider(body)
        elif path == "/api/models/unload":
            self._unload_provider(body)
        elif path == "/api/model-profiles/test":
            self._test_provider(body)
        elif path == "/api/model-profiles/models":
            self._provider_models(body)
        elif path == "/api/model-profiles/unload":
            self._unload_provider(body)
        elif path == "/api/model-profiles":
            try:
                self._json(APP.config.upsert_model_profile(body))
            except Exception as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/imaging/clean":
            try:
                result = _clean_uploads_cache()
            except OSError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            else:
                self._json(result)
        elif path == "/api/settings":
            try:
                if "data_dir" in body:
                    requested = APP.config.resolve_data_dir(str(body.get("data_dir") or "data"))
                    if requested != DATA_DIR.resolve() and APP.storage.list_background_tasks(active_only=True):
                        self._json({"error": "存在活动任务，请先等待完成或取消后再切换数据目录"}, HTTPStatus.CONFLICT)
                        return
                settings = APP.config.update_settings(body)
                model_key = str(body.get("model_key") or body.get("default_model_key") or "").strip()
                if model_key:
                    APP.config.set_default_model_key(model_key)
                    settings = APP.config.public()
                APP.executor.command_timeout = int(APP.config.data.get("command_timeout", 120))
                APP.executor.set_permission_mode(str(APP.config.data.get("permission_mode", "confirm")))
                # 工作区变更：仅影响新任务；已运行后台任务继续使用其启动时的快照路径。
                if "workspace_dir" in body:
                    APP.executor.workspace = APP.config.resolve_workspace_dir()
                self._json(
                    {
                        "settings": settings,
                        "default_model_key": APP.config.default_model_key(),
                        "resolved_workspace_dir": str(APP.config.resolve_workspace_dir()),
                        "resolved_data_dir": str(APP.config.resolve_data_dir()),
                        "image_cache_bytes": _uploads_total_bytes(),
                        "restart_required": (
                            ("data_dir" in body and APP.config.resolve_data_dir() != DATA_DIR.resolve())
                            or ("host" in body and str(APP.config.data.get("host")) != APP.listener_host)
                        ),
                        "network_access": network_access_status(
                            APP.listener_host,
                            int(APP.config.data.get("port", 8765)),
                        ),
                    }
                )
            except (OSError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/settings/import-legacy":
            # 从用户指定的旧数据目录导入 config.json 与 data/（保留目标已存在数据）。
            try:
                source = Path(str(body.get("source") or "").strip()).expanduser().resolve()
                if not source.is_dir():
                    self._json({"error": "旧数据目录不存在"}, HTTPStatus.BAD_REQUEST)
                    return
                imported = APP.import_legacy_data(source)
                self._json({"ok": True, **imported})
            except (OSError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/mcp/test":
            try:
                server_id = str(body.get("server_id") or "").strip()
                result = APP.test_mcp_server(server_id)
                self._json(result)
            except (OSError, ValueError, RuntimeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/workspace/pick":
            try:
                self._json(APP.pick_workspace_directory(str(body.get("initial") or "")))
            except (OSError, ValueError, RuntimeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/mcp/register":
            try:
                self._json(APP.register_mcp_server(body))
            except (OSError, ValueError, RuntimeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/mcp/remove":
            try:
                server_id = str(body.get("server_id") or "").strip()
                if not server_id:
                    raise ValueError("server_id 不能为空")
                self._json(APP.remove_mcp_server(server_id))
            except (OSError, ValueError, RuntimeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/mcp/reconnect":
            try:
                server_id = str(body.get("server_id") or "").strip()
                result = APP.reconnect_mcp_server(server_id)
                self._json(result)
            except (OSError, ValueError, RuntimeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/vision/test":
            try:
                selected = body.get("provider_model_key")
                selected_key = str(selected) if selected is not None else None
                probe = str(body.get("probe") or "vision").strip().lower()
                if probe == "text":
                    self._json(APP.vision.probe_text(selected_key))
                elif probe == "vision":
                    self._json(APP.vision.probe(selected_key))
                else:
                    self._json({"ok": False, "reason": "Unsupported vision probe"}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "reason": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/search/test":
            try:
                self._json(APP.web_search.probe(body))
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "reason": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/update/check":
            self._json(APP.updater.start_check(force=True))
        elif path == "/api/update/install":
            try:
                target_tag = None
                if isinstance(body, dict):
                    target_tag = body.get("tag")
                self._json(APP.updater.start_install(target_tag=target_tag, on_ready=APP.update_restart_callback))
            except RuntimeError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/uploads":
            self._upload(body)
        elif path == "/api/install/dir":
            self._install_dir(body)
        elif path == "/api/install/dir/remove":
            self._remove_install_dir(body)
        elif path == "/api/skills/install":
            self._install_skill(body)
        elif path == "/api/skills/install_folder":
            self._install_folder(body)
        elif path == "/api/skills/scan":
            self._json({
                "skills": APP.catalog.scan(),
                "configured": APP.config.get_skills_dirs(),
                "hidden_skills": self._hidden_skill_entries(),
            })
        elif path == "/api/skills/delete":
            self._delete_skill(body)
        elif path == "/api/skills/unhide":
            self._unhide_skill(body)
        elif path == "/api/starter-prompts":
            title = str(body.get("title") or "").strip()
            text = str(body.get("text") or "").strip()
            if not text:
                self._json({"error": "指令内容不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                prompts = APP.config.add_starter_prompt(title, text)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"prompts": prompts}, HTTPStatus.CREATED)
        elif path.startswith("/api/starter-prompts/"):
            index = path.rsplit("/", 1)[-1]
            try:
                idx = int(index)
                if idx < 0:
                    raise ValueError
            except ValueError:
                self._json({"error": "无效的指令序号"}, HTTPStatus.BAD_REQUEST)
                return
            title = str(body.get("title") or "").strip()
            text = str(body.get("text") or "").strip()
            if not text:
                self._json({"error": "指令内容不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                prompts = APP.config.update_starter_prompt(idx, title, text)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"prompts": prompts})
        elif path == "/api/migration/backup":
            self._json(APP.migration_backup())
        elif path == "/api/migration/run":
            self._json(APP.migration_run())
        elif path == "/api/migration/move-data":
            self._json(APP.migration_move_data(body))
        elif path == "/api/migration/merge":
            self._json(APP.migration_merge(body))
        elif path == "/api/chat/cancel":
            run_id = str(body.get("run_id") or "").strip()
            conversation_id = str(body.get("conversation_id") or "").strip()
            if not run_id and not conversation_id:
                self._json({"error": "run_id 和 conversation_id 不能同时为空"}, HTTPStatus.BAD_REQUEST)
            else:
                if not run_id:
                    active = next(
                        (
                            item for item in APP.runs.list(conversation_id, active_only=True)
                            if str(item.get("kind") or "") in {"chat", "plan_execute"}
                        ),
                        None,
                    )
                    run_id = str((active or {}).get("id") or "")
                run = APP.runs.get(run_id) if run_id else None
                if run and conversation_id and str(run.get("conversation_id") or "") != conversation_id:
                    self._json({"error": "运行不属于当前对话"}, HTTPStatus.BAD_REQUEST)
                    return
                cancelled = APP.runs.cancel(run_id) if run_id else None
                self._json(
                    {"cancelled": bool(cancelled), "run": cancelled},
                    HTTPStatus.OK if cancelled else HTTPStatus.NOT_FOUND,
                )
        elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
            job_id = path.split("/")[-2]
            if not job_id:
                self._json({"error": "job_id 不能为空"}, HTTPStatus.BAD_REQUEST)
            else:
                reason = str(body.get("reason") or "") or None
                job = APP.jobs.cancel(job_id, owner=body.get("conversation_id") or None, reason=reason)
                self._json(
                    {"cancelled": bool(job), "job": job},
                    HTTPStatus.OK if job else HTTPStatus.NOT_FOUND,
                )
        elif path.startswith("/api/jobs/") and path.endswith("/resume"):
            job_id = path.split("/")[-2]
            if not job_id:
                self._json({"error": "job_id 不能为空"}, HTTPStatus.BAD_REQUEST)
            else:
                new_id = APP.jobs.resume(job_id, owner=body.get("conversation_id") or None)
                if new_id:
                    self._json({"resumed": True, "job_id": new_id}, HTTPStatus.OK)
                else:
                    self._json({"error": "Job 不可恢复或不存在"}, HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/jobs/") and path.endswith("/retry"):
            job_id = path.split("/")[-2]
            if not job_id:
                self._json({"error": "job_id 不能为空"}, HTTPStatus.BAD_REQUEST)
            else:
                try:
                    new_id = APP.jobs.retry(job_id, owner=body.get("conversation_id") or None)
                except ValueError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                else:
                    if new_id:
                        self._json({"retried": True, "job_id": new_id}, HTTPStatus.OK)
                    else:
                        self._json({"error": "Job 不存在或无权访问"}, HTTPStatus.NOT_FOUND)
        elif path == "/api/chat/interject":
            try:
                self._json(APP.runs.interject(body), HTTPStatus.ACCEPTED)
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/chat/interject/guide":
            try:
                self._json(APP.runs.guide_interjection(body))
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/chat/interject/edit":
            try:
                self._json(APP.runs.edit_interjection(body))
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/chat/interject/delete":
            try:
                self._json(APP.runs.delete_interjection(body))
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/chat":
            self._chat(body)
        elif path == "/api/tasks":
            try:
                self._json(APP.tasks.submit(body), HTTPStatus.ACCEPTED)
            except ActiveRunError as exc:
                self._json(
                    {"error": str(exc), "active_run_id": exc.run_id},
                    HTTPStatus.CONFLICT,
                )
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except (ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/tool/confirm":
            self._confirm_tool(body)
        elif path == "/api/tool/reject":
            self._reject_tool(body)
        elif path.startswith("/api/plans/") and path.endswith("/execute"):
            plan_id = path.split("/")[-2]
            try:
                self._json(
                    APP.runs.submit_plan(
                        plan_id,
                        web_search_enabled=bool(body.get("web_search_enabled", False)),
                    ),
                    HTTPStatus.ACCEPTED,
                )
            except ActiveRunError as exc:
                self._json(
                    {"error": str(exc), "active_run_id": exc.run_id},
                    HTTPStatus.CONFLICT,
                )
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path.startswith("/api/plans/") and path.endswith("/cancel"):
            plan_id = path.split("/")[-2]
            try:
                self._json(APP.plans.cancel(plan_id))
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path.startswith("/api/plans/") and path.endswith("/keep-planning"):
            plan_id = path.split("/")[-2]
            try:
                self._json(APP.plans.keep_planning(plan_id))
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/messages/edit":
            self._edit_message(body)
        else:
            self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._read_json()
        if body is None:
            return
        if not self._authorized(parsed):
            self._json({"error": "访问口令无效"}, HTTPStatus.UNAUTHORIZED)
            return
        if path.startswith("/api/plans/"):
            plan_id = path.rsplit("/", 1)[-1]
            try:
                self._json(
                    APP.plans.edit_plan(
                        plan_id,
                        title=body.get("title"),
                        content=body.get("content"),
                    )
                )
            except LookupError as exc:
                self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if not self._authorized(parsed):
            self._json({"error": "访问口令无效"}, HTTPStatus.UNAUTHORIZED)
            return
        path = parsed.path
        if path == "/api/tasks/clear":
            self._json({"deleted": APP.storage.clear_terminal_background_tasks()})
        elif path.startswith("/api/starter-prompts/"):
            index = path.rsplit("/", 1)[-1]
            try:
                idx = int(index)
                if idx < 0:
                    raise ValueError
            except ValueError:
                self._json({"error": "无效的指令序号"}, HTTPStatus.BAD_REQUEST)
                return
            self._json({"prompts": APP.config.remove_starter_prompt(idx)})
        elif path.startswith("/api/conversations/") and path.endswith("/messages"):
            conversation_id = path.split("/")[-2]
            if APP.storage.list_background_tasks(conversation_id, active_only=True):
                self._json({"error": "当前对话仍有运行中的任务，无法清空"}, HTTPStatus.CONFLICT)
                return
            self._json({"deleted": APP.storage.clear_conversation_messages(conversation_id)})
        elif path.startswith("/api/conversations/"):
            deleted = APP.storage.delete_conversation(path.rsplit("/", 1)[-1])
            self._json({"ok": deleted}, HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/tasks/") and path.endswith("/cancel"):
            task_id = path.split("/")[-2]
            task = APP.tasks.cancel(task_id)
            self._json(task or {"error": "任务不存在"}, HTTPStatus.OK if task else HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/agents/"):
            deleted = APP.config.delete_agent(path.rsplit("/", 1)[-1])
            self._json({"ok": deleted}, HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/model-profiles/"):
            deleted = APP.config.delete_model_profile(path.rsplit("/", 1)[-1])
            self._json({"ok": deleted}, HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/providers/"):
            deleted = APP.config.delete_provider(path.rsplit("/", 1)[-1])
            self._json({"ok": deleted}, HTTPStatus.OK if deleted else HTTPStatus.NOT_FOUND)
        elif path.startswith("/api/skills/"):
            skill_id = path.rsplit("/", 1)[-1]
            self._delete_skill_by_id(skill_id)
        else:
            self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def _authorized(self, parsed: urllib.parse.ParseResult) -> bool:
        # Desktop/localhost requests do not need the LAN access token.
        if self._is_local_request():
            return True
        expected = str(APP.config.data["access_token"])
        header = self.headers.get("Authorization", "")
        provided = header[7:] if header.startswith("Bearer ") else ""
        if not provided:
            provided = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
        return bool(provided) and secrets.compare_digest(provided, expected)

    def _is_local_request(self) -> bool:
        return self.client_address[0] in {"127.0.0.1", "::1", "localhost"}

    def _read_json(self, max_size: int = 2 * 1024 * 1024) -> dict[str, Any] | None:
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > max_size:
                self._json({"error": "请求内容过大"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return None
            payload = self.rfile.read(size) if size else b"{}"
            value = json.loads(payload.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("JSON 必须是对象")
            return value
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json({"error": f"无效 JSON：{exc}"}, HTTPStatus.BAD_REQUEST)
            return None

    def _json(self, value: Any, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, requested_path: str) -> None:
        relative = "index.html" if requested_path in {"", "/"} else requested_path.lstrip("/")
        path = (PUBLIC_DIR / relative).resolve()
        try:
            path.relative_to(PUBLIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not path.is_file():
            path = PUBLIC_DIR / "index.html"
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        if path.name == "index.html":
            data = data.replace(b"__ASSET_VERSION__", STATIC_ASSET_VERSION.encode("ascii"))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _serve_local_file(self, source: str) -> None:
        if source.startswith("http://127.0.0.1:8188/") or source.startswith("http://localhost:8188/"):
            import urllib.request

            try:
                with urllib.request.urlopen(source, timeout=60) as response:
                    data = response.read()
                    content_type = response.headers.get_content_type()
            except Exception as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
                return
        else:
            path = Path(source).expanduser().resolve()
            # Attachments created before a data-directory migration contain an
            # absolute path from the old install. Resolve those records by
            # filename inside the current uploads directory.
            current_data_root = DATA_DIR.resolve()
            current_uploads = (current_data_root / "uploads").resolve()
            if not path.is_file() and path.name:
                migrated_path = current_uploads / path.name
                if migrated_path.is_file():
                    path = migrated_path
            allowed_roots = [
                APP.config.resolve_workspace_dir(),
                current_data_root,
            ]
            if not any(path_within(path, root) for root in allowed_roots):
                self._json({"error": "文件不在允许访问的目录中"}, HTTPStatus.FORBIDDEN)
                return
            if not path.is_file():
                self._json({"error": "文件不存在"}, HTTPStatus.NOT_FOUND)
                return
            data = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if content_type == "application/octet-stream":
                content_type = _MEDIA_MIME_FALLBACK.get(path.suffix.lower(), content_type)
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type,
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _upload(self, body: dict[str, Any]) -> None:
        name = Path(str(body.get("name") or "upload.bin")).name
        encoded = str(body.get("data") or "")
        if "," in encoded and encoded.startswith("data:"):
            encoded = encoded.split(",", 1)[1]
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            self._json({"error": "文件内容不是有效 Base64"}, HTTPStatus.BAD_REQUEST)
            return
        if len(data) > 80 * 1024 * 1024:
            self._json({"error": "单个文件不能超过 80 MB"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        safe_name = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]", "_", name)
        target_dir = (DATA_DIR / "uploads").resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"naiba_chat_{int(time.time())}_{secrets.token_hex(3)}_{safe_name}"
        imaging = dict(APP.config.data.get("imaging") or {}) if getattr(APP, "config", None) else {}
        main_bytes, thumb_name, thumb_bytes = _process_uploaded_image(data, target.name, imaging)
        target.write_bytes(main_bytes)
        thumb_path = ""
        if thumb_name and thumb_bytes:
            thumb_file = target_dir / thumb_name
            thumb_file.write_bytes(thumb_bytes)
            thumb_path = str(thumb_file)
        self._json({
            "name": target.name,
            "path": str(target),
            "size": len(main_bytes),
            "thumb_path": thumb_path,
        })

    def _install_dir(self, body: dict[str, Any]) -> None:
        raw = str(body.get("dir") or "").strip()
        try:
            resolved = APP.config.add_skills_dir(raw)
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        APP.catalog.add_directory(raw)
        skills = APP.catalog.scan()
        self._json({"dir": str(resolved), "configured": APP.config.get_skills_dirs(), "skills": skills})

    def _remove_install_dir(self, body: dict[str, Any]) -> None:
        raw = str(body.get("dir") or "").strip()
        if not raw:
            self._json({"error": "目录路径不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        configured = APP.config.remove_skills_dir(raw)
        APP.catalog.remove_directory(raw)
        self._json({"configured": configured, "skills": APP.catalog.scan()})

    def _resolve_skill_dest(self, body: dict[str, Any]) -> tuple[str, Path] | None:
        """解析 Skill 安装目标目录：未指定时默认安装到托管 Skills 目录。

        托管 Skills 目录位于数据目录内（DATA_DIR/skills）；旧 ``APP_DIR/skills``
        与旧数据目录同级 skills 会重定向/合并到托管目录，保证旧配置不丢且默认落点离开 C 盘。
        """
        configured = APP.config.get_skills_dirs()
        managed = APP.config.resolve_managed_skills_dir()
        if str(body.get("dir") or "").strip():
            dest_raw = str(body.get("dir") or "").strip()
        elif configured and configured[0] != "skills":
            dest_raw = configured[0]
        else:
            dest_raw = str(managed)
        dest = APP.config._resolve_dir(dest_raw)
        try:
            validate_skills_dir(dest)
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return None
        allowed = {APP.config._resolve_dir(item) for item in configured}
        allowed.add(APP.config._resolve_dir("skills"))
        allowed.add(managed)
        allowed.add((APP_DIR / "skills").resolve())
        if dest not in allowed:
            self._json(
                {"error": "只能安装到已添加的 Skill 扫描目录，请先在上方添加该目录"},
                HTTPStatus.FORBIDDEN,
            )
            return None
        return dest_raw, dest

    def _finish_install(self, dest_raw: str, dest: Path, extra: dict[str, Any] | None = None) -> None:
        APP.config.add_skills_dir(dest_raw)
        APP.catalog.add_directory(dest_raw)
        # “导入即启用”：若本次安装目录里的 Skill 命中过 hidden_skill_ids（此前被隐藏/删除），
        # 自动取消隐藏，避免 scan() 静默过滤导致 UI 导入成功却不显示。
        installed_ids = {str(item.get("id") or "") for item in SkillCatalog([dest]).scan()}
        hidden_ids = set(APP.config.get_hidden_skill_ids())
        unhidden: list[str] = [sid for sid in installed_ids if sid in hidden_ids]
        for sid in unhidden:
            APP.config.unhide_skill(sid)
            APP.catalog.hidden_ids.discard(sid)
        payload: dict[str, Any] = {
            "dir": str(dest),
            "configured": APP.config.get_skills_dirs(),
            "skills": APP.catalog.scan(),
            "hidden_skills": self._hidden_skill_entries(),
            "unhidden": unhidden,
        }
        if extra:
            payload.update(extra)
        self._json(payload)

    def _install_skill(self, body: dict[str, Any]) -> None:
        name = Path(str(body.get("name") or "skill.zip")).name
        encoded = str(body.get("data") or "")
        if "," in encoded and encoded.startswith("data:"):
            encoded = encoded.split(",", 1)[1]
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError:
            self._json({"error": "文件内容不是有效 Base64"}, HTTPStatus.BAD_REQUEST)
            return
        if len(data) > 80 * 1024 * 1024:
            self._json({"error": "Skill 压缩包不能超过 80 MB"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        resolved = self._resolve_skill_dest(body)
        if resolved is None:
            return
        dest_raw, dest = resolved
        dest.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(prefix="naiba_skill_"))
        try:
            zip_path = tmp_dir / name
            zip_path.write_bytes(data)
            with zipfile.ZipFile(zip_path) as archive:
                bad = archive.testzip()
                if bad is not None:
                    self._json({"error": f"压缩包损坏：{bad}"}, HTTPStatus.BAD_REQUEST)
                    return
                if not _zip_has_skill_md(archive):
                    self._json(
                        {"error": "压缩包必须包含 SKILL.md（位于压缩包顶层或其下一级目录）"},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                members = archive.infolist()
                if len(members) > 5000:
                    self._json({"error": "压缩包内文件数量过多（超过 5000）"}, HTTPStatus.BAD_REQUEST)
                    return
                if sum(member.file_size for member in members) > 500 * 1024 * 1024:
                    self._json({"error": "压缩包解压后体积过大（超过 500 MB）"}, HTTPStatus.BAD_REQUEST)
                    return
                for member in members:
                    target = (dest / member.filename).resolve()
                    if target != dest and not path_within(target, dest):
                        self._json(
                            {"error": f"压缩包包含越界路径：{member.filename}"},
                            HTTPStatus.BAD_REQUEST,
                        )
                        return
                archive.extractall(dest)
            self._finish_install(dest_raw, dest)
        except zipfile.BadZipFile:
            self._json({"error": "不是有效的 zip 压缩包"}, HTTPStatus.BAD_REQUEST)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _install_folder(self, body: dict[str, Any]) -> None:
        files = body.get("files")
        if not isinstance(files, list) or not files:
            self._json({"error": "没有收到文件夹内容"}, HTTPStatus.BAD_REQUEST)
            return
        if len(files) > 2000:
            self._json({"error": "文件夹内文件数量过多（超过 2000）"}, HTTPStatus.BAD_REQUEST)
            return
        resolved = self._resolve_skill_dest(body)
        if resolved is None:
            return
        dest_raw, dest = resolved
        pending: list[tuple[Path, bytes]] = []
        total = 0
        for item in files:
            if not isinstance(item, dict):
                self._json({"error": "文件条目格式不正确"}, HTTPStatus.BAD_REQUEST)
                return
            rel = str(item.get("path") or "").replace("\\", "/").lstrip("/")
            parts = [part for part in rel.split("/") if part not in {"", "."}]
            if not parts or any(part == ".." or ":" in part for part in parts):
                self._json(
                    {"error": f"文件夹包含非法路径：{item.get('path')}"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            encoded = str(item.get("data") or "")
            if "," in encoded and encoded.startswith("data:"):
                encoded = encoded.split(",", 1)[1]
            try:
                data = base64.b64decode(encoded, validate=True)
            except ValueError:
                self._json({"error": f"文件内容不是有效 Base64：{rel}"}, HTTPStatus.BAD_REQUEST)
                return
            total += len(data)
            if total > 300 * 1024 * 1024:
                self._json(
                    {"error": "文件夹总大小不能超过 300 MB"},
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
                return
            target = (dest / Path(*parts)).resolve()
            if target != dest and not path_within(target, dest):
                self._json({"error": f"文件夹包含越界路径：{rel}"}, HTTPStatus.BAD_REQUEST)
                return
            pending.append((target, data))
        dest.mkdir(parents=True, exist_ok=True)
        for target, data in pending:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        self._finish_install(dest_raw, dest, {"files": len(pending)})

    def _hidden_skill_entries(self) -> list[dict[str, Any]]:
        """返回当前被隐藏（命中 hidden_skill_ids，但不带隐藏过滤扫描得到）的 Skill 条目。"""
        hidden_ids = set(APP.config.get_hidden_skill_ids())
        if not hidden_ids:
            return []
        try:
            all_skills = SkillCatalog(list(APP.catalog.directories)).scan()
        except Exception:  # noqa: BLE001 - 隐藏列表只是展示信息，不应让扫描失败
            return []
        return [
            {**item, "hidden": True}
            for item in all_skills
            if str(item.get("id") or "") in hidden_ids
        ]

    def _unhide_skill(self, body: dict[str, Any]) -> None:
        skill_id = str(body.get("skill_id") or "").strip()
        if not skill_id:
            self._json({"error": "skill_id 不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        APP.config.unhide_skill(skill_id)
        APP.catalog.hidden_ids.discard(skill_id)
        self._json({
            "ok": True,
            "skills": APP.catalog.scan(),
            "hidden_skills": self._hidden_skill_entries(),
        })

    def _delete_skill(self, body: dict[str, Any]) -> None:
        skill_id = str(body.get("skill_id") or "").strip()
        if not skill_id:
            self._json({"error": "skill_id 不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        self._delete_skill_by_id(skill_id)

    def _delete_skill_by_id(self, skill_id: str) -> None:
        """可恢复删除：移动到应用托管的回收目录，并从 Agent 固定 Skill 中清理引用。"""
        skills = APP.catalog.by_id()
        skill = skills.get(skill_id)
        if not skill:
            self._json({"error": "Skill 不存在"}, HTTPStatus.NOT_FOUND)
            return
        root = Path(str(skill.get("root") or skill.get("path") or "")).expanduser().resolve()
        if not root.exists():
            self._json({"error": "Skill 目录不存在"}, HTTPStatus.NOT_FOUND)
            return
        managed_dir = root.parent
        recycle_dir = DATA_DIR / "skills_recycle"
        agents = APP.config.public_agents()
        try:
            result = delete_skill(
                skill_id,
                str(recycle_dir),
                agents,
                str(managed_dir),
                skills_by_id=skills,
            )
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"删除失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if not result.get("success"):
            self._json({"error": result.get("error", "删除失败")}, HTTPStatus.BAD_REQUEST)
            return
        if result.get("hidden"):
            APP.config.hide_skill(skill_id)
            APP.catalog.hidden_ids.add(skill_id)
        updated_agents = remove_skill_references(skill_id, agents)
        for agent in updated_agents:
            if agent.get("id") in built_in_agent_ids():
                continue
            try:
                APP.config.upsert_agent(agent)
            except Exception:  # noqa: BLE001
                pass
        self._json(
            {
                "ok": True,
                "recycled_to": result.get("recycled_to"),
                "hidden": bool(result.get("hidden")),
                "cleaned_agent_refs": result.get("cleaned_agent_refs", []),
                "skills": APP.catalog.scan(),
                "agents": APP.config.public_agents(),
                "hidden_skills": self._hidden_skill_entries(),
            }
        )


    def _test_provider(self, body: dict[str, Any]) -> None:
        try:
            provider = self._resolve_model_profile(body)
            result = APP.models.complete(
                provider,
                [
                    {"role": "system", "content": "你是连接测试助手。直接回答，不要调用工具。"},
                    {"role": "user", "content": "只回复 OK"},
                ],
                {"temperature": 0, "max_tokens": 128, "stream": False, "connection_test": True},
            )
            capability_resolver = getattr(APP.vision, "brain_image_capability", None)
            capability = (
                capability_resolver(provider, probe_if_unknown=True)
                if callable(capability_resolver)
                else {
                    "supported": bool(APP.vision.brain_supports_images(provider)),
                    "confirmed": False,
                    "source": "model_name",
                }
            )
            self._json({
                "ok": True,
                "response": result,
                "supports_images": bool(capability.get("supported")),
                "capability_confirmed": bool(capability.get("confirmed")),
                "capability_source": str(capability.get("source") or "model_name"),
            })
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _provider_models(self, body: dict[str, Any]) -> None:
        try:
            provider = self._resolve_model_profile(body)
            self._json({"models": APP.models.list_online_models(provider)})
        except Exception as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _unload_provider(self, body: dict[str, Any]) -> None:
        try:
            model_key = str(body.get("model_key") or "").strip()
            provider = APP.config.profile(model_key)
            result = APP.models.unload_local_model(provider)
            self._json({"ok": True, **result})
        except Exception as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _resolve_model_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        """优先用 model_key 解析，缺失时回退到内联 provider（兼容旧调用）。"""
        model_key = str(body.get("model_key") or "").strip()
        if model_key:
            return APP.config.profile(model_key)
        return self._provider_profile(body)

    @staticmethod
    def _provider_profile(body: dict[str, Any]) -> dict[str, Any]:
        provider = dict(body)
        if provider.get("id") and not provider.get("api_key"):
            stored = next(
                (item for item in APP.config.data.get("providers", []) if item.get("id") == provider["id"]),
                None,
            )
            if stored:
                provider = {
                    **stored,
                    **{
                        key: value for key, value in provider.items()
                        if key != "api_key" or bool(value)
                    },
                }
        request_format = str(provider.get("request_format") or "openai_chat").strip().lower()
        provider["kind"] = (
            str(provider.get("kind") or "").strip().lower()
            if str(provider.get("kind") or "").strip().lower() in VALID_MODEL_KINDS
            else _infer_kind_for_request_format(request_format)
        )
        explicit_images = provider.get("supports_images")
        provider["supports_images_explicit"] = (
            explicit_images if isinstance(explicit_images, bool) else None
        )
        return provider

    def _edit_message(self, body: dict[str, Any]) -> None:
        """删除指定消息及其之后所有消息，供"从该处重新编辑对话"使用。

        前端随后会携带新内容调用 /api/chat 重发一轮，因此这里只负责截断。
        """
        conversation_id = str(body.get("conversation_id") or "")
        message_id = str(body.get("message_id") or "")
        if not conversation_id or not message_id:
            self._json({"error": "conversation_id 和 message_id 不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        conversation = APP.storage.get_conversation(conversation_id)
        if not conversation:
            self._json({"error": "对话不存在"}, HTTPStatus.NOT_FOUND)
            return
        target = next((m for m in conversation.get("messages", []) if m.get("id") == message_id), None)
        if not target:
            self._json({"error": "消息不存在"}, HTTPStatus.NOT_FOUND)
            return
        if target.get("role") != "user":
            self._json({"error": "只能编辑用户消息"}, HTTPStatus.BAD_REQUEST)
            return
        removed = APP.storage.truncate_from_message(conversation_id, message_id)
        self._json({"ok": True, "removed": removed, "attachments": (target.get("metadata") or {}).get("attachments") or []})

    def _chat(self, body: dict[str, Any]) -> None:
        try:
            run = APP.runs.submit_chat(body)
        except ActiveRunError as exc:
            self._json(
                {"error": str(exc), "active_run_id": exc.run_id},
                HTTPStatus.CONFLICT,
            )
            return
        except LookupError as exc:
            self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        except (ValueError, TypeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self._stream_run(str(run["id"]), 0, known_run=run)

    def _stream_run(
        self,
        run_id: str,
        after: int = 0,
        known_run: dict[str, Any] | None = None,
    ) -> None:
        run = known_run or APP.runs.get(run_id)
        if not run:
            self._json({"error": "运行不存在"}, HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        sequence = max(0, int(after))
        try:
            while True:
                events = APP.runs.wait_for_events(run_id, sequence, timeout=15.0)
                for event in events:
                    self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
                    self.wfile.flush()
                    sequence = max(sequence, int(event.get("sequence") or 0))
                current = APP.runs.get(run_id)
                if not current or current.get("status") in APP.runs.TERMINAL:
                    if not APP.runs.events_after(run_id, sequence):
                        break
                if not events:
                    self.wfile.write(b'{"type":"heartbeat"}\n')
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Detaching a stream never cancels its conversation-owned run.
            return

    def _confirm_tool(self, body: dict[str, Any]) -> None:
        confirm_id = str(body.get("confirm_id") or "").strip()
        run_id = str(body.get("run_id") or "").strip()
        if not confirm_id or not run_id:
            self._json({"error": "run_id 和 confirm_id 不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        # Do not hold the browser's approval request open while a generation,
        # command or MCP action runs for minutes. The owning Run keeps waiting
        # for the real result through the confirmation condition.
        result_pair = APP.runs.confirm_tool_async(run_id, confirm_id)
        if result_pair is None:
            self._json({"error": "确认请求不属于该运行或已失效"}, HTTPStatus.CONFLICT)
            return
        success, result = result_pair
        self._json({"success": success, "result": result})

    def _reject_tool(self, body: dict[str, Any]) -> None:
        confirm_id = str(body.get("confirm_id") or "").strip()
        run_id = str(body.get("run_id") or "").strip()
        if not confirm_id or not run_id:
            self._json({"error": "run_id 和 confirm_id 不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        result_pair = APP.runs.reject_tool(run_id, confirm_id)
        if result_pair is None:
            self._json({"error": "确认请求不属于该运行或已失效"}, HTTPStatus.CONFLICT)
            return
        success, result = result_pair
        self._json({"success": success, "result": result})


def write_status(host: str, port: int, token: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    access = network_access_status(host, port)
    STATUS_PATH.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": host,
                "port": port,
                **access,
                "access_token": token,
                "started_at": int(time.time()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def acquire_instance_lock():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        handle = LOCK_PATH.open("a+b")
        if LOCK_PATH.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            # 锁文件已能正常打开并写入，仍加锁失败，视为已有实例正在运行。
            handle.close()
            raise RuntimeError("naiba-chat 已经在运行，请勿重复启动") from exc
        return handle
    except OSError as exc:
        # 数据目录创建失败、锁文件打不开等属于环境/权限问题，绝不能误报为"重复启动"。
        raise RuntimeError(
            f"无法创建实例锁文件（{exc}）：请检查数据目录 {DATA_DIR} 及锁文件 {LOCK_PATH} 是否可写"
        ) from exc


def main() -> None:
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace", line_buffering=True, write_through=True)
    parser = argparse.ArgumentParser(description="naiba-chat 局域网对话服务")
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    try:
        instance_lock = acquire_instance_lock()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc

    global APP
    APP = NaibaChatApp()
    host = args.host or str(APP.config.data.get("host", "0.0.0.0"))
    port = args.port or int(APP.config.data.get("port", 8765))
    APP.config.data["host"] = host
    APP.config.data["port"] = port
    APP.config.save()
    APP.listener_host = host
    server = ThreadingHTTPServer((host, port), RequestHandler)
    server.daemon_threads = True
    write_status(host, port, str(APP.config.data["access_token"]))
    print("\nnaiba-chat 已启动")
    access = network_access_status(host, port)
    print(f"手机访问： {access['lan_url'] or access['lan_reason']}")
    print(f"本机访问： {access['local_url']}")
    print(f"访问口令： {APP.config.data['access_token']}")
    print("电脑端不需要打开网页。按 Ctrl+C 停止服务。\n")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        APP.stop()
        instance_lock.close()
        try:
            STATUS_PATH.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    main()
