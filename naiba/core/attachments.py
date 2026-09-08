"""消息附件汇总与用户轮次文本拼装（纯函数层）。

职责边界（P2 收口后）：
- **媒体采集**（扫工具结果、托管缓存、缩略图、去重、落盘前分桶预截断）归
  ``storage/media_collect.py``——它只在工具产出的那一刻运行，结果写进该次
  ``tool_run["media"]``（原始 result 只有那里可见）。
- 本模块只做**纯计算**：用户轮次模型可见文本（``compose_user_content``）、
  图像意图判定（``_image_intent``）、把各次工具调用的媒体记录汇总成消息级
  ``metadata.attachments``（``union_run_media``）。
- 名单唯一定义在 ``core/media_types.py``；这里只做 re-export 兼容。
"""

from __future__ import annotations

import re
from typing import Any

from naiba.core.media_types import MEDIA_EXTS, is_media_path, truncate_by_kind
from naiba.core.paths import path_within  # noqa: F401  (re-export for callers)

# 多媒体产物（图片/视频/音频）走消息内产物卡片预览
# （tool_run.media → metadata.attachments → mediaMarkup），不列入"修改文件"总结，
# 避免同一产物出现两套入口。名单唯一定义在 core/media_types.py
# （前端经 /api/bootstrap.media_exts 取同一份）。
MEDIA_PRODUCT_EXTS = MEDIA_EXTS


def _is_media_product_path(raw: str) -> bool:
    """按扩展名判断文件是否属于多媒体产物（图片/视频/音频）。"""
    return is_media_path(raw)


# 仅附件、无文字的用户轮次：模型侧显式说明"用户没写指令"，避免模型自行编造用户诉求。
ATTACHMENT_ONLY_NOTICE = "[用户未输入文字，只发送了以下附件]"


def upload_reference_lines(uploads: list[dict[str, Any]]) -> list[str]:
    """用户上传附件的模型侧引用行（_run_chat 与历史重放共用，保证逐字节一致）。

    PDF 附件追加固定处理指引：提取文本用 read_pdf；扫描版/看图用 pdf_render_pages
    渲染页图后 vision_analyze；细节不清时 pdf_zoom_region 局部放大。
    该文本条件出现、每轮稳定，不改变非 PDF 会话的前缀。
    """
    lines: list[str] = []
    for item in uploads or []:
        if not isinstance(item, dict):
            continue
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


def compose_user_content(message: str, uploads: list[dict[str, Any]]) -> str:
    """用户轮次的模型可见文本（_run_chat 与历史重放共用的唯一拼接口径）。

    非空文字：保持历史口径逐字节不变（``文字 + "\\n" + 引用行``），前缀缓存不受影响；
    纯附件（用户未输入文字）：以固定提示行替代空文字，再接附件引用行，使模型明确
    "本轮只有附件、没有指令"，而不是自行脑补诉求。
    """
    text = str(message or "")
    lines = upload_reference_lines(uploads)
    if not lines:
        return text
    body = "\n".join(lines)
    if text.strip():
        return f"{text}\n{body}"
    return f"{ATTACHMENT_ONLY_NOTICE}\n{body}"


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
    """用户是否明确要求查看/列出/查找图片（据此决定枚举类工具返回的图片是否显示）。

    必须同时命中"图片词"与"查看/列出/查找/确认类动作词"，才算图像意图，减少误伤。
    结果经 run_context["media_intent"] 传给采集器，由工具声明 ``intent_gated`` 消费
    （不再按工具名硬编码枚举集合）。
    """
    t = str(text or "")
    return bool(_IMAGE_MEDIA_TERM_RE.search(t) and _IMAGE_VIEW_ACTION_RE.search(t))


def union_run_media(
    runs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """把本轮各次工具调用的媒体记录汇总为消息级附件列表（含分桶截断）。

    数据来源是各 run 的 ``media``（由 ``storage/media_collect`` 在工具产出时按声明
    提取并托管缓存）；**不再扫描结果文本**——取消/失败路径的 runs 由事件重建，
    同样带 media，三条收尾路径口径一致。

    去重口径：同来源只留一份；同名（不同来源）优先保留带缩略图的版本。
    截断口径：按类型分桶（图 20 / 视频 8 / 音频 8），返回可自述的 truncated 信息
    （调用方写进 ``metadata.attachments_truncated``，前端渲染提示块）。
    """
    ordered: list[dict[str, Any]] = []
    seen_source: set[str] = set()
    index_by_name: dict[str, int] = {}
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        for record in run.get("media") or []:
            if not isinstance(record, dict):
                continue
            source = str(record.get("source") or "")
            if not source or source in seen_source:
                continue
            name_key = str(record.get("name") or "").strip().lower()
            if name_key and name_key in index_by_name:
                index = index_by_name[name_key]
                if record.get("thumb_path") and not ordered[index].get("thumb_path"):
                    ordered[index] = record
                continue
            seen_source.add(source)
            if name_key:
                index_by_name[name_key] = len(ordered)
            ordered.append(record)
    return truncate_by_kind(ordered)
