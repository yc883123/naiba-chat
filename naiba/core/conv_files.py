"""会话文件查看/编辑保存（右侧文件面板后端，原 server.py _conv_file_* 族）。

安全边界：面板只允许"本会话改动过 或 位于会话工作区内"的文件；
保存写回必须同时满足两者；越界路径（../穿越、绝对路径、http 前缀）一律拒绝。
config 对象只要求 resolve_workspace_dir(raw) 契约（窄接口，见设计文档 §2.1）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from naiba.core.paths import path_within

_CONV_FILE_SNIFF_BYTES = 4096          # 二进制嗅探长度
_CONV_FILE_READ_CAP = 2 * 1024 * 1024  # 单次读取/预览上限
_CONV_FILE_SAVE_CAP = 8 * 1024 * 1024  # 单次写回上限
_CONV_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"})


def _conv_touched_files(conversation: dict[str, Any] | None) -> list[str]:
    """该会话所有 assistant 消息 metadata.files 里的原始路径（去重）。"""
    result: list[str] = []
    seen: set[str] = set()
    for message in (conversation or {}).get("messages") or []:
        meta = (message or {}).get("metadata") or {}
        files = meta.get("files") if isinstance(meta, dict) else None
        if not isinstance(files, list):
            continue
        for item in files:
            raw = str((item if isinstance(item, dict) else {}).get("path") or "").strip()
            if not raw:
                continue
            key = raw.replace("\\", "/").rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(raw)
    return result


def _conv_workspace_root(conversation: dict[str, Any] | None, config: Any) -> Path | None:
    """会话生效的工作区根：优先会话 workspace_dir，否则配置默认工作区。"""
    raw = str((conversation or {}).get("workspace_dir") or "").strip()
    try:
        return config.resolve_workspace_dir(raw or None).resolve()
    except (OSError, ValueError):
        return None


def _conv_file_target(raw_path: Any, root: Path | None) -> Path | None:
    """把消息记录里的路径解析成磁盘绝对路径；相对路径按工作区根补全。"""
    raw = str(raw_path or "").strip()
    if not raw or "\x00" in raw or raw.startswith(("http://", "https://", "\\\\")):
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        if root is None:
            return None
        path = root / path
    return path.resolve()


def _conv_file_allow(
    conversation: dict[str, Any] | None, config: Any, raw_path: Any
) -> tuple[Path | None, bool, bool, Path | None]:
    """返回 (目标文件, listed, within_root, root)。listed=本会话改动过；within_root=在工作区内。"""
    raw = str(raw_path or "").strip()
    root = _conv_workspace_root(conversation, config)
    target = _conv_file_target(raw, root)
    touched_lower = {item.replace("\\", "/").rstrip("/").lower() for item in _conv_touched_files(conversation)}
    raw_key = raw.replace("\\", "/").rstrip("/").lower()
    listed = bool(target and str(target).replace("\\", "/").rstrip("/").lower() in touched_lower) or raw_key in touched_lower
    within_root = bool(target and root and path_within(target, root))
    return target, listed, within_root, root


def _conv_file_open(conversation: dict[str, Any] | None, config: Any, raw_path: Any) -> dict[str, Any]:
    """读取会话文件的预览信息（文本带内容；图片/二进制只给元数据）。"""
    target, listed, within_root, _root = _conv_file_allow(conversation, config, raw_path)
    if target is None or not (listed or within_root):
        raise ValueError("无权访问该文件：不在本会话改动记录中，也不在会话工作区内")
    if not target.is_file():
        raise FileNotFoundError(str(target))
    stat = target.stat()
    suffix = target.suffix.lower()
    name = target.name
    info: dict[str, Any] = {
        "path": str(target),
        "name": name,
        "size": stat.st_size,
        "mtime": round(stat.st_mtime * 1000),
        "savable": False,
        "kind": "binary",
        "truncated": False,
    }
    if suffix in _CONV_IMAGE_EXTS:
        info["kind"] = "image"
        return info
    with target.open("rb") as handle:
        head = handle.read(_CONV_FILE_SNIFF_BYTES)
    if b"\x00" in head:
        info["kind"] = "binary"
        return info
    info["kind"] = "text"
    info["savable"] = bool(listed and within_root)
    with target.open("rb") as handle:
        payload = handle.read(_CONV_FILE_READ_CAP + 1)
    info["truncated"] = len(payload) > _CONV_FILE_READ_CAP
    info["content"] = payload[:_CONV_FILE_READ_CAP].decode("utf-8", errors="replace")
    return info


def _conv_file_save(
    conversation: dict[str, Any] | None, config: Any, raw_path: Any, content: Any
) -> dict[str, Any]:
    """把右侧面板的编辑内容写回磁盘。

    安全门槛：目标必须同时在"本会话改动记录"与"会话工作区"内；拒绝其他任何路径。
    """
    target, listed, within_root, _root = _conv_file_allow(conversation, config, raw_path)
    if target is None or not (listed and within_root):
        raise ValueError("无权保存该文件：只允许保存本会话改动过且位于会话工作区内的文件")
    text = str(content if content is not None else "")
    if len(text.encode("utf-8", errors="replace")) > _CONV_FILE_SAVE_CAP:
        raise ValueError("保存内容过大，已超过上限")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.naiba-tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
    stat = target.stat()
    return {"path": str(target), "name": target.name, "size": stat.st_size, "mtime": round(stat.st_mtime * 1000)}
