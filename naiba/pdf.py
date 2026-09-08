# -*- coding: utf-8 -*-
"""PDF 服务：文本提取 / 每页渲染 / 局部高清渲染（PyMuPDF 1.28）。

设计约束（与 storage.media 同构）：所有函数显式接收 data_dir（宿主数据根目录），
不读取模块级全局；缓存一律写入宿主托管的 ``<data_dir>/uploads/pdf_pages/<sha16>/``，
与用户上传/缩略图同一清理与统计体系（/api/imaging/clean / _uploads_total_bytes）。

工具职责：本模块只做 PDF 计算与缓存产物，schema/策略绑定见 tools/providers/documents.py。

上限口径（用户确认的推荐数值）：
- 文本：单次 ≤30000 字符（与 read_file 对齐）、≤50 页，截断带续读起点；
- 整页渲染：单次 ≤20 页、长边 ≤1600px、单 PDF 缓存 ≤100 张页图；
- 局部放大：scale 1-6（默认 3）、输出长边 ≤3072px。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pymupdf

from naiba.core.paths import path_within  # noqa: F401  (re-export for callers)

PDF_TEXT_PAGE_BUDGET = 50
PDF_TEXT_CHAR_LIMIT = 30000
PDF_TEXT_EMPTY_THRESHOLD = 20
PDF_RENDER_MAX_PAGES = 20
PDF_RENDER_MAX_EDGE = 1600
PDF_CACHE_MAX_IMAGES = 100
PDF_ZOOM_MAX_EDGE = 3072
PDF_ZOOM_MIN_RATIO = 0.05  # 区域宽/高至少占页面 5%，防止无效调用

PDF_REGION_KEYWORDS = {
    "top": (0, 0, 100, 50),
    "bottom": (0, 50, 100, 100),
    "left": (0, 0, 50, 100),
    "right": (50, 0, 100, 100),
    "middle": (25, 25, 75, 75),
}

PDF_CACHE_REL = "uploads/pdf_pages"


def _pdf_cache_root(data_dir: Path) -> Path:
    return (data_dir / PDF_CACHE_REL).resolve()


def _pdf_cache_dir(data_dir: Path, pdf_path: Path) -> Path:
    """按文件内容哈希键控的缓存目录：<root>/<sha16>/（内容一致即复用）。"""
    digest = hashlib.sha256()
    with pdf_path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    root = _pdf_cache_root(data_dir)
    return root / digest.hexdigest()[:16]


def _open_pdf(pdf_path: Path) -> pymupdf.Document:
    if not pdf_path.is_file():
        raise ValueError(f"PDF 文件不存在：{pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"不是 PDF 文件（扩展名 {pdf_path.suffix or '（无）'}）：{pdf_path}")
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as exc:  # noqa: BLE001 - PyMuPDF 对损坏/非 PDF 抛多种异常
        raise ValueError(f"无法解析 PDF（文件已损坏或不是有效 PDF）：{type(exc).__name__}: {exc}") from exc
    if doc.needs_pass:
        doc.close()
        raise ValueError("该 PDF 已加密，暂不支持读取")
    if doc.page_count <= 0:
        doc.close()
        raise ValueError("PDF 没有页面")
    return doc


def _norm_pages(start_page: int, end_page: int | None, total: int, budget: int, tool_name: str) -> tuple[int, int]:
    """归一页段（1-based 闭区间）；越界/超预算时明确报错。"""
    if start_page < 1:
        raise ValueError("start_page 必须 ≥ 1")
    if start_page > total:
        raise ValueError(f"页码越界：共 {total} 页，start_page={start_page} 超出范围")
    if end_page is not None:
        if end_page < 1:
            raise ValueError("end_page 必须 ≥ 1")
        if end_page > total:
            raise ValueError(f"页码越界：共 {total} 页，end_page={end_page} 超出范围")
        if end_page < start_page:
            raise ValueError("end_page 小于 start_page，无法读取")
        return start_page, end_page
    end = min(total, start_page + budget - 1)
    return start_page, end


def _page_marker(page: int) -> str:
    return f"== 第 {page} 页 =="


# ---- 文本提取 ----

def extract_pdf_text(
    pdf_path: Path,
    start_page: int = 1,
    end_page: int | None = None,
) -> dict[str, Any]:
    """提取 PDF 文本层，按页分隔；字符预算截断并携带续读起点。

    返回 {text, total_pages, range_start, range_end, truncated, has_text_layer,
          resume_start_page}；无文本层（扫描版）时 text 为空并给出提示。
    """
    doc = _open_pdf(pdf_path)
    try:
        total = doc.page_count
        n_start, n_end = _norm_pages(start_page, end_page, total, PDF_TEXT_PAGE_BUDGET, "read_pdf")
        chunks: list[str] = []
        chars = 0
        real_chars = 0
        truncated = False
        resume = 0
        for page_no in range(n_start, n_end + 1):
            try:
                page_text = doc[page_no - 1].get_text("text") or ""
            except Exception:  # noqa: BLE001 - 单页提取失败不阻断其它页
                page_text = ""
            marker = _page_marker(page_no)
            block = (marker + "\n" + page_text) if page_text.strip() else marker + "\n（本页无可提取文本）"
            if chars + len(block) > PDF_TEXT_CHAR_LIMIT:
                block = block[: max(0, PDF_TEXT_CHAR_LIMIT - chars)]
                truncated = True
                resume = page_no
                chunks.append(block)
                break
            chunks.append(block)
            chars += len(block)
            real_chars += len(re.sub(r"\s+", "", page_text))
        text = "\n\n".join(chunks).strip()
        has_layer = real_chars >= PDF_TEXT_EMPTY_THRESHOLD
        if not has_layer and not truncated:
            return {
                "text": "",
                "total_pages": total,
                "range_start": n_start,
                "range_end": n_end,
                "truncated": False,
                "has_text_layer": False,
                "resume_start_page": 0,
                "hint": (
                    f"该 PDF（共 {total} 页）没有可提取的文本层（可能是扫描版或图片型 PDF）。"
                    "请用 pdf_render_pages 渲染页图，再对页图路径调用 vision_analyze 识别内容。"
                ),
            }
        if truncated and resume:
            resume = resume + 1 if resume < total else total + 1
        return {
            "text": text,
            "total_pages": total,
            "range_start": n_start,
            "range_end": n_end,
            "truncated": truncated,
            "has_text_layer": True,
            "resume_start_page": resume,
        }
    finally:
        doc.close()


# ---- 整页渲染 ----

def _render_scale(page: pymupdf.Page, max_edge: int) -> float:
    width, height = page.rect.width, page.rect.height
    if not width or not height:
        raise ValueError("页面尺寸无效")
    return max(0.1, max_edge / max(width, height))


def render_pdf_pages(
    pdf_path: Path,
    data_dir: Path,
    pages: str | list[int] | None = None,
    imaging: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 PDF 页面渲染为 PNG（托管缓存，幂等复用），返回 [{page, path, thumb_path}]。

    pages 语法："1-5,8" / 数字列表；缺省渲染前 20 页。单次 ≤20 页、单 PDF 缓存 ≤100 张。
    """
    cache_dir = _pdf_cache_dir(data_dir, pdf_path)
    if not cache_dir.is_dir():
        cache_dir.mkdir(parents=True, exist_ok=True)
    doc = _open_pdf(pdf_path)
    try:
        total = doc.page_count
        page_nos = _parse_pages_arg(pages, total)
        if len(page_nos) > PDF_RENDER_MAX_PAGES:
            raise ValueError(
                f"单次最多渲染 {PDF_RENDER_MAX_PAGES} 页（请求 {len(page_nos)} 页）；"
                f"文件共 {total} 页，可用 pages 参数分段渲染（如 pages=\"1-20\"、pages=\"21-40\"）。"
            )
        existing = [p for p in cache_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".png"]
        if len(existing) + len(page_nos) > PDF_CACHE_MAX_IMAGES:
            raise ValueError(
                f"该 PDF 的页图缓存已达上限（{PDF_CACHE_MAX_IMAGES} 张）；"
                "请删除 data/uploads/pdf_pages 下本文件缓存，或更换更小的 PDF。"
            )
        from naiba.storage.media import _ensure_webp_thumb

        results: list[dict[str, str]] = []
        for page_no in page_nos:
            target = cache_dir / f"page_{page_no:03d}.png"
            if not target.is_file() or target.stat().st_size <= 0:
                page = doc[page_no - 1]
                scale = _render_scale(page, PDF_RENDER_MAX_EDGE)
                pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                tmp = target.with_suffix(".png.part")
                pix.save(str(tmp), output="png")
                tmp.replace(target)
                pix = None
            thumb = _ensure_webp_thumb(target, imaging)
            results.append({
                "page": page_no,
                "path": str(target),
                "thumb_path": thumb or str(target),
            })
        return {"total_pages": total, "pages": results}
    finally:
        doc.close()


def _parse_pages_arg(pages: Any, total: int) -> list[int]:
    """解析 pages 参数：None → 前 20 页；"1-5,8" → [1,2,3,4,5,8]；列表/整数。"""
    if pages is None or pages == "":
        return list(range(1, min(total, PDF_RENDER_MAX_PAGES) + 1))
    if isinstance(pages, int):
        raw = [pages]
    elif isinstance(pages, list):
        raw = [int(item) for item in pages if str(item).strip() != ""]
    else:
        raw = []
        for token in str(pages).split(","):
            token = token.strip()
            if not token:
                continue
            if "-" in token and len(token.split("-")) == 2:
                a, b = token.split("-", 1)
                a, b = int(a.strip()), int(b.strip())
                if a > b:
                    raise ValueError(f"页码范围无效：{token}")
                raw.extend(range(a, b + 1))
            else:
                raw.append(int(token))
    seen: list[int] = []
    for page_no in raw:
        if page_no < 1 or page_no > total:
            raise ValueError(f"页码越界：共 {total} 页，pages 含 {page_no}")
        if page_no not in seen:
            seen.append(page_no)
    seen.sort()
    if not seen:
        raise ValueError("pages 参数为空")
    return seen


# ---- 局部高清渲染 ----

def _parse_region(region: Any) -> tuple[float, float, float, float]:
    """区域解析：半区关键词（top/bottom/left/right/middle）或 'x1,y1,x2,y2'（页面百分比 0-100）。"""
    value = str(region or "").strip().lower()
    if value in PDF_REGION_KEYWORDS:
        return PDF_REGION_KEYWORDS[value]
    parts = [part for part in re.split(r"[,，;；\s]+", value) if part]
    if len(parts) != 4:
        raise ValueError(f"region 格式无效：{region!r}（应为 'x1,y1,x2,y2' 百分比或 top/bottom/left/right/middle）")
    try:
        x1, y1, x2, y2 = (float(part) for part in parts)
    except ValueError as exc:
        raise ValueError(f"region 含非数字：{region!r}") from exc
    for num, label in ((x1, "x1"), (y1, "y1"), (x2, "x2"), (y2, "y2")):
        if num < 0 or num > 100:
            raise ValueError(f"region 的 {label} 必须在 0-100 之间：{region!r}")
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"region 无效：右下角必须大于左上角 {region!r}")
    return x1, y1, x2, y2


def render_pdf_region(
    pdf_path: Path,
    data_dir: Path,
    page: int,
    region: Any,
    scale: int = 3,
    imaging: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """页面局部高清渲染（矢量源高倍重渲）：用于整页图细节看不清时的精读材料。

    返回 {page, region, scale, path, thumb_path}；区域/页码越界、区域过小、scale 越界均明确报错。
    """
    if isinstance(scale, bool) or not isinstance(scale, (int, float)):
        raise ValueError("scale 必须是数字")
    scale = int(scale)
    if scale < 1 or scale > 6:
        raise ValueError("scale 必须在 1-6 之间")
    x1, y1, x2, y2 = _parse_region(region)
    if (x2 - x1) < PDF_ZOOM_MIN_RATIO * 100 or (y2 - y1) < PDF_ZOOM_MIN_RATIO * 100:
        raise ValueError(f"区域过小（至少占页面宽/高 5%）：region={region!r}")
    doc = _open_pdf(pdf_path)
    try:
        total = doc.page_count
        if page < 1 or page > total:
            raise ValueError(f"页码越界：共 {total} 页，page={page}")
        page_obj = doc[page - 1]
        pwidth, pheight = page_obj.rect.width, page_obj.rect.height
        clip = pymupdf.Rect(
            x1 / 100 * pwidth, y1 / 100 * pheight,
            x2 / 100 * pwidth, y2 / 100 * pheight,
        )
        clip_w, clip_h = clip.width, clip.height
        # 输出长边 ≤ PDF_ZOOM_MAX_EDGE：超限时按比例降 scale（保证图不超大）。
        max_w = max(clip_w, clip_h)
        adjusted = min(float(scale), PDF_ZOOM_MAX_EDGE / max_w if max_w else float(scale))
        cache_dir = _pdf_cache_dir(data_dir, pdf_path)
        cache_dir.mkdir(parents=True, exist_ok=True)
        slug = f"p{page:03d}_r_{x1:g}-{y1:g}-{x2:g}-{y2:g}_s{int(adjusted)}"
        target = cache_dir / f"{slug}.png"
        if not target.is_file() or target.stat().st_size <= 0:
            pix = page_obj.get_pixmap(
                clip=clip, matrix=pymupdf.Matrix(adjusted, adjusted), alpha=False
            )
            tmp = target.with_suffix(".png.part")
            pix.save(str(tmp), output="png")
            tmp.replace(target)
            pix = None
        from naiba.storage.media import _ensure_webp_thumb

        thumb = _ensure_webp_thumb(target, imaging)
        return {
            "page": page,
            "region": f"{x1:g},{y1:g},{x2:g},{y2:g}",
            "scale": int(adjusted),
            "path": str(target),
            "thumb_path": thumb or str(target),
        }
    finally:
        doc.close()
