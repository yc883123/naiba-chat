"""Skill 目录扫描/缓存/引用（SkillCatalog 整类 + frontmatter/display 辅助，原样搬移）。"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("naiba.skill_runtime")


EventCallback = Callable[[dict[str, Any]], None]

SKILL_POLICY_MODES = {"auto", "pinned", "exclusive"}



# Conservative context ceiling (tokens) used when a provider exposes no window
# (e.g. DeepSeek's /v1/models returns no context-length field, so auto-detection
# yields 0). Rather than silently truncating history — which both drops context
# and re-breaks DeepSeek's token-prefix cache every turn — a conversation is
# blocked with a user-visible notice once it reaches this bound.



def _frontmatter_value(text: str, key: str) -> str:
    match = re.search(rf"(?m)^{re.escape(key)}:\s*(.*)$", text)
    if not match:
        return ""
    value = match.group(1).strip().strip("'\"")
    if value not in {"|", ">"}:
        return value
    lines = []
    for line in text[match.end() :].splitlines()[1:]:
        if line and not line[0].isspace():
            break
        if line.strip():
            lines.append(line.strip())
    return " ".join(lines)


def _skill_display_name(skill_file: Path) -> str:
    """Read the optional UI title without adding a YAML runtime dependency."""
    if skill_file.name != "SKILL.md":
        return ""
    metadata_file = skill_file.parent / "agents" / "openai.yaml"
    try:
        metadata = metadata_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    match = re.search(r"(?m)^\s*display_name:\s*(.*?)\s*$", metadata)
    return match.group(1).strip().strip("'\"") if match else ""


class SkillCatalog:
    def __init__(
        self,
        directories: list[Path],
        base_dir: Path | None = None,
        hidden_ids: list[str] | set[str] | None = None,
    ):
        self.base_dir = base_dir or Path.cwd()
        self.directories = [self._resolve(directory) for directory in directories]
        self.hidden_ids = {str(item) for item in (hidden_ids or [])}
        self._scan_cache: list[dict[str, Any]] | None = None
        self._scan_signature: tuple[Any, ...] | None = None
        self._scan_lock = threading.RLock()
        self._content_cache: dict[str, tuple[int, int, str]] = {}
        self._content_lock = threading.RLock()

    def _resolve(self, directory: Path) -> Path:
        directory = Path(directory).expanduser()
        if not directory.is_absolute():
            directory = (self.base_dir / directory).resolve()
        return directory

    def add_directory(self, directory: Path) -> Path:
        resolved = self._resolve(directory)
        if resolved not in self.directories:
            self.directories.append(resolved)
            self._scan_cache = None
        return resolved

    def remove_directory(self, directory: Path) -> None:
        resolved = self._resolve(directory)
        self.directories = [item for item in self.directories if item != resolved]
        self._scan_cache = None

    @staticmethod
    def _iter_skill_files(directory: Path) -> list[Path]:
        """收集 skill 定义文件：优先 SKILL.md；目录下无 SKILL.md 时回退识别该目录唯一的 .md 文件。"""
        all_md = [p for p in directory.rglob("*.md") if p.is_file()]
        skill_md_dirs = {p.parent for p in all_md if p.name == "SKILL.md"}
        candidates: list[Path] = []
        for p in all_md:
            if p.name == "SKILL.md":
                candidates.append(p)
                continue
            # Documentation below a real Skill root (notably references/*.md)
            # belongs to that Skill and must never become a second fallback Skill.
            if any(root == p.parent or root in p.parent.parents for root in skill_md_dirs):
                continue
            siblings = [q for q in all_md if q.parent == p.parent]
            if len(siblings) == 1:
                candidates.append(p)
        return candidates

    def scan(self) -> list[dict[str, Any]]:
        # Skill discovery is read-only but can run on every agent turn. Cache
        # by file metadata so ordinary chat does not repeatedly parse every
        # Skill while still noticing edits, installs, and removals promptly.
        signature_rows: list[tuple[str, int, int]] = []
        skill_files_by_directory: list[tuple[Path, list[Path]]] = []
        for directory in self.directories:
            if not directory.exists():
                continue
            skill_files = self._iter_skill_files(directory)
            skill_files_by_directory.append((directory, skill_files))
            for skill_file in skill_files:
                try:
                    stat = skill_file.stat()
                except OSError:
                    continue
                signature_rows.append((os.path.normcase(str(skill_file)), stat.st_mtime_ns, stat.st_size))
                metadata_file = skill_file.parent / "agents" / "openai.yaml"
                try:
                    metadata_stat = metadata_file.stat()
                except OSError:
                    pass
                else:
                    signature_rows.append((
                        os.path.normcase(str(metadata_file)),
                        metadata_stat.st_mtime_ns,
                        metadata_stat.st_size,
                    ))
        signature = (tuple(sorted(signature_rows)), tuple(sorted(self.hidden_ids)))
        with self._scan_lock:
            if self._scan_cache is not None and signature == self._scan_signature:
                return [dict(item) for item in self._scan_cache]
        found: dict[str, dict[str, Any]] = {}
        seen_files: set[str] = set()
        files_by_directory = {directory: files for directory, files in skill_files_by_directory}
        for directory_index, directory in enumerate(self.directories):
            # 目录 0 为内置（bundled）Skill 目录；目录 1 为应用托管（安装目标）目录；
            # 其余为用户额外添加的扫描目录（外部）。
            if directory_index == 0:
                directory_source = "builtin"
            elif directory_index == 1:
                directory_source = "managed"
            else:
                directory_source = "external"
            if not directory.exists():
                continue
            for skill_file in files_by_directory.get(directory, []):
                file_key = os.path.normcase(str(skill_file))
                if file_key in seen_files:
                    continue
                seen_files.add(file_key)
                if any(part.startswith(".") or part == "_template" for part in skill_file.parts):
                    continue
                try:
                    text = self.read_skill_content(skill_file)
                except (OSError, UnicodeError):
                    continue
                declared_name = _frontmatter_value(text, "name") or skill_file.parent.name
                name = _skill_display_name(skill_file) or declared_name
                # ref：/ 索引引用用的“可手输、无空白”别名。来自声明标识（frontmatter name
                # 或目录名），把连续空白折叠成单个连字符，保证能在输入框里手输；与展示用名字
                # name 分开，避免 display_name 带空格或重名破坏引用解析。
                ref = re.sub(r"\s+", "-", declared_name).strip("-") or declared_name
                description = _frontmatter_value(text, "description")
                declared_mcp = (
                    _frontmatter_value(text, "requires_mcp")
                    or _frontmatter_value(text, "requires-mcp")
                    or _frontmatter_value(text, "mcp_servers")
                ).lower()
                declared_mcp_servers = [
                    item.strip().strip("[]\"'")
                    for item in re.split(r"[,\s]+", declared_mcp)
                    if item.strip().strip("[]\"'")
                ] if declared_mcp else []
                mcp_signals = f"{declared_name} {name} {description} {skill_file.parent.name}".lower()
                requires_mcp = (
                    declared_mcp in {"1", "true", "yes", "required"}
                    or "mcp" in mcp_signals
                    or "mcp__" in text
                )
                try:
                    stable_path = skill_file.relative_to(directory)
                except ValueError:
                    stable_path = skill_file
                # Keep the stable id tied to the declared Skill identifier, not
                # to the user-facing title, which may change or be translated.
                identity = f"{declared_name}/{stable_path}".replace("\\", "/").lower()
                skill_id = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
                if skill_id in self.hidden_ids:
                    continue
                scripts_dir = skill_file.parent / "scripts"
                script_count = sum(1 for item in scripts_dir.rglob("*") if item.is_file()) if scripts_dir.exists() else 0
                found[skill_id] = {
                    "id": skill_id,
                    "name": name,
                    "ref": ref,
                    "description": description or "未提供描述",
                    "char_count": len(text),
                    "path": str(skill_file),
                    "root": str(skill_file.parent),
                    "script_count": script_count,
                    "requires_mcp": requires_mcp,
                    "mcp_servers": declared_mcp_servers,
                    "source": directory_source,
                }
        result = sorted(found.values(), key=lambda item: item["name"].lower())
        with self._scan_lock:
            self._scan_signature = signature
            self._scan_cache = [dict(item) for item in result]
        return result

    def read_skill_content(self, path: str | Path) -> str:
        """Read one Skill body with metadata-aware in-memory caching."""
        skill_path = Path(path).expanduser().resolve()
        stat = skill_path.stat()
        key = os.path.normcase(str(skill_path))
        with self._content_lock:
            cached = self._content_cache.get(key)
            if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
                return cached[2]
        text = skill_path.read_text(encoding="utf-8", errors="replace")
        with self._content_lock:
            self._content_cache[key] = (stat.st_mtime_ns, stat.st_size, text)
        return text

    def by_id(self) -> dict[str, dict[str, Any]]:
        return {skill["id"]: skill for skill in self.scan()}


