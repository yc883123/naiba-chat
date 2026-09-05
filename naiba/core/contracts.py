"""跨模块契约定义（重构哲学指引 §0.6 第②条"边显式化"的载体）。

- ``RunContext``：run/工具/技能/视觉层共享的"上下文袋"契约（原裸字典，16 键 + 扩展键）。
  TypedDict 方案：运行时仍是 dict（行为零变化），但在类型层面把键与消费方钉死；
  阶段 2 切分 async_tasks 时可平滑升级为带默认值/校验的 dataclass。
- ``EventType``：前后端事件流 type 枚举。前端可处置的种类必须 ⊆ 本枚举
  （测试 test_contracts 从 public/app.js 反查校验，防止两端漂移）。
- ``MetadataKeys``：消息 metadata JSON 键常量（写入方与重放方的唯一契约来源）。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, TypedDict


class RunContext(TypedDict, total=False):
    """一轮执行（Run/Job/子 Agent）的共享上下文。所有键均可选：不同执行形态按需填充。"""

    run_id: str                      # 本轮 Run id
    job_id: str                      # 本 Job id（子 Agent/后台任务形态）
    conversation_id: str             # 会话 id
    owner_session_id: str            # 归属会话（job_kill 的读写边界）
    parent_job_id: str               # 父 Job id（父子级联取消用）
    depth: int                       # 子 Agent 嵌套深度
    allowed_tools: list[str]         # 本轮可用工具集
    skill_policy: dict[str, Any]     # Skill 策略（冻结集/引用集）
    job_registry: Any                # JobRegistry（后台 Job 工具用）
    executor: Any                    # ToolExecutor（工具执行与权限确认）
    cancel_event: Any                # threading.Event（取消信号）
    vision_budget: Any               # VisionBudget（本轮视觉预算）
    interaction_mode: str            # craft / plan / ask
    routing_message: str             # 触发本轮的用户消息
    pull_interjections: Any          # 拉取待发插话回调
    mark_interjections_consumed: Any # 标记插话已消费回调
    # ---- 扩展键（生产方写入、消费方读取，属既有隐式协议，一并显式化）----
    mcp_active: bool                 # 本轮是否激活 MCP（skills 写入）
    trace_messages: list[Any]        # 本轮 trace 原样消息（技能层写入）
    plan_exit_content: str           # Plan 出口内容（async_tasks 读取）
    plan_step_title: str             # 当前计划步骤标题（metadata 用）


class EventType(str, Enum):
    """流式事件 type 契约（前端无线变更；新增事件必须先加进这里）。"""

    # ---- 对话流（前端 handleChatEvent 可处置）----
    DELTA = "delta"
    REASONING = "reasoning"
    REASONING_START = "reasoning_start"
    REASONING_DELTA = "reasoning_delta"
    REASONING_END = "reasoning_end"
    STATUS = "status"
    SKILLS = "skills"
    SKILL_WARNING = "skill_warning"
    TOOLS_AVAILABLE = "tools_available"
    TOOL_START = "tool_start"
    TOOL_START_LEGACY = "tool_start_legacy"
    TOOL_RESULT = "tool_result"
    TOOL_RESULT_LEGACY = "tool_result_legacy"
    TOOL_CONFIRM = "tool_confirm"
    CHOICE = "choice"
    CANCELLED = "cancelled"
    RUN_FAILED = "run_failed"
    RUN_STARTED = "run_started"
    CONTEXT_FULL = "context_full"
    DONE = "done"
    ERROR = "error"
    USER_GUIDANCE = "user_guidance"
    INTERJECTION_CONSUMED = "interjection_consumed"
    RESPONSE_RETRACTED = "response_retracted"
    VISION_START = "vision_start"
    VISION_DONE = "vision_done"
    VISION_ERROR = "vision_error"
    DEBUG_CACHE = "debug_cache"
    HEARTBEAT = "heartbeat"

    # ---- Job 事件流（任务弹窗）----
    JOB_STATUS = "job_status"
    JOB_FINISHED = "job_finished"
    JOB_LOG = "job_log"
    JOB_CHECK = "job_check"

    # ---- Agent 循环内部事件 ----
    STEP_STARTED = "step_started"
    STEP_FINISHED = "step_finished"
    PARSE_ERROR = "parse_error"
    RETRY = "retry"
    MODEL_REQUEST = "model_request"
    RUN_COMPLETED = "run_completed"
    RUN_CANCELLED = "run_cancelled"
    SUBAGENT_RESULT = "subagent_result"
    SUBAGENT_CANCELLED = "subagent_cancelled"
    USER_INTERJECTION_EDITED = "user_interjection_edited"
    USER_INTERJECTION_DELETED = "user_interjection_deleted"


class MetadataKeys:
    """消息 metadata JSON 键（写入方 async_tasks / 重放方 core.history 共用契约）。"""

    ATTACHMENTS = "attachments"
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
