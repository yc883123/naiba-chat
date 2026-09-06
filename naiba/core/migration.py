# -*- coding: utf-8 -*-
"""旧安装/数据目录迁移辅助（层级 1；路径经 PathContext 显式传入，不读全局）。"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from naiba.paths import PathContext


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


def _copy_legacy_data(source: Path, paths: PathContext, replace_empty_target: bool = True) -> dict[str, bool]:
    """Merge a legacy install into the current data directory, including all subdirectories."""
    report = {"config": False, "data": False, "skills": False}
    legacy_config = source / "config.json"
    legacy_data = source / "data"
    paths.app_dir.mkdir(parents=True, exist_ok=True)

    if legacy_config.is_file() and (
        not paths.config_path.exists() or not _config_has_providers(paths.config_path)
    ):
        shutil.copy2(legacy_config, paths.config_path)
        report["config"] = True

    if not legacy_data.is_dir():
        return report
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    source_db = legacy_data / "chat.db"
    target_db = paths.data_dir / "chat.db"
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
        report["data"] = _merge_data_tree(item, paths.data_dir / item.name) or report["data"]

    # 导入旧安装根目录的 Skills（旧版 paths.app_dir/skills 或数据目录同级 skills）到新托管目录。
    managed_skills = (paths.data_dir / "skills").resolve()
    for legacy_skills in (
        (source / "skills").resolve(),
        (source.parent / "skills").resolve(),
        (legacy_data / "skills").resolve(),
    ):
        if legacy_skills.is_dir() and legacy_skills != managed_skills:
            report["skills"] = _merge_data_tree(legacy_skills, managed_skills) or report["skills"]
    return report


def migrate_legacy_data(paths: PathContext) -> dict[str, Any]:
    """冻结版首次启动：从 EXE 相邻旧目录迁移 config.json 与 data/ 到数据目录。

    仅当数据目录（%LOCALAPPDATA%\\NaibaChat）尚未初始化时执行；旧文件保留，
    不覆盖已存在的新数据。源码模式（paths.app_dir == paths.exe_dir）跳过。
    """
    report: dict[str, Any] = {"migrated": False, "config": False, "data": False, "source": ""}
    if str(paths.app_dir) == str(paths.exe_dir):
        return report
    legacy_config = paths.exe_dir / "config.json"
    legacy_data = paths.exe_dir / "data"
    if not legacy_config.is_file() and not legacy_data.is_dir():
        return report
    try:
        migrated = _copy_legacy_data(paths.exe_dir)
        report.update(migrated)
    except OSError as exc:
        print(f"迁移旧数据失败：{exc}")
    if report["config"] or report["data"]:
        report["migrated"] = True
        report["source"] = str(paths.exe_dir)
    return report
