from __future__ import annotations

from naiba.core.contracts import RunContext

import hashlib
import concurrent.futures
import json
import logging
import os
import re
import shutil
import zipfile
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

import net_io
from mcp_runtime import MCPRegistry
from naiba.core.diagnostics import _cache_debug_enabled, _debug_message_digest
from naiba.core.history import _vision_read_folder_model_summary, encode_image_for_model
from naiba.core.exceptions import TaskCancelled
from naiba.tools.executor import ToolExecutor
from naiba.skills.agent import SKILL_CONTENT_WARN_CHARS, SKILL_PROMPT_HEADER, SkillAgent
from naiba.skills.context import DEFAULT_CONTEXT_WINDOW
from naiba.skills.policy import SKILL_POLICY_MODES, normalize_skill_policy

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
                    or "call_mcp" in text
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


# --------------------------------------------------------------------------
# Skill import (folder / ZIP / single .md) validation + recoverable delete
# --------------------------------------------------------------------------

# Validation constants
MAX_FILE_COUNT = 2000
MAX_TOTAL_SIZE = 50 * 1024 * 1024          # 50 MB
ZIP_BOMB_RATIO = 100                        # uncompressed > 100x compressed
MAX_UNCOMPRESSED_ENTRY = 50 * 1024 * 1024  # 50 MB single entry


class _SkillInstallError(RuntimeError):
    pass


def _path_within(path: Any, root: Any) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _unique_dir(parent: Path, base_name: str) -> Path:
    base = re.sub(r"[^\w.\-]+", "_", str(base_name).strip()) or "skill"
    candidate = parent / base
    if not candidate.exists():
        return candidate
    index = 2
    while (parent / f"{base}_{index}").exists():
        index += 1
    return parent / f"{base}_{index}"


def _folder_has_skill_md(directory: Path) -> bool:
    """SKILL.md present at top level or exactly one level down."""
    if (directory / "SKILL.md").is_file():
        return True
    for child in directory.iterdir():
        if child.is_dir() and (child / "SKILL.md").is_file():
            return True
    return False


def _zip_has_skill_md(archive: zipfile.ZipFile) -> bool:
    for info in archive.infolist():
        parts = Path(info.filename).parts
        if len(parts) in (1, 2) and parts[-1] == "SKILL.md":
            return True
    return False


def _finalize_install(dest: Path, display_name: str | None = None) -> dict[str, Any]:
    catalog = SkillCatalog([dest])
    skills = catalog.scan()
    if not skills:
        raise _SkillInstallError("未能在来源中识别到有效的 Skill 定义")
    skill = skills[0]
    return {
        "success": True,
        "skill_id": skill["id"],
        "name": skill["name"],
        "path": skill["path"],
        "source": "managed",
        "error": None,
    }


def _install_folder(src: Path, managed_dir: Path, name: str | None) -> dict[str, Any]:
    file_count = 0
    total = 0
    for item in src.rglob("*"):
        if item.is_file():
            file_count += 1
            total += item.stat().st_size
    if file_count > MAX_FILE_COUNT:
        raise _SkillInstallError(f"文件夹内文件数量过多（超过 {MAX_FILE_COUNT}）")
    if total > MAX_TOTAL_SIZE:
        raise _SkillInstallError("文件夹总大小超过 50 MB")
    if not _folder_has_skill_md(src):
        raise _SkillInstallError("文件夹缺少 SKILL.md（需位于顶层或下一级目录）")
    dest = _unique_dir(managed_dir, name or src.name)
    shutil.copytree(src, dest)
    return _finalize_install(dest, name)


def _install_zip(src: Path, managed_dir: Path, name: str | None) -> dict[str, Any]:
    try:
        archive = zipfile.ZipFile(src)
    except zipfile.BadZipFile as exc:
        raise _SkillInstallError(f"不是有效的 zip 压缩包：{exc}")
    with archive:
        bad = archive.testzip()
        if bad is not None:
            raise _SkillInstallError(f"压缩包损坏：{bad}")
        members = archive.infolist()
        if len(members) > MAX_FILE_COUNT:
            raise _SkillInstallError(f"压缩包内文件数量过多（超过 {MAX_FILE_COUNT}）")
        total_uncompressed = sum(member.file_size for member in members)
        if total_uncompressed > MAX_TOTAL_SIZE:
            raise _SkillInstallError("压缩包解压后体积过大（超过 50 MB）")
        compressed = src.stat().st_size
        if compressed > 0 and total_uncompressed > ZIP_BOMB_RATIO * compressed:
            raise _SkillInstallError("检测到可能的 zip 炸弹（解压体积远超压缩体积）")
        for member in members:
            if member.file_size > MAX_UNCOMPRESSED_ENTRY:
                raise _SkillInstallError(f"压缩包单文件解压后过大（超过 50 MB）：{member.filename}")
            filename = member.filename
            parts = Path(filename).parts
            if (
                Path(filename).is_absolute()
                or filename.startswith("/")
                or ".." in parts
                or any(":" in part for part in parts)
            ):
                raise _SkillInstallError(f"压缩包包含非法或越界路径：{filename}")
        if not _zip_has_skill_md(archive):
            raise _SkillInstallError("压缩包缺少 SKILL.md（需位于顶层或下一级目录）")
        dest = _unique_dir(managed_dir, name or src.stem)
        dest.mkdir(parents=True, exist_ok=True)
        for member in members:
            target = (dest / member.filename).resolve()
            if target != dest and not _path_within(target, dest):
                raise _SkillInstallError(f"压缩包包含越界路径：{member.filename}")
        archive.extractall(dest)
    return _finalize_install(dest, name)


def _install_single_md(src: Path, managed_dir: Path, name: str | None) -> dict[str, Any]:
    try:
        text = src.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _SkillInstallError(f"无法读取 .md 文件：{exc}")
    md_name = _frontmatter_value(text, "name")
    md_desc = _frontmatter_value(text, "description")
    if not md_name or not md_desc:
        raise _SkillInstallError("单个 .md 必须包含有效的 YAML frontmatter，且同时具备 name 与 description 字段")
    dest = _unique_dir(managed_dir, name or md_name or src.stem)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(text, encoding="utf-8")
    return _finalize_install(dest, name or md_name)


def validate_and_install_skill(
    source_path: Any,
    managed_dir: Any,
    name: str | None = None,
) -> dict[str, Any]:
    """校验并安装一个 Skill 来源（文件夹 / ZIP / 单个 .md）。

    Args:
        source_path: 本地来源路径（文件夹、.zip 或 .md）。
        managed_dir: 应用托管的 skills 目录，安装目标。
        name: 可选的目标目录名覆盖。

    Returns:
        {"success": True, "skill_id", "name", "path", "source": "managed", "error": None}
        或 {"success": False, "error": str}
    """
    src = Path(source_path).expanduser().resolve()
    managed = Path(managed_dir).expanduser().resolve()
    managed.mkdir(parents=True, exist_ok=True)
    try:
        if src.is_dir():
            return _install_folder(src, managed, name)
        if src.suffix.lower() == ".md":
            return _install_single_md(src, managed, name)
        if src.suffix.lower() == ".zip":
            return {"success": False, "error": "压缩包请先使用 unpack_skill_archive 解压到工作区后，再对该文件夹调用 install_skill"}
        return {"success": False, "error": "不支持的来源类型：仅支持文件夹或单个 .md 文件"}
    except _SkillInstallError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


def validate_and_extract_archive(
    archive_path: Any,
    target_dir: Any,
    name: str | None = None,
) -> dict[str, Any]:
    """校验一个 zip 压缩包并在安全校验通过后解压到 target_dir（工作区专属子目录）。

    校验与 ``_install_zip`` 一致：zip 损坏、越界路径（绝对/``..``/盘符/UNC）、
    zip 炸弹比率、条目数/体积上限、解压后必须含 SKILL.md。校验失败抛 ``_SkillInstallError``。

    Returns:
        {"success": True, "extracted_dir": str, "root_dir": str, "name": str}
        或 {"success": False, "error": str}
    """
    src = Path(archive_path).expanduser().resolve()
    if not src.is_file() or src.suffix.lower() != ".zip":
        return {"success": False, "error": "仅支持 .zip 压缩包（rar/7z 暂不支持，请转成 zip）"}
    target = Path(target_dir).expanduser().resolve()
    try:
        archive = zipfile.ZipFile(src)
    except zipfile.BadZipFile as exc:
        return {"success": False, "error": f"不是有效的 zip 压缩包：{exc}"}
    with archive:
        default_error = None
        try:
            bad = archive.testzip()
            if bad is not None:
                return {"success": False, "error": f"压缩包损坏：{bad}"}
            members = archive.infolist()
            if len(members) > MAX_FILE_COUNT:
                return {"success": False, "error": f"压缩包内文件数量过多（超过 {MAX_FILE_COUNT}）"}
            total_uncompressed = sum(member.file_size for member in members)
            if total_uncompressed > MAX_TOTAL_SIZE:
                return {"success": False, "error": "压缩包解压后体积过大（超过 50 MB）"}
            compressed = src.stat().st_size
            if compressed > 0 and total_uncompressed > ZIP_BOMB_RATIO * compressed:
                return {"success": False, "error": "检测到可能的 zip 炸弹（解压体积远超压缩体积）"}
            for member in members:
                if member.file_size > MAX_UNCOMPRESSED_ENTRY:
                    return {"success": False, "error": f"压缩包单文件解压后过大（超过 50 MB）：{member.filename}"}
                filename = member.filename
                parts = Path(filename).parts
                if (
                    Path(filename).is_absolute()
                    or filename.startswith("/")
                    or ".." in parts
                    or any(":" in part for part in parts)
                ):
                    return {"success": False, "error": f"压缩包包含非法或越界路径：{filename}"}
            if not _zip_has_skill_md(archive):
                return {"success": False, "error": "压缩包缺少 SKILL.md（需位于顶层或下一级目录）"}
            target.mkdir(parents=True, exist_ok=True)
            dest = _unique_dir(target, name or src.stem)
            dest.mkdir(parents=True, exist_ok=True)
            for member in members:
                t = (dest / member.filename).resolve()
                if t != dest and not _path_within(t, dest):
                    return {"success": False, "error": f"压缩包包含越界路径：{member.filename}"}
            archive.extractall(dest)
        except _SkillInstallError as exc:
            default_error = str(exc)
        except Exception as exc:  # noqa: BLE001
            default_error = f"{type(exc).__name__}: {exc}"
        if default_error:
            return {"success": False, "error": default_error}
    # 定位含 SKILL.md 的目录（顶层或下一级）
    head = dest
    if not (dest / "SKILL.md").is_file():
        sub = next(
            (child for child in dest.iterdir() if child.is_dir() and (child / "SKILL.md").is_file()),
            None,
        )
        if sub is not None:
            head = sub
    return {
        "success": True,
        "extracted_dir": str(head),
        "root_dir": str(dest),
        "name": str(head.name),
    }


def remove_skill_references(
    skill_id: str,
    agent_configs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """返回一个新的 agent_configs 列表，移除每个 agent 对 skill_id 的引用。

    不修改传入的列表（调用方负责持久化）。
    """
    updated: list[dict[str, Any]] = []
    for agent in agent_configs:
        new_agent = dict(agent)
        skill_ids = list(agent.get("skill_ids", []))
        if skill_id in skill_ids:
            skill_ids.remove(skill_id)
        new_agent["skill_ids"] = skill_ids
        updated.append(new_agent)
    return updated


def delete_skill(
    skill_id: str,
    recycle_dir: Any,
    agent_configs: list[dict[str, Any]],
    managed_dir: Any,
    skills_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """可恢复删除（移动到回收目录，而非永久删除）一个托管 Skill。

    Args:
        skill_id: 目标 Skill id。
        recycle_dir: 应用托管的回收目录。
        agent_configs: agent 配置列表，每项含 'id' 与 'skill_ids'。
        managed_dir: 应用托管的 skills 目录（用于校验路径归属）。
        skills_by_id: 可选，预构建的 {id: skill}；缺省时扫描 managed_dir。

    Returns:
        {"success": True, "skill_id", "name", "recycled_to", "cleaned_agent_refs": [...], "error": None}
        或 {"success": False, "error": str}
    """
    if skills_by_id is None:
        managed = Path(managed_dir).expanduser().resolve()
        skills_by_id = SkillCatalog([managed]).by_id()
    skill = skills_by_id.get(skill_id)
    if not skill:
        return {"success": False, "error": "Skill 不存在或未被托管"}
    cleaned = [
        str(agent["id"])
        for agent in agent_configs
        if skill_id in agent.get("skill_ids", [])
    ]
    # 托管 Skill 移入回收目录；内置/外部 Skill 由上层持久化隐藏，不移动原文件。
    if skill.get("source") != "managed":
        return {
            "success": True,
            "skill_id": skill_id,
            "name": str(skill.get("name") or ""),
            "hidden": True,
            "recycled_to": None,
            "cleaned_agent_refs": cleaned,
            "error": None,
        }

    root = Path(str(skill.get("root") or skill.get("path") or "")).expanduser().resolve()
    recycle = Path(recycle_dir).expanduser().resolve()
    recycle.mkdir(parents=True, exist_ok=True)
    dest = _unique_dir(recycle, root.name)
    try:
        shutil.move(str(root), str(dest))
    except OSError as exc:
        return {"success": False, "error": f"移动失败：{exc}"}
    return {
        "success": True,
        "skill_id": skill_id,
        "name": str(skill.get("name", root.name)),
        "recycled_to": str(dest),
        "cleaned_agent_refs": cleaned,
        "error": None,
    }
