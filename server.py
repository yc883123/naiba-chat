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
# 因此区分两类目录：
#   - RESOURCE_DIR：静态资源（public 等），随 exe 打包，运行时从 sys._MEIPASS 读取
#   - APP_DIR：可写运行目录（config.json / data / skills / workspace），用 exe 所在目录
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR)).resolve()
else:
    APP_DIR = Path(__file__).resolve().parent
    RESOURCE_DIR = APP_DIR
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from mcp_runtime import MCPRegistry
from model_runtime import ModelRuntime
from skill_runtime import SkillAgent, SkillCatalog, ToolExecutor
from storage import ChatStorage
from updater import UpdateManager


PUBLIC_DIR = RESOURCE_DIR / "public"
DATA_DIR = APP_DIR / "data"
CONFIG_PATH = APP_DIR / "config.json"
STATUS_PATH = DATA_DIR / "server.json"
LOCK_PATH = DATA_DIR / "server.lock"


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
        "max_agent_steps": 8,
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
            "call_mcp",
        ],
        "command_timeout": 120,
        "providers": [],
        # MCP 服务默认不注册：ComfyUI 生图等能力以"可选 MCP skill"形式按需添加，
        # 主程序本身不依赖 ComfyUI。需要时在设置中手动添加对应 MCP 服务。
        "mcp_servers": [],
    }


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
        self.data = defaults
        self.save()

    def save(self) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)

    def public(self) -> dict[str, Any]:
        with self.lock:
            return {
                key: value
                for key, value in self.data.items()
                if key not in {"access_token", "providers", "mcp_servers"}
            }

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
            "max_agent_steps",
            "agent_system_prompt",
            "permission_mode",
            "agent_tools",
            "command_timeout",
            "access_token",
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
                            "run_command", "run_skill_script", "http_request", "call_mcp",
                        }
                        requested = values[key] if isinstance(values[key], list) else []
                        self.data[key] = [tool for tool in requested if tool in valid_tools]
                    else:
                        self.data[key] = values[key]
            self.save()
            return self.public()

    def upsert_provider(self, values: dict[str, Any]) -> dict[str, Any]:
        provider_id = str(values.get("id") or uuid.uuid4().hex[:12])
        request_format = str(values.get("request_format") or "openai_chat")
        valid_formats = {"openai_chat", "codex_responses", "gemini", "claude", "lm_studio"}
        if request_format not in valid_formats:
            raise ValueError("不支持的请求格式")
        with self.lock:
            providers = self.data.setdefault("providers", [])
            existing = next((item for item in providers if item.get("id") == provider_id), None)
            payload = {
                "id": provider_id,
                "name": str(values.get("name") or "在线模型").strip(),
                "base_url": str(values.get("base_url") or "").strip().rstrip("/"),
                "model": str(values.get("model") or "").strip(),
                "api_key": str(values.get("api_key") or "").strip(),
                "request_format": request_format,
            }
            if not payload["base_url"] or not payload["model"]:
                raise ValueError("API URL 和模型名称不能为空")
            if existing and not payload["api_key"]:
                payload["api_key"] = existing.get("api_key", "")
            if existing:
                existing.update(payload)
            else:
                providers.append(payload)
            self.save()
        return {**payload, "api_key": "", "has_api_key": bool(payload["api_key"])}

    def delete_provider(self, provider_id: str) -> bool:
        with self.lock:
            providers = self.data.setdefault("providers", [])
            before = len(providers)
            self.data["providers"] = [item for item in providers if item.get("id") != provider_id]
            if self.data.get("provider_id") == provider_id:
                self.data["provider_id"] = ""
            self.save()
            return len(self.data["providers"]) < before

    def provider_secret(self, provider_id: str) -> str | None:
        with self.lock:
            provider = next(
                (item for item in self.data.get("providers", []) if item.get("id") == provider_id),
                None,
            )
            return str(provider.get("api_key") or "") if provider else None

    def profile(self, selection: str = "") -> dict[str, Any]:
        with self.lock:
            selected = selection
            if selected.startswith("online:"):
                provider_id = selected[7:]
            else:
                provider_id = self.data.get("provider_id", "")
            provider = next(
                (item for item in self.data.get("providers", []) if item.get("id") == provider_id),
                None,
            )
            if not provider:
                raise ValueError("找不到选择的在线模型配置")
            return {"kind": "online", **provider}

    def generation_options(self) -> dict[str, Any]:
        with self.lock:
            return {
                key: self.data[key]
                for key in ("temperature", "max_tokens")
            }


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


def encode_image_for_model(source: str) -> dict[str, str] | None:
    path = Path(source).expanduser().resolve()
    media_type = IMAGE_MEDIA_TYPES.get(path.suffix.lower())
    if not media_type or not path.is_file() or path.stat().st_size > 30 * 1024 * 1024:
        return None
    raw = path.read_bytes()
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(raw)) as opened:
            image = ImageOps.exif_transpose(opened)
            needs_conversion = max(image.size) > 2048 or len(raw) > 5 * 1024 * 1024 or image.format == "GIF"
            if needs_conversion:
                image.thumbnail((2048, 2048))
                if image.mode not in {"RGB", "L"}:
                    background = Image.new("RGB", image.size, "white")
                    if "A" in image.getbands():
                        background.paste(image, mask=image.getchannel("A"))
                    else:
                        background.paste(image)
                    image = background
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=85, optimize=True)
                raw = buffer.getvalue()
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


class NaibaChatApp:
    def __init__(self):
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
            Path(self.config.data["workspace_dir"]).resolve(),
            sys.executable,
            int(self.config.data.get("command_timeout", 120)),
            self.mcp,
            permission_mode=self.config.data.get("permission_mode", "confirm"),
        )
        self.agent = SkillAgent(self.catalog, self.executor, self.models.complete)
        self.updater = UpdateManager(APP_DIR, DATA_DIR)
        self.update_restart_callback = None

    def stop(self) -> None:
        self.mcp.stop()

    def bootstrap(self) -> dict[str, Any]:
        return {
            "settings": self.config.public(),
            "providers": self.config.public_providers(),
            "skills": self.catalog.scan(),
            "mcp_servers": self.mcp.states(),
            "lan_url": f"http://{get_lan_ip()}:{self.config.data['port']}",
            "update": self.updater.status(),
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
        elif path.startswith("/api/providers/") and path.endswith("/secret"):
            provider_id = path.split("/")[-2]
            api_key = APP.config.provider_secret(provider_id)
            self._json(
                {"api_key": api_key} if api_key is not None else {"error": "供应商不存在"},
                HTTPStatus.OK if api_key is not None else HTTPStatus.NOT_FOUND,
            )
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
            self._json(APP.storage.create_conversation(title=title), HTTPStatus.CREATED)
        elif path.startswith("/api/conversations/") and path.endswith("/settings"):
            conversation_id = path.split("/")[-2]
            system_prompt = body.get("system_prompt")
            stream_enabled = body.get("stream_enabled")
            if system_prompt is not None and not isinstance(system_prompt, str):
                self._json({"error": "system_prompt 必须是文本"}, HTTPStatus.BAD_REQUEST)
                return
            if stream_enabled is not None and not isinstance(stream_enabled, bool):
                self._json({"error": "stream_enabled 必须是布尔值"}, HTTPStatus.BAD_REQUEST)
                return
            updated = APP.storage.update_conversation_settings(
                conversation_id,
                system_prompt=system_prompt,
                stream_enabled=stream_enabled,
            )
            self._json(updated or {"error": "对话不存在"}, HTTPStatus.OK if updated else HTTPStatus.NOT_FOUND)
        elif path == "/api/providers":
            try:
                self._json(APP.config.upsert_provider(body))
            except Exception as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/providers/test":
            self._test_provider(body)
        elif path == "/api/providers/models":
            self._provider_models(body)
        elif path == "/api/settings":
            try:
                settings = APP.config.update_settings(body)
                APP.executor.command_timeout = int(APP.config.data.get("command_timeout", 120))
                APP.executor.set_permission_mode(str(APP.config.data.get("permission_mode", "confirm")))
                self._json({"settings": settings})
            except (OSError, ValueError) as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        elif path == "/api/update/check":
            self._json(APP.updater.check(force=True))
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
        elif path == "/api/chat":
            self._chat(body)
        elif path == "/api/tool/confirm":
            self._confirm_tool(body)
        elif path == "/api/tool/reject":
            self._reject_tool(body)
        elif path == "/api/messages/edit":
            self._edit_message(body)
        else:
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
                Path(APP.config.data["workspace_dir"]).expanduser().resolve(),
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
        self.send_header("Content-Type", content_type)
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
            provider = self._provider_profile(body)
            result = APP.models.complete(
                provider,
                [
                    {"role": "system", "content": "你是连接测试助手。直接回答，不要调用工具。"},
                    {"role": "user", "content": "只回复 OK"},
                ],
                {"temperature": 0, "max_tokens": 1024},
            )
            self._json({"ok": True, "response": result})
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def _provider_models(self, body: dict[str, Any]) -> None:
        try:
            provider = self._provider_profile(body)
            self._json({"models": APP.models.list_online_models(provider)})
        except Exception as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

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
        conversation_id = str(body.get("conversation_id") or "")
        message = str(body.get("message") or "").strip()
        uploads = body.get("attachments") or []
        if not conversation_id or not message:
            self._json({"error": "conversation_id 和 message 不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        conversation = APP.storage.get_conversation(conversation_id)
        if not conversation:
            self._json({"error": "对话不存在"}, HTTPStatus.NOT_FOUND)
            return
        if uploads:
            upload_lines = [f"[用户上传文件：{item.get('path')}]" for item in uploads if item.get("path")]
            effective_message = message + "\n" + "\n".join(upload_lines)
        else:
            effective_message = message
        APP.storage.add_message(conversation_id, "user", message, {"attachments": uploads})
        conversation_messages = APP.storage.get_conversation(conversation_id)["messages"]
        recent_image_paths: list[str] = []
        for item in reversed(conversation_messages[-16:]):
            if item["role"] != "user":
                continue
            for attachment in reversed((item.get("metadata") or {}).get("attachments") or []):
                source = str(attachment.get("path") or "")
                if Path(source).suffix.lower() in IMAGE_MEDIA_TYPES and source not in recent_image_paths:
                    recent_image_paths.append(source)
                    if len(recent_image_paths) >= 3:
                        break
            if len(recent_image_paths) >= 3:
                break
        selected_images = set(recent_image_paths)

        history = []
        for item in conversation_messages:
            if item["role"] not in {"user", "assistant"}:
                continue
            content = item["content"]
            previous_uploads = (item.get("metadata") or {}).get("attachments") or []
            if item["role"] == "user" and previous_uploads:
                paths = [f"[用户上传文件：{upload.get('path')}]" for upload in previous_uploads if upload.get("path")]
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

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        client_connected = True

        def event(payload: dict[str, Any]) -> None:
            nonlocal client_connected
            if not client_connected:
                return
            try:
                self.wfile.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                client_connected = False

        try:
            model_key = str(body.get("model_key") or "")
            profile = APP.config.profile(model_key)
            if model_key.startswith("online:"):
                provider_id = model_key[7:]
                if provider_id and APP.config.data.get("provider_id") != provider_id:
                    APP.config.update_settings({"provider_id": provider_id})
            options = APP.config.generation_options()
            options["stream"] = bool(conversation.get("stream_enabled", 1))
            selected_ids = [str(item) for item in body.get("skill_ids", [])]
            auto_skills = bool(body.get("auto_skills", False))

            def logger(tool: str, arguments: dict[str, Any], result: str, success: bool) -> None:
                APP.storage.log_tool_run(conversation_id, tool, arguments, result, success)

            allowed_tools = [str(tool) for tool in APP.config.data.get("agent_tools", [])]

            global_prompt = str(APP.config.data.get("agent_system_prompt", ""))
            conversation_prompt = str(conversation.get("system_prompt") or "").strip()
            combined_prompt = "\n\n".join(item for item in (global_prompt.strip(), conversation_prompt) if item)
            response, runs, reasonings, usage = APP.agent.run(
                effective_message,
                history,
                profile,
                options,
                auto_skills,
                selected_ids,
                int(APP.config.data.get("max_agent_steps", 8)),
                combined_prompt,
                allowed_tools,
                event,
                logger,
            )
            attachments = extract_attachments(runs)
            choice_groups = _detect_choice_groups(response)
            choices = choice_groups[0]["choices"] if choice_groups else []
            metadata = {
                "tool_runs": runs,
                "attachments": attachments,
                "reasoning": reasonings,
                "usage": usage,
                "choices": choices,
                "choice_groups": choice_groups,
            }
            saved = APP.storage.add_message(conversation_id, "assistant", response, metadata)
            if choices:
                event({"type": "choice", "choices": choices, "choice_groups": choice_groups})
            event({"type": "done", "message": saved})
        except Exception as exc:
            traceback.print_exc()
            error_message = str(exc)
            try:
                APP.storage.add_message(
                    conversation_id,
                    "error",
                    f"请求失败：{error_message}",
                    {"error": True},
                )
            except Exception:
                traceback.print_exc()
            event({"type": "error", "message": error_message})

    def _confirm_tool(self, body: dict[str, Any]) -> None:
        confirm_id = str(body.get("confirm_id") or "").strip()
        if not confirm_id:
            self._json({"error": "confirm_id 不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        success, result = APP.executor.confirm_execute(confirm_id)
        self._json({"success": success, "result": result})

    def _reject_tool(self, body: dict[str, Any]) -> None:
        confirm_id = str(body.get("confirm_id") or "").strip()
        if not confirm_id:
            self._json({"error": "confirm_id 不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        success, result = APP.executor.reject_execute(confirm_id)
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
