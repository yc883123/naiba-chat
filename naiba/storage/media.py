"""宿主图片缓存/缩略图处理（原 server.py 图片缓存族）。

来源：server.py 的 _thumb_webp_path/_fit_image_pixels/_ensure_webp_thumb/_process_uploaded_image
（112-216）与 _image_cache_dirs/_uploads_total_bytes/_clean_uploads_cache（332-420）。
vision_runtime 的 _cache_folder_images 缓存写入逻辑随阶段 2 视觉模块拆分归位。

设计约束：所有函数显式接收 data_dir / imaging 参数，不读取任何模块级全局
（原 _ensure_webp_thumb 经 APP.config 取 imaging 配置，现改为参数注入；
原 *目录族经模块级 DATA_DIR 取目录，现改为 data_dir 参数）。
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


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


def _ensure_webp_thumb(main_path: Path, imaging: dict[str, Any] | None = None) -> str:
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
        imaging = dict(imaging or {})
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


IMAGE_CACHE_CLEAN_LIMIT = 128 * 1024 * 1024  # 128 MB


def _image_cache_dirs(data_dir: Path) -> list[Path]:
    """返回宿主图片缓存的两个目录：用户上传/视觉缓存（uploads）与生成产物缓存（generated）。"""
    return [(data_dir / "uploads").resolve(), (data_dir / "generated").resolve()]


def _uploads_total_bytes(data_dir: Path) -> int:
    """Total size of all cached images (uploads + generated, main + thumbnails)."""
    total = 0
    for cache_dir in _image_cache_dirs(data_dir):
        if not cache_dir.is_dir():
            continue
        for path in cache_dir.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
    return total


def _clean_uploads_cache(
    limit: int = IMAGE_CACHE_CLEAN_LIMIT, data_dir: Path | None = None
) -> dict[str, Any]:
    """清理旧图片缓存（uploads + generated）：只保留最新的、总大小不超过 limit 的图片
    （主图+缩略图成组，跨两个文件夹合并后统一按时间戳从新到旧）。

    返回 {removed: 删除文件数, freed: 释放字节数, size: 清理后剩余字节数}。
    """
    if data_dir is None:
        raise ValueError("data_dir 必须显式传入")
    cache_dirs = [d for d in _image_cache_dirs(data_dir) if d.is_dir()]
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
    return {"removed": removed, "freed": freed, "size": _uploads_total_bytes(data_dir)}
