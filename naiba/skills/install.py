"""Skill 安装/校验/删除（原 skill_runtime 安装区整区搬移，含 ZIP 炸弹防护与可恢复删除）。"""

from __future__ import annotations

import logging
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any, Callable

from naiba.skills.catalog import SkillCatalog, _frontmatter_value

logger = logging.getLogger("naiba.skill_runtime")


EventCallback = Callable[[dict[str, Any]], None]

SKILL_POLICY_MODES = {"auto", "pinned", "exclusive"}



# Conservative context ceiling (tokens) used when a provider exposes no window
# (e.g. DeepSeek's /v1/models returns no context-length field, so auto-detection
# yields 0). Rather than silently truncating history — which both drops context
# and re-breaks DeepSeek's token-prefix cache every turn — a conversation is
# blocked with a user-visible notice once it reaches this bound.



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

    managed = Path(managed_dir).expanduser().resolve()
    root = Path(str(skill.get("root") or skill.get("path") or "")).expanduser().resolve()
    recycle = Path(recycle_dir).expanduser().resolve()
    recycle.mkdir(parents=True, exist_ok=True)
    if root == managed or not _path_within(root, managed):
        # 散装 Skill：定义文件直接放在扫描目录根下（例如单文件导入的 <managed>/SKILL.md），
        # 此时 root 就是扫描目录本身。绝不能整目录移动，否则会把该目录下所有 Skill 一起
        # 搬走；这里退化为只回收这一个定义文件（同样是可恢复删除）。
        skill_file = Path(str(skill.get("path") or "")).expanduser().resolve()
        if skill_file == managed or not _path_within(skill_file, managed) or not skill_file.is_file():
            return {"success": False, "error": "拒绝删除：Skill 定义不在托管目录内，请手动处理"}
        dest = _unique_dir(recycle, str(skill.get("name") or skill_file.stem))
        dest.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(skill_file), str(dest / skill_file.name))
        except OSError as exc:
            return {"success": False, "error": f"移动失败：{exc}"}
        return {
            "success": True,
            "skill_id": skill_id,
            "name": str(skill.get("name", skill_file.stem)),
            "recycled_to": str(dest),
            "cleaned_agent_refs": cleaned,
            "error": None,
        }
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
