"""图片处理工具：上传压缩、WebP 缩略图、图片缓存清理、媒体类型推断、模型输入编码。

从 server.py 拆出的纯函数集。路径/运行时可变全局统一从 app_state 读取。
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
from pathlib import Path
from typing import Any

import app_state

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

# 部分系统 mimetypes 未注册 webp/avif 等，导致 <img> 接到 application/octet-stream
# 配合 nosniff 而拒绝渲染（缩略图破图）。提供显式兜底映射。
_MEDIA_MIME_FALLBACK = {
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".ogv": "video/ogg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".svg": "image/svg+xml",
}


def _thumb_webp_path(main_path: Path) -> Path:
    """Given a cached main image path, derive the WebP thumbnail path."""
    return main_path.with_name(main_path.stem + "_thumb.webp")


def _fit_image_pixels(img: Any, max_pixels: int) -> Any:
    """Scale ``img`` down with Lanczos so width*height <= max_pixels."""
    from PIL import Image

    width, height = img.width, img.height
    if width * height <= max_pixels:
        return img.copy()
    ratio = (max_pixels / (width * height)) ** 0.5
    nw = max(1, int(width * ratio))
    nh = max(1, int(height * ratio))
    return img.resize((nw, nh), Image.LANCZOS)


def _ensure_webp_thumb(main_path: Path) -> str:
    """Generate a ``<stem>_thumb.webp`` next to ``main_path`` if missing.

    Best-effort: returns the thumb path on success, else ``""`` so the caller can
    fall back (e.g. to the main image). Used by generated-media caching so every
    ComfyUI image has a served thumbnail in the history.
    """
    try:
        from PIL import Image, ImageOps

        if main_path.suffix.lower() not in IMAGE_SUFFIXES:
            return ""
        if not main_path.is_file():
            return ""
        thumb_path = _thumb_webp_path(main_path)
        if thumb_path.is_file() and thumb_path.stat().st_size > 0:
            return str(thumb_path)
        img = Image.open(main_path)
        img.load()
        if (img.format or "").upper() == "GIF":
            return ""
        img = ImageOps.exif_transpose(img)
        imaging = dict(app_state.APP.config.data.get("imaging") or {}) if getattr(app_state.APP, "config", None) else {}
        thumb_px = max(1, int(imaging.get("thumbnail_max_pixels", 500000) or 500000))
        thumb_img = _fit_image_pixels(img, thumb_px)
        buf = io.BytesIO()
        out = thumb_img.convert("RGBA") if thumb_img.mode in ("P", "RGBA") else thumb_img
        out.save(buf, format="WEBP", quality=82)
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        thumb_path.write_bytes(buf.getvalue())
        return str(thumb_path)
    except Exception:  # noqa: BLE001 - thumbnail is best-effort
        return ""


def _process_uploaded_image(
    data: bytes, filename: str, imaging: dict[str, Any]
) -> tuple[bytes, str | None, bytes]:
    """Optionally compress an image and always emit a WebP thumbnail.

    Returns ``(main_bytes, thumb_filename, thumb_bytes)``. Non-images and GIFs
    are passed through untouched with no thumbnail. Compression keeps the source
    format and preserves alpha; thumbnails are always WebP.
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        return data, None, b""
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        fmt = (img.format or "").upper()
        if fmt == "GIF":
            return data, None, b""
        img = ImageOps.exif_transpose(img)
    except Exception:  # noqa: BLE001 - malformed image -> keep original bytes
        return data, None, b""

    original = bool(imaging.get("image_upload_original", False))
    max_px = max(1, int(imaging.get("image_max_pixels", 2000000) or 2000000))
    thumb_px = max(1, int(imaging.get("thumbnail_max_pixels", 500000) or 500000))

    main_bytes = data
    if not original and img.width * img.height > max_px:
        img = _fit_image_pixels(img, max_px)
        try:
            buf = io.BytesIO()
            out_fmt = fmt if fmt in {"PNG", "JPEG", "WEBP"} else "PNG"
            save_img = img
            if out_fmt == "JPEG" and save_img.mode not in ("RGB", "L"):
                save_img = save_img.convert("RGB")
            save_img.save(buf, format=out_fmt)
            main_bytes = buf.getvalue()
        except Exception:  # noqa: BLE001 - fall back to original bytes
            main_bytes = data

    thumb_img = _fit_image_pixels(img, thumb_px)
    try:
        thumb_buf = io.BytesIO()
        out = thumb_img.convert("RGBA") if thumb_img.mode in ("P", "RGBA") else thumb_img
        out.save(thumb_buf, format="WEBP", quality=82)
        thumb_name = Path(filename).stem + "_thumb.webp"
        return main_bytes, thumb_name, thumb_buf.getvalue()
    except Exception:  # noqa: BLE001
        return main_bytes, None, b""


def _image_cache_dirs() -> list[Path]:
    """返回宿主图片缓存的两个目录：用户上传/视觉缓存（uploads）与生成产物缓存（generated）。"""
    return [(app_state.DATA_DIR / "uploads").resolve(), (app_state.DATA_DIR / "generated").resolve()]


def _uploads_total_bytes() -> int:
    """Total size of all cached images (uploads + generated, main + thumbnails)."""
    total = 0
    for cache_dir in _image_cache_dirs():
        if not cache_dir.is_dir():
            continue
        for path in cache_dir.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
    return total


IMAGE_CACHE_CLEAN_LIMIT = 128 * 1024 * 1024  # 128 MB


def _clean_uploads_cache(limit: int = IMAGE_CACHE_CLEAN_LIMIT) -> dict[str, Any]:
    """清理旧图片缓存（uploads + generated）：只保留最新的、总大小不超过 limit 的图片
    （主图+缩略图成组，跨两个文件夹合并后统一按时间戳从新到旧）。

    返回 {removed: 删除文件数, freed: 释放字节数, size: 清理后剩余字节数}。
    """
    cache_dirs = [d for d in _image_cache_dirs() if d.is_dir()]
    if not cache_dirs:
        return {"removed": 0, "freed": 0, "size": 0}
    # 以"主图 + 其缩略图"成组（主图名 X.ext 与其缩略图 X_thumb.webp 归为一组）。
    # 用 "目录名/前缀" 作为组键，避免不同目录下同名前缀被合并。
    groups: dict[str, list[Path]] = {}
    for cache_dir in cache_dirs:
        for path in cache_dir.rglob("*"):
            if not path.is_file():
                continue
            name = path.name
            if name.endswith("_thumb.webp"):
                key = name[: -len("_thumb.webp")]
            else:
                key = path.stem
            groups.setdefault(f"{cache_dir.name}/{key}", []).append(path)

    def _group_mtime(paths: list[Path]) -> int:
        latest = 0
        for p in paths:
            try:
                latest = max(latest, int(p.stat().st_mtime))
            except OSError:
                continue
        return latest

    def _group_size(paths: list[Path]) -> int:
        total = 0
        for p in paths:
            try:
                total += p.stat().st_size
            except OSError:
                continue
        return total

    entries: list[tuple[int, str, list[Path]]] = [
        (_group_mtime(paths), key, paths) for key, paths in groups.items()
    ]
    entries.sort(key=lambda item: item[0], reverse=True)  # 新 -> 旧
    kept_keys: set[str] = set()
    kept_size = 0
    for mtime, key, paths in entries:
        group_size = _group_size(paths)
        if kept_size + group_size <= limit:
            kept_size += group_size
            kept_keys.add(key)
    removed = 0
    freed = 0
    for mtime, key, paths in entries:
        if key in kept_keys:
            continue
        for p in paths:
            try:
                size = p.stat().st_size
                p.unlink()
                removed += 1
                freed += size
            except OSError:
                continue
    return {"removed": removed, "freed": freed, "size": _uploads_total_bytes()}


def static_asset_version() -> str:
    digest = hashlib.sha256()
    for name in ("app.js", "styles.css"):
        digest.update((app_state.PUBLIC_DIR / name).read_bytes())
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
    forbidden_exact = {Path.home().resolve(), app_state.APP_DIR, app_state.PUBLIC_DIR.resolve(), app_state.DATA_DIR.resolve()}
    if resolved in forbidden_exact:
        raise ValueError("不能把用户主目录或程序自身目录作为 Skill 目录，请使用其子目录")


IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

MODEL_IMAGE_MAX_EDGE = 1600
MODEL_IMAGE_TARGET_BYTES = 900 * 1024
MODEL_IMAGE_HISTORY_LIMIT = 3


def _jpeg_for_model(image: Any, target_bytes: int = MODEL_IMAGE_TARGET_BYTES) -> bytes:
    from PIL import Image

    image.thumbnail((MODEL_IMAGE_MAX_EDGE, MODEL_IMAGE_MAX_EDGE))
    if image.mode != "RGB":
        background = Image.new("RGB", image.size, "white")
        if "A" in image.getbands():
            background.paste(image, mask=image.getchannel("A"))
        else:
            background.paste(image)
        image = background

    encoded = b""
    for quality in (85, 78, 70, 62):
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        encoded = buffer.getvalue()
        if len(encoded) <= target_bytes:
            return encoded

    while len(encoded) > target_bytes and max(image.size) > 768:
        next_size = tuple(max(1, int(value * 0.85)) for value in image.size)
        image = image.resize(next_size, Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=62, optimize=True)
        encoded = buffer.getvalue()
    return encoded


def encode_image_for_model(source: str) -> dict[str, str] | None:
    path = Path(source).expanduser().resolve()
    media_type = IMAGE_MEDIA_TYPES.get(path.suffix.lower())
    if not media_type or not path.is_file() or path.stat().st_size > 30 * 1024 * 1024:
        return None
    raw = path.read_bytes()
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(raw)) as opened:
            image = ImageOps.exif_transpose(opened).copy()
            raw = _jpeg_for_model(image)
    except (ImportError, OSError, ValueError):
        return None
    return {
        "type": "image",
        "media_type": "image/jpeg",
        "data": base64.b64encode(raw).decode("ascii"),
        "name": path.name,
    }
