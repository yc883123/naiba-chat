"""视觉图像原语（Pillow）：编码/缩略/尺寸/像素读取（自 naiba.vision.runtime 迁出）。

纯函数层：不持有 app、不读配置；输入输出仅经参数与文件路径。
VisionRouter 的工具处理器、编码回调与探测辅助经本模块复用同一套图像处理约定
（统一 RGB 化、MAX_EDGE 缩略、80%→62% 质量阶梯、LANCZOS 收敛）。
"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

MAX_EDGE = 1600
TARGET_BYTES = 900 * 1024


def _make_probe_jpeg_b64() -> str:
    """Create a small valid RGB JPEG without depending on a file asset."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (80, 120, 160)).save(buffer, format="JPEG", quality=85, optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _encode_image_bytes(raw: bytes, media_type: str, name: str = "") -> dict[str, Any] | None:
    """Decode every image and normalize it to a bounded RGB JPEG payload."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
        image.thumbnail((MAX_EDGE, MAX_EDGE))
        encoded = b""
        for quality in (85, 78, 70, 62):
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality, optimize=True)
            encoded = buffer.getvalue()
            if len(encoded) <= TARGET_BYTES:
                break
        while len(encoded) > TARGET_BYTES and max(image.size) > 768:
            image = image.resize(
                tuple(max(1, int(value * 0.85)) for value in image.size),
                Image.Resampling.LANCZOS,
            )
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=62, optimize=True)
            encoded = buffer.getvalue()
        return {
            "type": "image",
            "media_type": "image/jpeg",
            "data": base64.b64encode(encoded).decode("ascii"),
            "name": name,
        }
    except (OSError, ValueError):
        return None


def encode_image_file(path: str) -> dict[str, Any] | None:
    p = Path(path).expanduser()
    if not p.is_file():
        return None
    media_type = IMAGE_MEDIA_TYPES.get(p.suffix.lower())
    if not media_type or p.stat().st_size > 30 * 1024 * 1024:
        return None
    part = _encode_image_bytes(p.read_bytes(), media_type, p.name)
    if part is not None:
        part["path"] = str(p.resolve())
    return part


def _image_size(path: str) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.size
    except (ImportError, OSError, ValueError):
        return None


def _read_rgb(path: str):
    from PIL import Image

    image = Image.open(path)
    if image.mode not in {"RGB", "L"}:
        background = Image.new("RGB", image.size, "white")
        if "A" in image.getbands():
            background.paste(image, mask=image.getchannel("A"))
        else:
            background.paste(image)
        return background
    return image.convert("RGB")
