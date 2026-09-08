"""消息附件提取与图像意图判定（原 server.py extract_attachments 族）。

附带职责：宿主托管生成产物（ComfyUI /view URL 与本地文件 → generated 缓存 +
缩略图 + 内容级去重）。data_dir 由调用方显式传入（工作区根 / 数据目录），
本模块不读取任何模块级全局。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

from naiba import net as net_io

from naiba.core.paths import path_within  # noqa: F401  (re-export for callers)
from naiba.storage.media import _ensure_webp_thumb

# 多媒体产物（图片/视频/音频）走"原来那套"消息内产物卡片预览
# （extract_attachments → metadata.attachments → mediaMarkup），不列入
# "修改文件"总结，避免同一产物出现两套入口。名单与 extract_attachments 对齐。
MEDIA_PRODUCT_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".mp4", ".webm", ".mov", ".m4v", ".ogv",
    ".wav", ".mp3", ".m4a", ".ogg", ".flac",
})


def _is_media_product_path(raw: str) -> bool:
    """按扩展名判断文件是否属于多媒体产物（图片/视频/音频）。"""
    lower = str(raw or "").lower()
    if "?" in lower:
        lower = lower.split("?", 1)[0]
    dot = lower.rfind(".")
    return dot > 0 and lower[dot:] in MEDIA_PRODUCT_EXTS


def upload_reference_lines(uploads: list[dict[str, Any]]) -> list[str]:
    """用户上传附件的模型侧引用行（_run_chat 与历史重放共用，保证逐字节一致）。

    PDF 附件追加固定处理指引：提取文本用 read_pdf；扫描版/看图用 pdf_render_pages
    渲染页图后 vision_analyze；细节不清时 pdf_zoom_region 局部放大。
    该文本条件出现、每轮稳定，不改变非 PDF 会话的前缀。
    """
    lines: list[str] = []
    for item in uploads or []:
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        if path.lower().endswith(".pdf"):
            lines.append(
                f"[用户上传文件：{path}]"
                "（PDF 文档：提取文本用 read_pdf；扫描版或需要看图时用 "
                "pdf_render_pages 渲染页图后调用 vision_analyze；细节不清时用 pdf_zoom_region 局部放大）"
            )
        else:
            lines.append(f"[用户上传文件：{path}]")
    return lines


_IMAGE_MEDIA_TERM_RE = re.compile(
    r"(图片|图像|照片|缩略图|位图|图标|png|jpe?g|webp|gif|image|picture|photo|imag|(?<![地纸表网草截导流框])图)",
    re.IGNORECASE,
)
# 用户"要看到/列出/查找/确认图片"的动作词：与图片词同时命中才判定为图像意图，
# 避免"这张图片是谁画的"这类只是提及图片、并不是要显示的请求被误判。
_IMAGE_VIEW_ACTION_RE = re.compile(
    r"(列出|查看|找找|查找|找到|看看|看一下|看一看|看|显示|展示|预览|确认|查询|打开|发给|给我|浏览|翻看|看图|识图|贴出|放出)",
    re.IGNORECASE,
)


def _image_intent(text: str) -> bool:
    """用户是否明确要求查看/列出/查找图片（据此决定枚举类工具返回的图片是否作为附件显示）。

    必须同时命中"图片词"与"查看/列出/查找/确认类动作词"，才算图像意图，减少误伤。
    """
    t = str(text or "")
    return bool(_IMAGE_MEDIA_TERM_RE.search(t) and _IMAGE_VIEW_ACTION_RE.search(t))


def extract_attachments(
    runs: list[dict[str, Any]],
    allow_enumerated_media: bool = False,
    data_dir: Path | None = None,
    imaging: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """从一轮 tool_runs 提取多媒体附件并托管缓存到宿主数据目录。

    ``data_dir`` 为宿主数据根目录（uploads/generated 两个缓存目录位于其下），
    与原来读取模块级 DATA_DIR 语义一致；由调用方注入。
    """
    if data_dir is None:
        raise ValueError("extract_attachments 必须显式传入 data_dir")
    imaging = dict(imaging or {})
    extensions = (
        ".png", ".jpg", ".jpeg", ".webp", ".gif",
        ".mp4", ".webm", ".mov", ".m4v", ".ogv",
        ".wav", ".mp3", ".m4a", ".ogg", ".flac",
    )
    # 枚举类工具（列出/搜索目录、按名匹配文件）的返回值是一批文件路径；
    # 只有当用户明确要求查看/列出/查找图片时才把它们当可显示附件，否则不作为附件，
    # 避免 glob/list 把一堆不相干的图片都拉进消息末尾。
    enumeration_tools = {"glob_files", "glob", "list_directory", "search_files", "grep", "find_files"}
    # 结构化媒体记录里存放"真实路径/URL"的键。识别到这类 dict 时只产出单个附件，
    # 其 thumb_path/name 作为该附件的元数据，而不是被当作独立附件再次扫描。
    source_keys = ("path", "source", "url", "view_url", "file")
    thumb_keys = ("thumb_path", "thumbnail", "thumb_url")
    name_keys = ("name", "filename")
    candidates: list[dict[str, str]] = []

    def is_media(text: str) -> bool:
        text = str(text or "")
        t = text.lower().split("?")[0]
        if t.endswith(extensions):
            return True
        # ComfyUI 产物 URL 形如 http://127.0.0.1:8188/view?filename=xxx.png
        # （图片名在查询参数里，路径末尾是 /view 而非扩展名）。
        try:
            parsed = urllib.parse.urlsplit(text)
            if parsed.scheme in ("http", "https"):
                fn = (urllib.parse.parse_qs(parsed.query).get("filename", [""])[0] or "").lower()
                if fn.endswith(extensions):
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def record(source: str, name: str = "", thumb: str = "") -> None:
        if source and is_media(source):
            candidates.append({"source": source, "name": name or "", "thumb_path": thumb or ""})

    def visit(value: Any) -> None:
        if isinstance(value, str):
            if is_media(value):
                record(value)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        # dict 可能是一条结构化媒体记录：含 path/source/url/view_url 之一。
        # 命中时单独产出该附件，并携带其 thumb_path/name 元数据，随后停止递归，
        # 避免把 name / thumb_path 当作独立附件再次扫描。
        media_source = next(
            (str(value[key]) for key in source_keys if isinstance(value.get(key), str)
             and is_media(str(value[key]))),
            "",
        )
        if media_source:
            thumb = next(
                (str(value[key]) for key in thumb_keys if isinstance(value.get(key), str)),
                "",
            )
            name = next(
                (str(value[key]) for key in name_keys if isinstance(value.get(key), str)),
                "",
            )
            record(media_source, name, thumb)
            return
        for item in value.values():
            visit(item)

    for run in runs:
        # 枚举类工具（glob/list/search）返回一批路径；若非"用户明确要看图"，跳过其图片附件。
        if not allow_enumerated_media and str(run.get("tool") or "") in enumeration_tools:
            continue
        result = run.get("result", "")
        try:
            visit(json.loads(result))
        except (json.JSONDecodeError, TypeError):
            for match in re.findall(r"(?:[A-Za-z]:\\[^\r\n\"']+|https?://[^\s\"']+)", str(result)):
                visit(match.rstrip(".,)"))
    unique = []
    seen = set()
    for item in candidates:
        source = item["source"]
        parsed = urllib.parse.urlparse(source)
        query = urllib.parse.parse_qs(parsed.query)
        local_path = Path(source).expanduser()
        is_local_file = local_path.is_file()
        name = (
            item.get("name")
            or (
                local_path.name
                if is_local_file
                else Path((query.get("filename") or [parsed.path])[0]).name
            )
            or "生成结果"
        )
        thumb_path = item.get("thumb_path") or ""
        is_local_comfy = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in {"127.0.0.1", "localhost"}
            and parsed.port == 8188
        )
        # The host, not the model, collects and caches generated media. This
        # makes previews durable and keeps arbitrary output paths outside the
        # file-serving allowlist.
        if is_local_comfy or is_local_file:
            uploads_dir = (data_dir / "uploads").resolve()
            already_cached = is_local_file and path_within(local_path.resolve(), uploads_dir)
            if already_cached:
                # 已在宿主 uploads 缓存目录（且带缩略图）：保留 uploads 路径即可服务与展示，
                # 不用再重复拷贝到 generated。
                source = source
            else:
                try:
                    generated_dir = (data_dir / "generated").resolve()
                    generated_dir.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()[:16]
                    destination = generated_dir / f"{digest}_{name}"
                    if not destination.is_file() or destination.stat().st_size <= 0:
                        if is_local_comfy:
                            with net_io.open(source, timeout=120) as response, destination.open("wb") as handle:
                                shutil.copyfileobj(response, handle, length=1024 * 1024)
                        else:
                            shutil.copy2(local_path.resolve(), destination)
                    source = str(destination)
                    # 缓存主图后同步生成缩略图，否则前端请求 <source>_thumb.webp 会 404 → 破图占位符。
                    if not thumb_path:
                        thumb_path = _ensure_webp_thumb(destination, imaging)
                        if not thumb_path:
                            # 缩略图生成失败时退化为用主图当缩略图，保证可显示。
                            thumb_path = source
                except (OSError, urllib.error.URLError, ValueError):
                    # 缓存/下载失败：ComfyUI 的 /view URL 会由 /api/file 代理拉取，保留它即可显示。
                    pass
        content_key = (str(source or ""), str(thumb_path or ""))
        if content_key in seen:
            continue
        seen.add(content_key)
        unique.append({"source": source, "name": name, "thumb_path": thumb_path})

    # 同一张图可能同时被工具路径(glob/pwsh/…复制到 generated，无缩略图)与
    # vision_read_folder(缓存到 uploads，带缩略图)各记录一份。按原始文件名去重，
    # 优先保留带 thumb_path 的版本，避免出现"同图双份、其中一份缩略图破图"。
    by_key: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for attachment in unique:
        key = str(attachment.get("name") or "").strip().lower() or str(attachment.get("source") or "").lower()
        if key not in by_key:
            by_key[key] = attachment
            order.append(key)
        elif attachment.get("thumb_path") and not by_key[key].get("thumb_path"):
            by_key[key] = attachment
    # 内容级去重：同一张图可能被 ComfyUI /view URL 与复制到目录的本地路径各记录一份
    # （来源不同、文件名也可能不同）。对已缓存的图片按文件字节做 SHA-256，完全一致视为同一张，
    # 只保留第一份，避免"同图在末尾反复显示"。仅针对本轮消息内的附件，不遍历历史记录。
    final: list[dict[str, str]] = []
    seen_content: set[str] = set()
    for attachment in (by_key[key] for key in order):
        source = str(attachment.get("source") or "")
        try:
            p = Path(source).expanduser()
            if p.is_file() and p.stat().st_size > 0:
                h = hashlib.sha256()
                with p.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(65536), b""):
                        h.update(chunk)
                content_key = "file:" + h.hexdigest()
            else:
                content_key = "url:" + source
        except Exception:  # noqa: BLE001 - 读取失败按来源去重兜底
            content_key = "url:" + source
        if content_key in seen_content:
            continue
        seen_content.add(content_key)
        final.append(attachment)
        if len(final) >= 20:
            break
    return final
