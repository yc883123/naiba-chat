"""消息 metadata 契约常量（自 naiba.core.contracts 迁出；收官线 ③）。

写入方（run/chat、run/manager、plans）与重放方（core/history）共用同一键契约——
新增 metadata 键必须先在这里登记再使用（哲学指引 §0.6 第②条"边显式化"）。
``naiba.core.contracts`` 保留 re-export 兼容，既有导入零改动。
"""
from __future__ import annotations

# 全部 metadata 键的权威清单（新增键同步更新本表与 MetadataKeys）。
MESSAGE_METADATA_KEYS: tuple[str, ...] = (
    "attachments",
    "attachments_truncated",
    "reasoning",
    "tool_runs",
    "trace",
    "usage",
    "files",
    "plan_id",
    "plan_step",
    "plan_step_title",
    "aborted",
    "partial",
    "error",
    "run_id",
    "agent_id",
    "display_content",
)


class MetadataKeys:
    """消息 metadata JSON 键（写入方 async_tasks / 重放方 core.history 共用契约）。"""

    ATTACHMENTS = "attachments"
    # 消息级媒体分桶截断的自述信息（{"total","shown","kinds"}）：超出上限时不静默，
    # 前端在媒体网格下方渲染"共 N 张，仅显示前 M 张"。
    ATTACHMENTS_TRUNCATED = "attachments_truncated"
    REASONING = "reasoning"
    TOOL_RUNS = "tool_runs"
    TRACE = "trace"
    USAGE = "usage"
    FILES = "files"
    PLAN_ID = "plan_id"
    PLAN_STEP = "plan_step"
    PLAN_STEP_TITLE = "plan_step_title"
    ABORTED = "aborted"
    PARTIAL = "partial"
    ERROR = "error"
    RUN_ID = "run_id"
    AGENT_ID = "agent_id"
    DISPLAY_CONTENT = "display_content"
