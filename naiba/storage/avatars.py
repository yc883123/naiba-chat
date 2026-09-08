# -*- coding: utf-8 -*-
"""Agent 自定义头像：上传解码 → 中心裁切正方形 → WebP 落盘（内容哈希命名）。

设计要点：
- 落盘目录固定为 `<data_dir>/avatars`（**不进 uploads**——uploads 有"未被引用即自动清理"
  的语义，头像不属于消息引用，放那里会被清掉）；
- 文件名 = `<agent_id>_<内容哈希12>.webp`：换图即换名，浏览器缓存不会命中旧图，
  也让 `/api/agents/avatar/<name>` 可以放心长期缓存；
- 中心裁切用 `ImageOps.fit(..., centering=(0.5, 0.5))`，与前端 `object-fit: cover` 同口径；
- 纯 IO + PIL，不依赖上层模块（storage 层可被 app 直接调用）。
"""
from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

AVATAR_DIR_NAME = "avatars"
AVATAR_SIZE = 256  # 落盘边长（正方形，聊天里 30px 显示足够清晰）
AVATAR_MAX_BYTES = 8 * 1024 * 1024
_AVATAR_STEM_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def avatar_dir(data_dir: Path) -> Path:
    return Path(data_dir) / AVATAR_DIR_NAME


def is_avatar_filename(name: str) -> bool:
    """只接受本模块生成的 `<id>_<hash>.webp` 形态（挡目录穿越与任意文件读取）。"""
    name = str(name or "")
    if not name.endswith(".webp") or len(name) > 96:
        return False
    stem = name[: -len(".webp")]
    return bool(_AVATAR_STEM_RE.fullmatch(stem)) and "_" in stem


def store_agent_avatar(data_dir: Path, agent_id: str, raw: bytes, previous: str = "") -> str:
    """保存 Agent 头像并返回文件名；失败抛 ValueError（调用方转 400）。"""
    if not raw:
        raise ValueError("头像文件为空")
    if len(raw) > AVATAR_MAX_BYTES:
        raise ValueError(f"头像图片不能超过 {AVATAR_MAX_BYTES // (1024 * 1024)} MB")
    try:
        from PIL import Image, ImageOps
    except Exception as exc:  # noqa: BLE001 - 依赖缺失要明确报错，不静默降级
        raise ValueError(f"缺少图像处理依赖，无法处理头像：{exc}") from exc
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:  # noqa: BLE001 - 用户上传的坏图属于可预期错误
        raise ValueError("无法解析这张图片，请换一张常见格式（PNG / JPG / WebP）") from exc

    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
    square = ImageOps.fit(
        img, (AVATAR_SIZE, AVATAR_SIZE), method=Image.LANCZOS, centering=(0.5, 0.5)
    )
    buf = io.BytesIO()
    square.save(buf, format="WEBP", quality=88)
    payload = buf.getvalue()

    digest = hashlib.sha256(payload).hexdigest()[:12]
    name = f"{agent_id}_{digest}.webp"
    target_dir = avatar_dir(data_dir)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / name
        if not target.exists():
            tmp = target.with_name(target.name + ".part")
            tmp.write_bytes(payload)
            tmp.replace(target)
        if previous and previous != name and is_avatar_filename(previous):
            old = target_dir / previous
            if old.exists():
                old.unlink()
    except OSError as exc:
        raise ValueError(f"头像保存失败：{exc}") from exc
    return name


def read_agent_avatar(data_dir: Path, name: str) -> bytes | None:
    """读取头像字节；文件名不合法或文件不存在返回 None。"""
    if not is_avatar_filename(name):
        return None
    try:
        return (avatar_dir(data_dir) / name).read_bytes()
    except OSError:
        return None
