# -*- coding: utf-8 -*-
"""组装根：NaibaChatApp（层级 3/4，唯一允许组装全部子模块的对象）。

自 server.py 整类迁入（2026-09-06，3.4.2-②）；路径经 PathContext 注入，
本模块不 import server（哲学② DAG 红线）。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

import naiba.net as net_io
from naiba.capability import CapabilityRuntime
from naiba.config import ConfigStore, validate_skills_dir
from naiba.core.contracts import RunContext
from naiba.core.migration import (
    _copy_legacy_data, _database_has_conversations, _merge_data_tree,
    _sync_bundled_skills, migrate_legacy_data,
)
from naiba.core.network import network_access_status
from naiba.core.paths import path_within
from naiba.jobs import JobRegistry, JobSpec
from naiba.llm.runtime import ModelRuntime
from naiba.mcp import MCPRegistry
from naiba.paths import PathContext, default_path_context
from naiba.plans import PlanManager
from naiba.run.manager import ConversationRunManager
from naiba.search import WebSearchRuntime
from naiba.skills.catalog import SkillCatalog
from naiba.storage.media import _uploads_total_bytes
from naiba.storage.store import ChatStorage
from naiba.subagent import job_tool_handler_factory, run_subagent_agent, subagent_handler_factory
from naiba.tools.executor import ToolExecutor
from naiba.tools.registry import build_tool_registry
from naiba.updater import UpdateManager
from naiba.vision.runtime import VisionRouter


class NaibaChatApp:
    def __init__(self, paths: PathContext | None = None):
        self._paths = paths or default_path_context()
        # 公开访问别名（同对象引用：rebind_data_dir 会就地更新字段）
        self.paths = self._paths
        # 冻结版首次启动：从 EXE 相邻旧目录迁移配置与数据到 %LOCALAPPDATA%\NaibaChat。
        initial_data_dir = self._paths.data_dir.resolve()
        self.data_migration = migrate_legacy_data(self._paths)
        self.config = ConfigStore(self._paths.config_path, paths=self._paths)
        # 统一网络代理策略：优先用 config 的 proxy 字段；旧配置未含该字段时
        # 保持历史兼容行为（跟随系统代理），直到用户在运行设置中显式保存。
        net_io.configure(self.config.data.get("proxy"))
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
            self._paths.rebind_data_dir(configured_data_dir)
        self._paths.data_dir.mkdir(parents=True, exist_ok=True)
        self.storage = ChatStorage(self._paths.data_dir / "chat.db")
        repaired_bindings = self.storage.synchronize_workspace_bindings(
            self.config.workspace_bindings()
        )
        if repaired_bindings:
            print(f"Repaired {repaired_bindings} conversation workspace binding(s)")
        self.models = ModelRuntime()
        # Keep packaged Skills in the persistent managed directory as well.
        # A one-file executable extracts bundled assets to a temporary folder;
        # without this copy, replacing the executable can make a Skill that
        # existed in the previous build disappear from the user's catalog.
        bundled_skills = (self._paths.resource_dir / "skills").resolve()
        # 托管 Skills 目录位于数据目录内（self._paths.data_dir/skills），不再固定于 C 盘 self._paths.app_dir。
        managed_skills = self.config.resolve_managed_skills_dir()
        # 启动幂等合并：把旧版「数据目录同级 skills」与旧版「self._paths.app_dir/skills」中的
        # 用户自定义 Skill 合并进新托管目录（目标已有文件优先，不删除旧目录）。
        # 内置 Skills 由下方 _sync_bundled_skills 单独同步，这里跳过以免覆盖打包版本。
        for legacy_src in (
            (self._paths.data_dir.parent / "skills").resolve(),
            (self._paths.app_dir / "skills").resolve(),
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
        # 旧配置默认 `skills_dirs: ["skills"]` 解析为 self._paths.app_dir/skills；重定向到新托管目录，
        # 保证旧配置/旧 Skill 不丢且不再写回 C 盘。
        legacy_managed = (self._paths.app_dir / "skills").resolve()
        for raw in self.config.data.get("skills_dirs", []):
            try:
                resolved = self.config._resolve_dir(str(raw))
                if resolved == legacy_managed:
                    resolved = managed_skills
                validate_skills_dir(resolved, app_dir=self._paths.app_dir, public_dir=self._paths.public_dir, data_dir=self._paths.data_dir)
                if str(resolved) not in skills_dirs:
                    skills_dirs.append(str(resolved))
            except ValueError:
                print(f"已忽略不安全的 Skill 目录：{raw}")
        self.catalog = SkillCatalog(
            [Path(path) for path in skills_dirs],
            base_dir=self._paths.app_dir,
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
        from naiba.tools.registry import build_tool_registry
        from naiba.jobs import JobRegistry

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
        from naiba.subagent import (
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
        from naiba.capability import CapabilityRuntime

        self.capabilities = CapabilityRuntime(self)
        for _name, _handler in self.capabilities.tool_handlers().items():
            self.tool_registry.register_system_handler(_name, _handler)
        # 视觉运行时（Phase 1-3）：注册 7 个视觉工具处理器。文本大脑看不到图时自动路由。
        from naiba.vision.runtime import VisionRouter

        self.vision = VisionRouter(self)
        for _vname, _vhandler in self.vision.tool_handlers().items():
            self.tool_registry.register_system_handler(_vname, _vhandler)
        # 联网搜索运行时（PLAN4 §联网搜索）：搜索开关开启且 provider 可用时才被加入 allowed_tools。
        from naiba.search import WebSearchRuntime

        self.web_search = WebSearchRuntime(self)
        self.tool_registry.register_system_handler("web_search", self._web_search_handler)
        self.tool_registry.register_system_handler("recall_history", self._recall_history_handler)
        # Storage marks in-flight runs as interrupted on startup. Deterministic
        # jobs that explicitly opted into resume are safely re-created from
        # their durable checkpoint and persisted parameters.
        self.jobs.resume_interrupted()
        self.updater = UpdateManager(self._paths.app_dir, self._paths.data_dir)
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

    def _recall_history_handler(
        self, args: dict[str, Any], _skills: Any, _ctx: Any,
    ) -> tuple[bool, str]:
        """历史会话检索：只读本机会话库，按关键词匹配会话标题与消息文本。"""
        query = str(args.get("query") or "").strip()
        raw_max = args.get("max_results")
        max_results = min(max(int(raw_max) if isinstance(raw_max, (int, float)) else 5, 1), 20)
        if not query:
            return False, "query 不能为空"
        try:
            with self.storage._connect() as db:
                rows = db.execute(
                    "SELECT c.id AS cid, c.title AS title, "
                    "       c.updated_at AS conv_updated, "
                    "       m.role AS role, m.content AS content, "
                    "       m.created_at AS msg_created "
                    "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                    "WHERE instr(lower(m.content), lower(?)) > 0 "
                    "ORDER BY m.created_at DESC LIMIT 400",
                    (query,),
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            return False, f"检索失败：{type(exc).__name__}: {exc}"
        if not rows:
            return True, f"未在历史会话中找到与「{query}」相关的内容"
        # 按会话聚合：每个会话取时间最新的前 3 条命中消息
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            cid = str(row["cid"])
            bucket = grouped.setdefault(
                cid,
                {"title": str(row["title"] or "（无标题）"),
                 "updated": int(row["conv_updated"] or 0),
                 "hits": []},
            )
            if len(bucket["hits"]) < 3:
                bucket["hits"].append(
                    {"role": str(row["role"] or ""), "content": str(row["content"] or ""),
                     "created": int(row["msg_created"] or 0)}
                )
        ordered = sorted(grouped.values(), key=lambda item: item["updated"], reverse=True)[:max_results]
        now_ms = int(time.time() * 1000)
        output = [f"在历史会话中找到 {len(ordered)} 个相关会话（关键词「{query}」，仅本机检索）："]
        for index, bucket in enumerate(ordered, 1):
            age_days = max(0, (now_ms - bucket["updated"]) / 86400000)
            when = f"{age_days:.1f} 天前" if age_days >= 1 else "今天"
            output.append(f"{index}. 《{bucket['title']}》（{when} 更新）")
            for hit in bucket["hits"]:
                role_label = "用户" if hit["role"] == "user" else "助手"
                snippet = " ".join(hit["content"].split())[:200]
                output.append(f"   - [{role_label}] {snippet}{'…' if len(hit['content']) > 200 else ''}")
            output.append("")
        output.append("如需确认是哪一次对话，请把上面的标题与时间给用户核对；内容仅作回忆上下文，引用前应回到对应会话复核。")
        return True, "\n".join(output).strip()

    def _todo_write_handler(
        self,
        args: dict[str, Any],
        _skills: Any,
        run_context: RunContext | None = None,
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
        _run_context: RunContext | None = None,
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
        run_context: RunContext | None = None,
    ) -> tuple[bool, str]:
        """Submit a batch of API-format workflows through the durable JobRegistry."""
        from naiba.jobs import JobSpec

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
        _run_context: RunContext | None = None,
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
                initialdir=initial_path if Path(initial_path).is_dir() else str(self._paths.exe_dir),
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
        connection = self.mcp.connection(server_id)
        if not connection:
            raise ValueError(f"未注册的 MCP 服务：{server_id}")
        state = connection.state()
        if server_id == "comfy-mcp":
            address = (connection.env or {}).get("COMFYUI_SERVER_ADDRESS") or "http://127.0.0.1:8188"
            try:
                import urllib.request

                req = urllib.request.Request(f"{address.rstrip('/')}/", method="HEAD")
                with net_io.open(req, timeout=3) as resp:
                    state["comfyui_reachable"] = 200 <= resp.status < 500
            except Exception as exc:
                state["comfyui_reachable"] = False
                state["comfyui_error"] = str(exc)
        return state

    def reconnect_mcp_server(self, server_id: str) -> dict[str, Any]:
        """强制重连指定 MCP 服务：先停止再启动，返回最新状态。"""
        connection = self.mcp.connection(server_id)
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
        report = _copy_legacy_data(source, self._paths)
        if report["config"]:
            # 重新加载配置，使导入的模型/MCP 立即生效。
            self.config = ConfigStore(self._paths.config_path, paths=self._paths)
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
            "image_cache_bytes": _uploads_total_bytes(self._paths.data_dir),
            **access,
            "lan_restart_required": str(self.config.data.get("host", "0.0.0.0")) != self.listener_host,
            "update": self.updater.status(),
            "data_location": {
                "is_frozen": bool(getattr(sys, "frozen", False)),
                "data_dir": str(self._paths.data_dir),
                "config_path": str(self._paths.config_path),
                "exe_dir": str(self._paths.exe_dir),
                "migration": self.data_migration,
            },
            "data_migration": self.migration_health(),
            "resolved_workspace_dir": str(self.config.resolve_workspace_dir()),
            "proxy_state": net_io.proxy_state(),
        }

    def list_skill_dirs(self) -> dict[str, Any]:
        configured = self.config.get_skills_dirs()
        managed = self.config.resolve_managed_skills_dir()
        legacy_managed = (self._paths.app_dir / "skills").resolve()
        resolved = []
        for raw in configured:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = (self._paths.app_dir / path).resolve()
            path = path.resolve()
            # 旧默认 `skills` 解析为 self._paths.app_dir/skills，重定向到新的托管目录。
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
            "backup_location": str(self._paths.data_dir / "backups"),
            "resolved_skills_dirs": [str(path) for path in self.config.skills_dirs_resolved()],
        }

    def migration_backup(self) -> dict[str, Any]:
        """迁移前备份数据库及其 WAL/SHM  siblings。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = self._paths.data_dir / "backups" / f"migration-{stamp}"
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
        # 内的 skills（或旧版同级 skills、旧版 C 盘 self._paths.app_dir/skills），目标为目标 data_dir 内 skills。
        old_managed = (source / "skills").resolve()
        old_sibling_managed = (source.parent / "skills").resolve()
        new_managed = (target / "skills").resolve()
        legacy_managed = (self._paths.app_dir / "skills").resolve()
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
            # 搬迁托管 Skills 目录（旧 managed / 旧同级 managed / 旧 self._paths.app_dir/skills → 新 managed）。
            old_managed_sources = []
            for candidate in (old_managed, old_sibling_managed, legacy_managed):
                if candidate.is_dir() and candidate not in old_managed_sources:
                    old_managed_sources.append(candidate)
            if old_managed_sources and new_managed != old_managed:
                new_managed.mkdir(parents=True, exist_ok=True)
                for old_src in old_managed_sources:
                    if old_src.is_dir():
                        _merge_data_tree(old_src, new_managed)
            # 改写 skills_dirs：把指向旧 managed / 旧同级 managed / 旧 self._paths.app_dir/skills 的项改写为新绝对路径。
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



APP: NaibaChatApp


