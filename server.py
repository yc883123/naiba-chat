from __future__ import annotations

from naiba.core.contracts import RunContext

import argparse
import base64
import hashlib
import gzip
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
import struct
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

# 路径语义与目录分类见 naiba/paths.py：此处构建进程级 PathContext 并暴露同名模块级
# 常量（与既有引用面保持同名，语义不变；运行时二次重绑定见 NaibaChatApp.__init__）。
from naiba.paths import PathContext, default_path_context, static_asset_version

PC: PathContext = default_path_context()
EXE_DIR = PC.exe_dir
RESOURCE_DIR = PC.resource_dir
APP_DIR = PC.app_dir
PUBLIC_DIR = PC.public_dir
CONFIG_PATH = PC.config_path
DATA_DIR = PC.data_dir
STATUS_PATH = PC.status_path
LOCK_PATH = PC.lock_path

import net_io
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
from naiba.core.attachments import (
    MEDIA_PRODUCT_EXTS, _IMAGE_MEDIA_TERM_RE, _IMAGE_VIEW_ACTION_RE,
    _image_intent, _is_media_product_path, extract_attachments,
)
from naiba.core.file_changes import FILE_MODIFY_TOOLS, file_changes_from_runs
from naiba.core.conv_files import (
    _CONV_FILE_READ_CAP, _CONV_FILE_SAVE_CAP, _CONV_FILE_SNIFF_BYTES, _CONV_IMAGE_EXTS,
    _conv_file_allow, _conv_file_open, _conv_file_save, _conv_file_target,
    _conv_touched_files, _conv_workspace_root,
)
from naiba.core.history import (
    CONTENT_READ_TOOLS, IMAGE_MEDIA_TYPES, MODEL_IMAGE_HISTORY_LIMIT,
    MODEL_IMAGE_MAX_EDGE, MODEL_IMAGE_TARGET_BYTES, _content_read_tool_outputs,
    _copy_model_trace_message, _debug_replay_digest, build_model_history, encode_image_for_model,
)
from naiba.core.choices import _detect_choice_groups, _detect_choices
from naiba.core.paths import path_within
from naiba.storage.media import (
    _clean_uploads_cache, _ensure_webp_thumb, _fit_image_pixels, _image_cache_dirs,
    _process_uploaded_image, _thumb_webp_path, _uploads_total_bytes,
    IMAGE_CACHE_CLEAN_LIMIT, IMAGE_SUFFIXES,
)
from naiba.core.diagnostics import CACHE_DEBUG_ON, _cache_debug_enabled


from naiba.core.cards import _decode_card_payload, parse_sillytavern_card
from naiba.core.migration import (
    _config_has_providers, _copy_legacy_data, _database_has_conversations,
    _merge_data_tree, _sync_bundled_skills, migrate_legacy_data,
)
from naiba.core.network import _is_usable_lan_ipv4, get_lan_ip, network_access_status
from naiba.app import NaibaChatApp
from naiba.http import AppHTTPServer, RequestHandler
from naiba.config import (
    ConfigStore,
    VALID_MODEL_KINDS,
    _context_window_source,
    _infer_context_window,
    _infer_kind_for_request_format,
    _infer_supports_images,
    built_in_agent_ids,
    built_in_agents,
    default_config,
    resolve_tool_preset,
    tool_catalog_entries,
    tool_group_entries,
    tool_preset_entries,
    validate_skills_dir,
)


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

    global APP, DATA_DIR, STATUS_PATH, LOCK_PATH
    APP = NaibaChatApp(paths=PC)
    # 构造后同步路径全局（data_dir 可能被配置重绑定）
    DATA_DIR = PC.data_dir
    STATUS_PATH = PC.status_path
    LOCK_PATH = PC.lock_path
    host = args.host or str(APP.config.data.get("host", "0.0.0.0"))
    port = args.port or int(APP.config.data.get("port", 8765))
    APP.config.data["host"] = host
    APP.config.data["port"] = port
    APP.config.save()
    APP.listener_host = host
    server = AppHTTPServer((host, port), RequestHandler, APP)
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
