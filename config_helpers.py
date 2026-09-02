"""配置与数据迁移辅助：default_config、旧数据迁移、模型常量。

从 server.py 拆出的纯函数集。路径全局统一从 app_state 读取。
"""
from __future__ import annotations

import json
import secrets
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import app_state

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


def _merge_data_tree(source: Path, target: Path) -> bool:
    """Copy a data tree recursively, keeping files already present at target."""
    changed = False
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        destination = target / item.name
        if item.is_dir():
            changed = _merge_data_tree(item, destination) or changed
        elif not destination.exists():
            shutil.copy2(item, destination)
            changed = True
    return changed


def _sync_bundled_skills(source: Path, target: Path) -> bool:
    """Refresh packaged Skill files without deleting older persisted Skills."""
    changed = False
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        destination = target / item.name
        if item.is_dir():
            changed = _sync_bundled_skills(item, destination) or changed
            continue
        try:
            needs_copy = not destination.is_file() or item.read_bytes() != destination.read_bytes()
        except OSError:
            needs_copy = True
        if needs_copy:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)
            changed = True
    return changed


def _copy_legacy_data(source: Path, replace_empty_target: bool = True) -> dict[str, bool]:
    """Merge a legacy install into the current data directory, including all subdirectories."""
    report = {"config": False, "data": False, "skills": False}
    legacy_config = source / "config.json"
    legacy_data = source / "data"
    app_state.APP_DIR.mkdir(parents=True, exist_ok=True)

    if legacy_config.is_file() and (
        not app_state.CONFIG_PATH.exists() or not _config_has_providers(app_state.CONFIG_PATH)
    ):
        shutil.copy2(legacy_config, app_state.CONFIG_PATH)
        report["config"] = True

    if not legacy_data.is_dir():
        return report
    app_state.DATA_DIR.mkdir(parents=True, exist_ok=True)
    source_db = legacy_data / "chat.db"
    target_db = app_state.DATA_DIR / "chat.db"
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
        report["data"] = _merge_data_tree(item, app_state.DATA_DIR / item.name) or report["data"]

    # 导入旧安装根目录的 Skills（旧版 app_state.APP_DIR/skills 或数据目录同级 skills）到新托管目录。
    managed_skills = (app_state.DATA_DIR / "skills").resolve()
    for legacy_skills in (
        (source / "skills").resolve(),
        (source.parent / "skills").resolve(),
        (legacy_data / "skills").resolve(),
    ):
        if legacy_skills.is_dir() and legacy_skills != managed_skills:
            report["skills"] = _merge_data_tree(legacy_skills, managed_skills) or report["skills"]
    return report


def migrate_legacy_data() -> dict[str, Any]:
    """冻结版首次启动：从 EXE 相邻旧目录迁移 config.json 与 data/ 到数据目录。

    仅当数据目录（%LOCALAPPDATA%\\NaibaChat）尚未初始化时执行；旧文件保留，
    不覆盖已存在的新数据。源码模式（app_state.APP_DIR == app_state.EXE_DIR）跳过。
    """
    report: dict[str, Any] = {"migrated": False, "config": False, "data": False, "source": ""}
    if str(app_state.APP_DIR) == str(app_state.EXE_DIR):
        return report
    legacy_config = app_state.EXE_DIR / "config.json"
    legacy_data = app_state.EXE_DIR / "data"
    if not legacy_config.is_file() and not legacy_data.is_dir():
        return report
    try:
        migrated = _copy_legacy_data(app_state.EXE_DIR)
        report.update(migrated)
    except OSError as exc:
        print(f"迁移旧数据失败：{exc}")
    if report["config"] or report["data"]:
        report["migrated"] = True
        report["source"] = str(app_state.EXE_DIR)
    return report


def default_config() -> dict[str, Any]:
    return {
        "host": "0.0.0.0",
        "port": 8765,
        "access_token": f"{secrets.randbelow(1000000):06d}",
        "skills_dirs": ["skills"],
        "hidden_skill_ids": [],
        "workspace_dir": "workspace",
        "data_dir": "data",
        "workspaces": [],
        "provider_id": "",
        # Deprecated compatibility fields. They are retained for old config
        # files but are never used to build model requests.
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
            "pwsh",
            "run_skill_script",
            "http_request",
        ],
        "command_timeout": 120,
        # 图片缓存：image_upload_original=True 按原尺寸存；False 则超过 image_max_pixels
        # 时用 Lanczos 压缩。缩略图始终从保存后的主图按 thumbnail_max_pixels 生成 WebP（_thumb.webp）。
        "imaging": {
            "image_upload_original": False,
            "image_max_pixels": 2000000,
            "thumbnail_max_pixels": 500000,
        },
        "providers": [],
        # MCP 服务默认不注册；只有用户显式配置并授权时才可连接。
        "mcp_servers": [],
        # 多 Agent 定义：每个 Agent 有独立的预设/规则（system_prompt）与固定 Skill（skill_ids）。
        "agents": [
            {
                "id": "general",
                "name": "通用 Agent",
                "system_prompt": "",
                # Domain Skills are routed from the current request or
                # explicitly selected by the user; do not inject them into
                # every general-agent turn.
                "skill_ids": [],
            },
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
            "timeout_ms": 180000,
            "cache": True,
            "cache_ttl_seconds": 3600,
            "cache_max_entries": 200,
            "max_images": 4,
        },
        # 联网搜索（PLAN4 §联网搜索）：完全可选；endpoint/Key/模型/启用状态由用户配置。
        "search": {
            "provider_id": "",
            "profiles": [],
            "endpoint": "",
            "api_key": "",
            "model": "",
            "max_results": 5,
        },
    }


# 在线请求协议集合；llama.cpp 提供 OpenAI 兼容接口，但服务进程仍在本机。
ONLINE_REQUEST_FORMATS = {"openai_chat", "codex_responses", "gemini", "claude"}
LOCAL_REQUEST_FORMATS = {"ollama", "lm_studio", "llama_cpp", "unsloth"}
VALID_MODEL_KINDS = {"online", "local"}
VALID_LOCAL_BACKENDS = {"ollama", "lm_studio", "llama_cpp", "unsloth"}


def _infer_kind_for_request_format(request_format: str) -> str:
    """根据请求格式推断模型类别，兼容未携带 kind 的旧配置。"""
    return "local" if request_format in LOCAL_REQUEST_FORMATS else "online"
