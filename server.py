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

# 打包成 exe（PyInstaller）后，__file__ 指向临时解压目录，不能用于读写运行数据。
# 目录分三类：
#   - EXE_DIR：exe 所在目录（仅冻结时与仓库根不同），用于默认工作区与定位相邻旧数据。
#   - RESOURCE_DIR：静态资源（public 等），随 exe 打包，运行时从 sys._MEIPASS 读取。
#   - APP_DIR：可写运行数据目录（config.json / data / skills），冻结版固定到
#     %LOCALAPPDATA%\NaibaChat；源码模式继续使用仓库目录。
if getattr(sys, "frozen", False):
    EXE_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", EXE_DIR)).resolve()
    _localappdata = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    APP_DIR = Path(_localappdata).resolve() / "NaibaChat"
else:
    EXE_DIR = Path(__file__).resolve().parent
    RESOURCE_DIR = EXE_DIR
    APP_DIR = EXE_DIR
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if str(EXE_DIR) not in sys.path and str(EXE_DIR) != str(APP_DIR):
    sys.path.insert(0, str(EXE_DIR))

from mcp_runtime import MCPRegistry
from model_runtime import ModelRuntime
from plan_runtime import ASK_MODE_PROMPT, CraftToolExecutor, PlanManager, ReadOnlyToolExecutor, resolve_mode_tools
from skill_runtime import SkillAgent, SkillCatalog, TaskCancelled, ToolExecutor
from async_tasks import ActiveRunError, ConversationRunManager
from storage import ChatStorage
from updater import UpdateManager


PUBLIC_DIR = RESOURCE_DIR / "public"
DATA_DIR = APP_DIR / "data"
CONFIG_PATH = APP_DIR / "config.json"
STATUS_PATH = DATA_DIR / "server.json"
LOCK_PATH = DATA_DIR / "server.lock"


def _config_has_providers(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    providers = value.get("providers") if isinstance(value, dict) else None
    return isinstance(providers, list) and any(
        isinstance(provider, dict) and str(provider.get("id") or "").strip()
        for provider in providers
    )


def _database_has_conversations(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        try:
            row = connection.execute("SELECT COUNT(*) FROM conversations").fetchone()
            return bool(row and int(row[0] or 0))
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return False


def _copy_legacy_data(source: Path, replace_empty_target: bool = True) -> dict[str, bool]:
    """Merge a legacy install into APP_DIR without overwriting non-empty data."""
    report = {"config": False, "data": False}
    legacy_config = source / "config.json"
    legacy_data = source / "data"
    APP_DIR.mkdir(parents=True, exist_ok=True)

    if legacy_config.is_file() and (
        not CONFIG_PATH.exists() or not _config_has_providers(CONFIG_PATH)
    ):
        shutil.copy2(legacy_config, CONFIG_PATH)
        report["config"] = True

    if not legacy_data.is_dir():
        return report
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    source_db = legacy_data / "chat.db"
    target_db = DATA_DIR / "chat.db"
    replace_db = source_db.is_file() and (
        not target_db.exists()
        or (replace_empty_target and not _database_has_conversations(target_db)
            and _database_has_conversations(source_db))
    )
    if replace_db:
        # A stale WAL/SHM pair from the empty database must not be reused with
        # the restored database. The running server is stopped before startup
        # migration, and the target directory was backed up by the caller.
        for suffix in ("-wal", "-shm"):
            sidecar = target_db.with_name(target_db.name + suffix)
            try:
                sidecar.unlink(missing_ok=True)
            except OSError:
                pass
        shutil.copy2(source_db, target_db)
        report["data"] = True
        for suffix in ("-wal", "-shm"):
            sidecar = source_db.with_name(source_db.name + suffix)
            if sidecar.is_file() and sidecar.stat().st_size:
                shutil.copy2(sidecar, target_db.with_name(target_db.name + suffix))

    for item in legacy_data.iterdir():
        if item.name in {"chat.db", "chat.db-wal", "chat.db-shm", "server.lock"}:
            continue
        destination = DATA_DIR / item.name
        if destination.exists():
            continue
        if item.is_dir():
            shutil.copytree(item, destination)
        else:
            shutil.copy2(item, destination)
        report["data"] = True
    return report


def migrate_legacy_data() -> dict[str, Any]:
    """冻结版首次启动：从 EXE 相邻旧目录迁移 config.json 与 data/ 到数据目录。

    仅当数据目录（%LOCALAPPDATA%\\NaibaChat）尚未初始化时执行；旧文件保留，
    不覆盖已存在的新数据。源码模式（APP_DIR == EXE_DIR）跳过。
    """
    report: dict[str, Any] = {"migrated": False, "config": False, "data": False, "source": ""}
    if str(APP_DIR) == str(EXE_DIR):
        return report
    legacy_config = EXE_DIR / "config.json"
    legacy_data = EXE_DIR / "data"
    if not legacy_config.is_file() and not legacy_data.is_dir():
        return report
    try:
        migrated = _copy_legacy_data(EXE_DIR)
        report.update(migrated)
    except OSError as exc:
        print(f"迁移旧数据失败：{exc}")
    if report["config"] or report["data"]:
        report["migrated"] = True
        report["source"] = str(EXE_DIR)
    return report


def static_asset_version() -> str:
    digest = hashlib.sha256()
    for name in ("app.js", "styles.css"):
        digest.update((PUBLIC_DIR / name).read_bytes())
    return digest.hexdigest()[:12]


STATIC_ASSET_VERSION = static_asset_version()


def path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_skills_dir(resolved: Path) -> None:
    """限制 Skill 目录范围，防止把高危目录暴露给扫描、解压和文件读取。"""
    resolved = resolved.resolve()
    if resolved.parent == resolved:
        raise ValueError("不能把磁盘根目录作为 Skill 目录")
    system_roots = [Path(os.environ.get("SystemRoot", r"C:\Windows"))]
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        value = os.environ.get(env_name)
        if value:
            system_roots.append(Path(value))
    for root in system_roots:
        root = root.resolve()
        if resolved == root or path_within(resolved, root):
            raise ValueError(f"不允许使用系统目录作为 Skill 目录：{root}")
    forbidden_exact = {Path.home().resolve(), APP_DIR, PUBLIC_DIR.resolve(), DATA_DIR.resolve()}
    if resolved in forbidden_exact:
        raise ValueError("不能把用户主目录或程序自身目录作为 Skill 目录，请使用其子目录")


def _detect_choice_groups(text: str) -> list[dict[str, Any]]:
    """检测回复中的交互选项组，并保留每组前面的提示语。"""
    # 代码示例中的列表不是交互选项，先排除 fenced code block。
    visible_text = re.sub(r"```[\s\S]*?```", "", text)
    lines = visible_text.splitlines()

    def clean(value: str) -> str:
        value = re.sub(r"^(?:\[[ xX]\]\s*)", "", value.strip())
        value = re.sub(r"^(?:\*\*|__)", "", value)
        value = re.sub(r"\s*(?:\*\*|__)$", "", value)
        return value.strip()

    circled_numbers = {char: index for index, char in enumerate("①②③④⑤⑥⑦⑧", start=1)}
    choice_cue = re.compile(
        r"请(?:先|再)?选择|再选(?:一下|一个|个)?|供你选择|可选|选项|方案|哪个|选哪个|"
        r"pick|choose|select|options?",
        re.IGNORECASE,
    )

    numbered_pattern = re.compile(
        r"^\s*(?:\*\*|__)?(?:[（(\[【]?\s*(\d{1,2})\s*[.、):：）\]】])"
        r"\s*(?:\*\*|__)?\s*(.+?)\s*$"
    )
    lettered_pattern = re.compile(
        r"^\s*(?:\*\*|__)?(?:[（(\[【]?\s*([A-Ha-h])\s*[.、):：）\]】])"
        r"\s*(?:\*\*|__)?\s*(.+?)\s*$"
    )
    named_pattern = re.compile(
        r"^\s*(?:\*\*|__)?(?:选项|方案)\s*[一二三四五六七八\dA-Ha-h]+\s*[.、:：)）]"
        r"\s*(?:\*\*|__)?\s*(.+?)\s*$",
        re.IGNORECASE,
    )
    bullet_pattern = re.compile(r"^\s*[-+*•]\s+(?:\[[ xX]\]\s*)?(.+?)\s*$")

    groups: list[tuple[str, list[tuple[Any, str]], str]] = []
    named_items: list[tuple[Any, str]] = []
    named_group_added = False
    current_kind = ""
    current_items: list[tuple[Any, str]] = []
    current_prompt = ""
    preceding_prompt = ""
    recent_cue_prompt = ""

    def finish_group() -> None:
        nonlocal current_kind, current_items, current_prompt
        if current_items:
            groups.append((current_kind, current_items, current_prompt))
        current_kind = ""
        current_items = []
        current_prompt = ""

    for line in lines:
        parsed: tuple[str, Any, str] | None = None
        match = numbered_pattern.match(line)
        if match:
            parsed = ("numbered", int(match.group(1)), clean(match.group(2)))
        if not parsed:
            match = lettered_pattern.match(line)
            if match:
                parsed = ("lettered", match.group(1).upper(), clean(match.group(2)))
        if not parsed:
            match = named_pattern.match(line)
            if match:
                parsed = ("named", len(current_items), clean(match.group(1)))
        stripped = line.strip()
        if not parsed and stripped and stripped[0] in circled_numbers:
            value = clean(stripped[1:].lstrip(".、):：） "))
            if value:
                parsed = ("numbered", circled_numbers[stripped[0]], value)
        if not parsed:
            match = bullet_pattern.match(line)
            if match:
                parsed = ("bullet", len(current_items), clean(match.group(1)))

        if parsed and parsed[2]:
            kind, marker, value = parsed
            if kind == "named":
                finish_group()
                if not named_group_added:
                    named_prompt = (
                        preceding_prompt
                        if choice_cue.search(preceding_prompt)
                        else recent_cue_prompt
                    )
                    groups.append(("named", named_items, named_prompt))
                    named_group_added = True
                named_items.append((len(named_items), value))
                continue
            if current_items and kind != current_kind:
                finish_group()
            if not current_items:
                current_kind = kind
                current_prompt = (
                    preceding_prompt
                    if choice_cue.search(preceding_prompt)
                    else recent_cue_prompt
                )
            current_items.append((marker, value))
            continue

        finish_group()
        if stripped:
            preceding_prompt = clean(re.sub(r"^(?:#{1,6}\s*)", "", stripped))
            if choice_cue.search(preceding_prompt):
                recent_cue_prompt = preceding_prompt

    finish_group()

    candidates: list[dict[str, Any]] = []
    for kind, items, prompt in groups:
        if len(items) < 2:
            continue
        markers = [marker for marker, _ in items]
        if kind == "numbered" and markers != list(range(markers[0], markers[0] + len(items))):
            continue
        if kind == "lettered":
            expected = [chr(ord(markers[0]) + offset) for offset in range(len(items))]
            if markers != expected:
                continue
        has_cue = bool(choice_cue.search(prompt))
        if kind == "bullet" and not has_cue:
            continue
        candidates.append(
            {
                "prompt": prompt if has_cue else "",
                "choices": [value for _, value in items][:8],
                "has_cue": has_cue,
            }
        )

    prompted = [candidate for candidate in candidates if candidate["has_cue"]]
    selected = prompted or candidates[:1]
    return [
        {"prompt": candidate["prompt"], "choices": candidate["choices"]}
        for candidate in selected
    ]


def _detect_choices(text: str) -> list[str]:
    """兼容旧调用方：返回检测到的第一组选项。"""
    groups = _detect_choice_groups(text)
    return groups[0]["choices"] if groups else []


def default_config() -> dict[str, Any]:
    return {
        "host": "0.0.0.0",
        "port": 8765,
        "access_token": f"{secrets.randbelow(1000000):06d}",
        "skills_dirs": ["skills"],
        "workspace_dir": "workspace",
        "provider_id": "",
        "temperature": 0.7,
        "max_tokens": 8192,
        "context_size": 8192,
        "agent_system_prompt": "",
        "permission_mode": "confirm",
        "agent_tools": [
            "read_file",
            "write_file",
            "list_directory",
            "search_files",
            "run_command",
            "run_skill_script",
            "http_request",
            "register_mcp",
            "call_mcp",
        ],
        "command_timeout": 120,
        "providers": [],
        # MCP 服务默认不注册；需要 MCP 的 Skill 可通过受控工具自动注册。
        "mcp_servers": [],
        # 多 Agent 定义：每个 Agent 有独立的预设/规则（system_prompt）与固定 Skill（skill_ids）。
        "agents": [
            {"id": "general", "name": "通用 Agent", "system_prompt": "", "skill_ids": []},
            {
                "id": "coding",
                "name": "编程 Agent",
                "system_prompt": "你是资深编程助手。先理解需求，再给出可直接运行、结构清晰的代码；涉及文件操作时先说明改动范围。",
                "skill_ids": [],
            },
            {
                "id": "drama",
                "name": "短剧 Agent",
                "system_prompt": "你是短剧创作助手。遵循所选短剧类 Skill 的交互收集流程，逐步确认主题、角色、分镜与风格后再产出内容。",
                "skill_ids": [],
            },
        ],
        "default_agent_id": "general",
        # 视觉（Phase 0-3）：provider 缺省时使用内置 OVH 免费匿名视觉链兜底。
        "vision": {
            "auto_route": True,
            "provider_model_key": "",
            "fallback_models": [],
            "brain_supports_image": False,
            "timeout_ms": 120000,
            "cache": True,
            "cache_ttl_seconds": 3600,
            "cache_max_entries": 200,
            "max_images": 4,
        },
        # 联网搜索（PLAN4 §联网搜索）：完全可选；endpoint/Key/模型/启用状态由用户配置。
        "search": {
            "enabled": False,
            "provider": "custom",
            "endpoint": "",
            "api_key": "",
            "model": "",
            "max_results": 5,
        },
    }


# 内置 Agent（PLAN4 §Agent 与权限）：不可覆盖/删除，可复制为用户自定义 Agent。
# tool_scope 定义该 Agent 允许的最大工具集合；运行流中与对话权限（allowed_tools）取交集，
# 权限切换不能扩大 preset 本身的工具集合。内置 Agent 使用当前对话选择的模型。
_BUILT_IN_SCOPE_FULL = (
    "read_file", "write_file", "list_directory", "search_files", "run_command",
    "run_skill_script", "http_request", "register_mcp", "call_mcp",
    "run_in_background", "job_output", "job_status", "job_wait", "job_kill", "subagent",
    "vision_describe", "vision_ground", "vision_detect", "vision_crop", "vision_ocr",
    "vision_colors", "vision_pixel_diff", "web_search",
)
_BUILT_IN_SCOPE_CODE = (
    "read_file", "write_file", "list_directory", "search_files", "run_command",
    "run_skill_script", "http_request", "run_in_background", "job_output", "job_status",
    "job_wait", "job_kill", "subagent",
    "vision_describe", "vision_ground", "vision_detect", "vision_crop", "vision_ocr",
    "vision_colors", "vision_pixel_diff", "web_search",
)
_BUILT_IN_SCOPE_MINIMAL = (
    "read_file", "list_directory", "search_files", "write_file", "run_command",
)
_BUILT_IN_SCOPE_CORDIS = (
    "read_file", "write_file", "list_directory", "search_files", "run_command",
    "run_skill_script", "http_request", "subagent",
    "vision_describe", "vision_ground", "vision_ocr", "vision_colors", "web_search",
)


def built_in_agents() -> list[dict[str, Any]]:
    """返回内置 Agent 定义清单（含 tool_scope）。每次调用返回新副本，防止被外部篡改。"""
    return [
        {
            "id": "dsh-standard",
            "name": "dsh-standard（全能）",
            "system_prompt": (
                "你是 naiba-chat 的全能内置 Agent，拥有完整工具、Skill、计划、MCP、联网搜索与子任务能力。"
                "根据用户意图自主选择工具与子 Agent 完成多步任务；涉及文件改动先说明范围。"
            ),
            "skill_ids": [],
            "tool_scope": list(_BUILT_IN_SCOPE_FULL),
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
            "tool_scope": list(_BUILT_IN_SCOPE_CODE),
            "built_in": True,
        },
        {
            "id": "dsh-minimal",
            "name": "dsh-minimal（极简编码）",
            "system_prompt": (
                "你是极简编码 Agent，只开放读取、列出目录、搜索、写入与必要的测试命令。"
                "不要调用 MCP、视觉或联网搜索等扩展能力。"
            ),
            "skill_ids": [],
            "tool_scope": list(_BUILT_IN_SCOPE_MINIMAL),
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
            "tool_scope": list(_BUILT_IN_SCOPE_CORDIS),
            "built_in": True,
        },
    ]


def built_in_agent_ids() -> set[str]:
    return {agent["id"] for agent in built_in_agents()}


# 在线请求协议集合（首期本地后端仅支持 ollama / lm_studio）。
ONLINE_REQUEST_FORMATS = {"openai_chat", "codex_responses", "gemini", "claude"}
LOCAL_REQUEST_FORMATS = {"ollama", "lm_studio"}
VALID_MODEL_KINDS = {"online", "local"}
VALID_LOCAL_BACKENDS = {"ollama", "lm_studio"}


def _infer_kind_for_request_format(request_format: str) -> str:
    """根据请求格式推断模型类别，兼容未携带 kind 的旧配置。"""
    return "local" if request_format in LOCAL_REQUEST_FORMATS else "online"


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
        # MCP 配置去重：重复 server id 只保留首个（PLAN4 §MCP）。
        servers = defaults.get("mcp_servers")
        if isinstance(servers, list):
            seen: dict[str, int] = {}
            deduped = []
            for server in servers:
                if not isinstance(server, dict):
                    continue
                sid = str(server.get("id") or "").strip()
                if not sid or sid in seen:
                    if sid:
                        print(f"[config] Ignored duplicate MCP server id: {sid}")
                    continue
                seen[sid] = 1
                deduped.append(server)
            defaults["mcp_servers"] = deduped
        self.data = defaults
        # Legacy builds persisted max_agent_steps; it is intentionally ignored.
        self.data.pop("max_agent_steps", None)
        try:
            self.data["context_size"] = self._positive_context_size(
                self.data.get("context_size", 8192), "context_size"
            )
        except ValueError:
            self.data["context_size"] = 8192
        tools = self.data.get("agent_tools")
        if isinstance(tools, list) and "call_mcp" in tools and "register_mcp" not in tools:
            tools.insert(tools.index("call_mcp"), "register_mcp")
        # 在线/本地模型配置分层：为旧 providers 补全 kind/local_backend，并生成 default_model_key。
        self._migrate_model_profiles()
        self.save()

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
                if key not in {"access_token", "providers", "mcp_servers"}
            }
            result["resolved_workspace_dir"] = str(self.resolve_workspace_dir())
            return result

    def get_skills_dirs(self) -> list[str]:
        with self.lock:
            return list(self.data.get("skills_dirs", []))

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

    def _resolve_dir(self, raw: str) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (APP_DIR / path).resolve()
        return path.resolve()

    def resolve_workspace_dir(self, raw: str | None = None) -> Path:
        """解析工作区目录：相对路径以 EXE 所在目录为基准（不受启动目录影响）。"""
        raw = (raw if raw is not None else self.data.get("workspace_dir", "workspace") or "workspace").strip()
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (EXE_DIR / path).resolve()
        return path.resolve()

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
            APP_DIR,
            DATA_DIR.resolve(),
            PUBLIC_DIR.resolve(),
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
                }
                for provider in self.data.get("providers", [])
            ]

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {
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
            "vision",
            "search",
        }
        with self.lock:
            for key in allowed:
                if key in values:
                    if key == "access_token":
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
                            "run_command", "run_skill_script", "http_request", "register_mcp", "call_mcp",
                        }
                        requested = values[key] if isinstance(values[key], list) else []
                        self.data[key] = [tool for tool in requested if tool in valid_tools]
                    elif key == "context_size":
                        self.data[key] = self._positive_context_size(values[key], "context_size")
                    elif key == "workspace_dir":
                        raw = str(values[key] or "").strip()
                        if not raw:
                            # 恢复默认：EXE 所在目录下的 workspace。
                            raw = "workspace"
                        resolved = self.resolve_workspace_dir(raw)
                        self.ensure_workspace_writable(resolved)
                        self.data[key] = raw
                    elif key in ("vision", "search"):
                        incoming = values[key]
                        if not isinstance(incoming, dict):
                            raise ValueError(f"{key} 必须是对象")
                        # 合并到现有子配置，避免丢失其他子字段。
                        merged = dict(self.data.get(key, {}))
                        for sub_key, sub_value in incoming.items():
                            merged[str(sub_key)] = sub_value
                        self.data[key] = merged
                    else:
                        self.data[key] = values[key]
            self.save()
            return self.public()

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

    def upsert_provider(self, values: dict[str, Any]) -> dict[str, Any]:
        """兼容别名：按请求格式推断类别后转发到统一模型配置保存。"""
        request_format = str(values.get("request_format") or "openai_chat").strip().lower()
        payload = dict(values)
        payload["kind"] = _infer_kind_for_request_format(request_format)
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
                raise ValueError("本地后端必须是 ollama 或 lm_studio")
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
                if local_backend == "ollama":
                    raw_context_size = values.get("context_size")
                    if raw_context_size not in (None, ""):
                        payload["context_size"] = self._positive_context_size(
                            raw_context_size, "Ollama context_size"
                        )
            else:
                payload.pop("local_backend", None)
            if not payload["base_url"] or not payload["model"]:
                raise ValueError("API/服务地址和模型名称不能为空")
            if existing:
                # 空 API Key 表示保留已有 Key（不覆盖、不清除）。
                if not payload["api_key"]:
                    payload["api_key"] = existing.get("api_key", "")
                if "context_size" not in payload:
                    existing.pop("context_size", None)
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
        return {
            **stored,
            "model_key": f"{kind}:{model_id}",
            "api_key": "",
            "has_api_key": bool(stored["api_key"]),
            "is_default": (self.data.get("default_model_key") == f"{kind}:{model_id}"),
            "supports_images_explicit": (
                explicit_images if isinstance(explicit_images, bool) else None
            ),
            "supports_images": _infer_supports_images(stored),
        }

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
            return {
                "kind": provider.get("kind", kind),
                **provider,
                "supports_images": _infer_supports_images(provider),
            }

    def generation_options(self) -> dict[str, Any]:
        with self.lock:
            return {
                key: self.data[key]
                for key in ("temperature", "max_tokens", "context_size")
            }

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
            # 内置 Agent 追加在用户自定义之后，标记 built_in 且不可覆盖。
            built_in = [dict(agent) for agent in built_in_agents()]
            return custom + built_in

    def default_agent_id(self) -> str:
        with self.lock:
            agents = self.data.get("agents", [])
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
        # 内置 Agent 不可覆盖或删除（PLAN4 §Agent 与权限）。
        if agent_id in built_in_agent_ids():
            raise ValueError("内置 Agent 不可覆盖，请复制为用户自定义 Agent 后修改")
        name = str(values.get("name") or "").strip()
        if not name:
            raise ValueError("Agent 名称不能为空")
        system_prompt = str(values.get("system_prompt") or "")[:12000]
        raw_skills = values.get("skill_ids") or []
        if not isinstance(raw_skills, list):
            raise ValueError("skill_ids 必须是数组")
        skill_ids = list(dict.fromkeys(str(item) for item in raw_skills if str(item).strip()))
        payload = {
            "id": agent_id,
            "name": name[:80],
            "system_prompt": system_prompt,
            "skill_ids": skill_ids,
        }
        with self.lock:
            agents = self.data.setdefault("agents", [])
            index = next((i for i, item in enumerate(agents) if item.get("id") == agent_id), None)
            if index is None:
                agents.append(payload)
            else:
                agents[index] = payload
            if not any(item.get("id") == self.data.get("default_agent_id") for item in agents):
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


def get_lan_ip() -> str:
    candidates: list[str] = []
    try:
        candidates.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        candidates.append(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()
    unique = list(dict.fromkeys(candidates))
    for prefix in ("192.168.", "10."):
        preferred = next((address for address in unique if address.startswith(prefix)), None)
        if preferred:
            return preferred
    for address in unique:
        try:
            parsed = ipaddress.ip_address(address)
            first, second = map(int, address.split(".")[:2])
            if first == 172 and 16 <= second <= 31:
                return address
            if parsed.is_private and not parsed.is_loopback and not address.startswith("198.18."):
                return address
        except ValueError:
            continue
    return "127.0.0.1"


IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

MODEL_IMAGE_MAX_EDGE = 1600
MODEL_IMAGE_TARGET_BYTES = 900 * 1024
MODEL_IMAGE_HISTORY_LIMIT = 3


def _jpeg_for_model(image: Any, target_bytes: int = MODEL_IMAGE_TARGET_BYTES) -> bytes:
    from PIL import Image

    image.thumbnail((MODEL_IMAGE_MAX_EDGE, MODEL_IMAGE_MAX_EDGE))
    if image.mode not in {"RGB", "L"}:
        background = Image.new("RGB", image.size, "white")
        if "A" in image.getbands():
            background.paste(image, mask=image.getchannel("A"))
        else:
            background.paste(image)
        image = background

    encoded = b""
    for quality in (85, 78, 70, 62):
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        encoded = buffer.getvalue()
        if len(encoded) <= target_bytes:
            return encoded

    while len(encoded) > target_bytes and max(image.size) > 768:
        next_size = tuple(max(1, int(value * 0.85)) for value in image.size)
        image = image.resize(next_size, Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=62, optimize=True)
        encoded = buffer.getvalue()
    return encoded


def encode_image_for_model(source: str) -> dict[str, str] | None:
    path = Path(source).expanduser().resolve()
    media_type = IMAGE_MEDIA_TYPES.get(path.suffix.lower())
    if not media_type or not path.is_file() or path.stat().st_size > 30 * 1024 * 1024:
        return None
    raw = path.read_bytes()
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(raw)) as opened:
            source_format = opened.format
            image = ImageOps.exif_transpose(opened).copy()
            needs_conversion = (
                max(image.size) > MODEL_IMAGE_MAX_EDGE
                or len(raw) > MODEL_IMAGE_TARGET_BYTES
                or source_format == "GIF"
            )
            if needs_conversion:
                raw = _jpeg_for_model(image)
                media_type = "image/jpeg"
    except (ImportError, OSError, ValueError):
        if len(raw) > 8 * 1024 * 1024:
            return None
    return {
        "type": "image",
        "media_type": media_type,
        "data": base64.b64encode(raw).decode("ascii"),
        "name": path.name,
    }


def build_model_history(conversation_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build model history while carrying only the most recent image batch."""
    selected_paths: list[str] = []
    for item in reversed(conversation_messages[-16:]):
        if item.get("role") != "user":
            continue
        batch = []
        for attachment in (item.get("metadata") or {}).get("attachments") or []:
            source = str(attachment.get("path") or "")
            if Path(source).suffix.lower() in IMAGE_MEDIA_TYPES and source not in batch:
                batch.append(source)
        if batch:
            selected_paths = batch[:MODEL_IMAGE_HISTORY_LIMIT]
            break
    selected_images = set(selected_paths)

    history: list[dict[str, Any]] = []
    for item in conversation_messages:
        if item.get("role") not in {"user", "assistant"}:
            continue
        content = str(item.get("content") or "")
        previous_uploads = (item.get("metadata") or {}).get("attachments") or []
        if item.get("role") == "user" and previous_uploads:
            paths = [
                f"[用户上传文件：{upload.get('path')}]"
                for upload in previous_uploads
                if upload.get("path")
            ]
            if paths:
                content += "\n" + "\n".join(paths)
            image_parts = [
                encoded
                for upload in previous_uploads
                if str(upload.get("path") or "") in selected_images
                for encoded in [encode_image_for_model(str(upload.get("path") or ""))]
                if encoded
            ]
            if image_parts:
                history.append(
                    {
                        "role": item["role"],
                        "content": [{"type": "text", "text": content}, *image_parts],
                    }
                )
                continue
        history.append({"role": item["role"], "content": content})
    return history


def extract_attachments(runs: list[dict[str, Any]]) -> list[dict[str, str]]:
    extensions = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm", ".wav", ".mp3")
    candidates: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, str):
            if value.lower().split("?")[0].endswith(extensions):
                candidates.append(value)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)

    for run in runs:
        result = run.get("result", "")
        try:
            visit(json.loads(result))
        except (json.JSONDecodeError, TypeError):
            for match in re.findall(r"(?:[A-Za-z]:\\[^\r\n\"']+|https?://[^\s\"']+)", str(result)):
                visit(match.rstrip(".,)"))
    unique = []
    seen = set()
    for item in candidates:
        if item in seen:
            continue
        seen.add(item)
        unique.append({"name": Path(urllib.parse.urlparse(item).path).name or "生成结果", "source": item})
    return unique[:20]


def _infer_supports_images(provider: dict[str, Any]) -> bool:
    """推断模型是否支持图片输入（supports_images 能力字段）。

    - 配置显式给出布尔值时直接使用；
    - DeepSeek 官方接口（api.deepseek.com 等）默认 false（纯文本模型）；
    - 其余按模型名启发式推断（gemini / claude / 含 vl 等关键词）。
    """
    explicit = provider.get("supports_images")
    if isinstance(explicit, bool):
        return explicit
    base_url = str(provider.get("base_url") or "").lower()
    if "api.deepseek.com" in base_url or "deepseek.com" in base_url:
        return False
    try:
        from vision_runtime import VisionRouter

        return VisionRouter._brain_supports_vision(provider)
    except Exception:  # noqa: BLE001 - 视觉模块不可用时不阻塞模型解析
        return False


class NaibaChatApp:
    def __init__(self):
        # 冻结版首次启动：从 EXE 相邻旧目录迁移配置与数据到 %LOCALAPPDATA%\NaibaChat。
        self.data_migration = migrate_legacy_data()
        self.config = ConfigStore(CONFIG_PATH)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.storage = ChatStorage(DATA_DIR / "chat.db")
        self.models = ModelRuntime()
        skills_dirs = []
        for raw in self.config.data.get("skills_dirs", []):
            try:
                validate_skills_dir(self.config._resolve_dir(str(raw)))
                skills_dirs.append(raw)
            except ValueError:
                print(f"已忽略不安全的 Skill 目录：{raw}")
        bundled_skills = RESOURCE_DIR / "skills"
        if bundled_skills.exists():
            skills_dirs.insert(0, str(bundled_skills))
        self.catalog = SkillCatalog(
            [Path(path) for path in skills_dirs],
            base_dir=APP_DIR,
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
        # MCP 生命周期：启动即在后台连接所有已启用服务，保持到退出。
        threading.Thread(target=self._start_mcp_background, name="mcp-startup", daemon=True).start()
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
        # 视觉运行时（Phase 1-3）：注册 7 个视觉工具处理器。文本大脑看不到图时自动路由。
        from vision_runtime import VisionRouter

        self.vision = VisionRouter(self)
        for _vname, _vhandler in self.vision.tool_handlers().items():
            self.tool_registry.register_system_handler(_vname, _vhandler)
        # 联网搜索运行时（PLAN4 §联网搜索）：搜索开关开启且 provider 可用时才被加入 allowed_tools。
        from web_search_runtime import WebSearchRuntime

        self.web_search = WebSearchRuntime(self)
        self.tool_registry.register_system_handler("web_search", self._web_search_handler)
        # Local smoke/test builds can opt out without changing persisted user settings.
        auto_update = os.environ.get("NAIBA_DISABLE_AUTO_UPDATE", "").strip().lower() not in {"1", "true", "yes"}
        self.updater = UpdateManager(APP_DIR, DATA_DIR, auto_update=auto_update)
        self.update_restart_callback = None

    def stop(self) -> None:
        self.runs.shutdown()
        self.plans.shutdown()
        self.jobs.shutdown()
        self.mcp.stop()

    def _web_search_handler(self, args: dict[str, Any], _skills: Any, _ctx: Any) -> tuple[bool, str]:
        query = str(args.get("query") or args.get("q") or "")
        max_results = args.get("max_results")
        return self.web_search.search(query, int(max_results) if isinstance(max_results, (int, float)) else None)

    def register_mcp_server(self, values: dict[str, Any]) -> dict[str, Any]:
        if str(values.get("id") or "").strip() == "comfyui":
            values = self._persist_comfyui_mcp(values)
        config = self.config.upsert_mcp_server(values)
        return {"saved": True, "server": self.mcp.upsert(config)}

    @staticmethod
    def _persist_comfyui_mcp(values: dict[str, Any]) -> dict[str, Any]:
        payload = dict(values)
        args = [str(item) for item in payload.get("args", [])]
        env = {str(key): str(value) for key, value in (payload.get("env") or {}).items()}
        source_script = next((Path(item) for item in args if Path(item).name == "comfyui_mcp_server.py"), None)
        if not source_script or not source_script.is_file():
            raise ValueError("找不到 ComfyUI MCP 服务脚本")
        target_root = DATA_DIR / "mcp" / "comfyui"
        target_root.mkdir(parents=True, exist_ok=True)
        target_script = target_root / source_script.name
        shutil.copy2(source_script, target_script)
        workflows = Path(env.get("COMFYUI_WORKFLOWS_DIR", ""))
        if workflows.is_dir():
            target_workflows = target_root / "workflows"
            shutil.copytree(workflows, target_workflows, dirs_exist_ok=True)
            env["COMFYUI_WORKFLOWS_DIR"] = str(target_workflows)
        payload["args"] = [str(target_script) if Path(item) == source_script else item for item in args]
        payload["env"] = env
        return payload

    def _start_mcp_background(self) -> None:
        """应用启动后在后台连接所有已启用 MCP 服务，并保持到退出。"""
        try:
            self.mcp.start()
        except Exception as exc:  # 单个服务启动失败不应中断其他服务
            print(f"MCP 后台启动部分失败：{exc}")

    def test_mcp_server(self, server_id: str) -> dict[str, Any]:
        """返回指定 MCP 的 stdio 状态，并对 ComfyUI 额外探测 HTTP 可达性。"""
        with self.mcp._lock:
            connection = self.mcp.connections.get(server_id)
        if not connection:
            raise ValueError(f"未注册的 MCP 服务：{server_id}")
        state = connection.state()
        if server_id == "comfyui":
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
        """从用户指定的旧数据目录导入 config.json 与 data/（保留目标已存在数据）。"""
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
        return {
            "settings": self.config.public(),
            "providers": self.config.public_providers(),
            "model_profiles": self.config.model_profiles(),
            "default_model_key": self.config.default_model_key(),
            "skills": self.catalog.scan(),
            "mcp_servers": self.mcp.states(),
            "agents": self.config.public_agents(),
            "default_agent_id": self.config.default_agent_id(),
            "lan_url": f"http://{get_lan_ip()}:{self.config.data['port']}",
            "update": self.updater.status(),
            "data_location": {
                "is_frozen": bool(getattr(sys, "frozen", False)),
                "data_dir": str(DATA_DIR),
                "config_path": str(CONFIG_PATH),
                "exe_dir": str(EXE_DIR),
                "migration": self.data_migration,
            },
            "resolved_workspace_dir": str(self.config.resolve_workspace_dir()),
        }

    def list_skill_dirs(self) -> dict[str, Any]:
        configured = self.config.get_skills_dirs()
        resolved = []
        for raw in configured:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = (APP_DIR / path).resolve()
            resolved.append(str(path))
        return {"configured": configured, "resolved": resolved}


APP: NaibaChatApp


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "naiba-chat/1.0"

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
        elif path == "/api/install/dirs":
            self._json(APP.list_skill_dirs())
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
            agent_id = str(body.get("agent_id") or APP.config.default_agent_id())
            interaction_mode = str(body.get("interaction_mode") or "craft")
            permission_mode = str(body.get("permission_mode") or "confirm")
            if interaction_mode not in ("craft", "plan", "ask"):
                self._json({"error": "interaction_mode 必须是 craft / plan / ask"}, HTTPStatus.BAD_REQUEST)
                return
            if permission_mode not in ("confirm", "auto", "full"):
                self._json({"error": "permission_mode 必须是 confirm / auto / full"}, HTTPStatus.BAD_REQUEST)
                return
            self._json(
                APP.storage.create_conversation(
                    title=title, provider_id=provider_id, agent_id=agent_id,
                    interaction_mode=interaction_mode, model_key=model_key,
                    permission_mode=permission_mode,
                ),
                HTTPStatus.CREATED,
            )
        elif path.startswith("/api/conversations/") and path.endswith("/settings"):
            conversation_id = path.split("/")[-2]
            title = body.get("title")
            system_prompt = body.get("system_prompt")
            stream_enabled = body.get("stream_enabled")
            provider_id = body.get("provider_id")
            agent_id = body.get("agent_id")
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
            model_key = body.get("model_key")
            if model_key is not None and not isinstance(model_key, str):
                self._json({"error": "model_key 必须是文本"}, HTTPStatus.BAD_REQUEST)
                return
            interaction_mode = body.get("interaction_mode")
            if interaction_mode is not None:
                if not isinstance(interaction_mode, str) or interaction_mode not in ("craft", "plan", "ask"):
                    self._json({"error": "interaction_mode 必须是 craft / plan / ask"}, HTTPStatus.BAD_REQUEST)
                    return
            permission_mode = body.get("permission_mode")
            if permission_mode is not None:
                if not isinstance(permission_mode, str) or permission_mode not in ("confirm", "auto", "full"):
                    self._json({"error": "permission_mode 必须是 confirm / auto / full"}, HTTPStatus.BAD_REQUEST)
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
            )
            self._json(updated or {"error": "对话不存在"}, HTTPStatus.OK if updated else HTTPStatus.NOT_FOUND)
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
        elif path == "/api/settings":
            try:
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
        elif path == "/api/mcp/reconnect":
            try:
                server_id = str(body.get("server_id") or "").strip()
                result = APP.reconnect_mcp_server(server_id)
                self._json(result)
            except (OSError, ValueError, RuntimeError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/vision/test":
            try:
                self._json(APP.vision.probe())
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "reason": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/search/test":
            try:
                self._json(APP.web_search.probe())
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "reason": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/update/check":
            self._json(APP.updater.start_check(force=True))
        elif path == "/api/update/install":
            try:
                self._json(APP.updater.start_install(APP.update_restart_callback))
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
            self._json({"skills": APP.catalog.scan(), "configured": APP.config.get_skills_dirs()})
        elif path == "/api/chat/cancel":
            run_id = str(body.get("run_id") or "").strip()
            if not run_id:
                self._json({"error": "run_id 不能为空"}, HTTPStatus.BAD_REQUEST)
            else:
                run = APP.runs.cancel(run_id)
                self._json(
                    {"cancelled": bool(run), "run": run},
                    HTTPStatus.OK if run else HTTPStatus.NOT_FOUND,
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
        if path.startswith("/api/conversations/"):
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
        else:
            self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def _authorized(self, parsed: urllib.parse.ParseResult) -> bool:
        expected = str(APP.config.data["access_token"])
        header = self.headers.get("Authorization", "")
        provided = header[7:] if header.startswith("Bearer ") else ""
        if not provided:
            provided = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
        return bool(provided) and secrets.compare_digest(provided, expected)

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
            allowed_roots = [
                APP.config.resolve_workspace_dir(),
                DATA_DIR.resolve(),
            ]
            if not any(path_within(path, root) for root in allowed_roots):
                self._json({"error": "文件不在允许访问的目录中"}, HTTPStatus.FORBIDDEN)
                return
            if not path.is_file():
                self._json({"error": "文件不存在"}, HTTPStatus.NOT_FOUND)
                return
            data = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
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
        target.write_bytes(data)
        self._json({"name": target.name, "path": str(target), "size": len(data)})

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
        """解析 Skill 安装目标目录：必须是已配置的扫描目录（未配置时为默认 skills）。"""
        configured = APP.config.get_skills_dirs()
        dest_raw = str(body.get("dir") or "").strip() or (configured[0] if configured else "skills")
        dest = APP.config._resolve_dir(dest_raw)
        try:
            validate_skills_dir(dest)
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return None
        allowed = {APP.config._resolve_dir(item) for item in configured}
        allowed.add(APP.config._resolve_dir("skills"))
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
        payload: dict[str, Any] = {
            "dir": str(dest),
            "configured": APP.config.get_skills_dirs(),
            "skills": APP.catalog.scan(),
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
            self._json({"ok": True, "response": result})
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
                provider = {**stored, **{key: value for key, value in provider.items() if value}}
        provider["kind"] = "online"
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
        result_pair = APP.runs.confirm_tool(run_id, confirm_id)
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
    STATUS_PATH.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": host,
                "port": port,
                "lan_url": f"http://{get_lan_ip()}:{port}",
                "local_url": f"http://127.0.0.1:{port}",
                "access_token": token,
                "started_at": int(time.time()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def acquire_instance_lock():
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
        handle.close()
        raise RuntimeError("naiba-chat 已经在运行，请勿重复启动") from exc
    return handle


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
    server = ThreadingHTTPServer((host, port), RequestHandler)
    server.daemon_threads = True
    write_status(host, port, str(APP.config.data["access_token"]))
    print("\nnaiba-chat 已启动")
    print(f"手机访问： http://{get_lan_ip()}:{port}")
    print(f"本机访问： http://127.0.0.1:{port}")
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
