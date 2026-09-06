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
from typing import Any, Protocol, TypedDict, runtime_checkable

import threading  # noqa: F401  (供 run_context 键类型引用；协议成员注解用)

RUN_CONTEXT_KEYS: tuple[str, ...] = (
    "run_id", "job_id", "conversation_id", "owner_session_id", "parent_job_id",
    "depth", "allowed_tools", "skill_policy", "job_registry", "executor",
    "cancel_event", "vision_budget", "interaction_mode", "routing_message",
    "mcp_active", "trace_messages", "plan_exit_content", "plan_step_title",
)

# 运行期保持 dict 形态（零行为变化）；"带默认值/校验"经由工厂与校验函数落地，
# 避免把 TypedDict 硬切成 dataclass 的改写型手术风险（哲学⑥ 守则④）。
def default_run_context() -> dict[str, Any]:
    """构造带默认值的 RunContext（缺失键由消费方按需读取，字段按类型入位）。"""
    return {
        "run_id": "",
        "job_id": "",
        "conversation_id": "",
        "owner_session_id": "",
        "parent_job_id": "",
        "depth": 0,
        "allowed_tools": [],
        "skill_policy": {},
        "job_registry": None,
        "executor": None,
        "cancel_event": None,
        "vision_budget": None,
        "interaction_mode": "craft",
        "routing_message": "",
        "mcp_active": False,
        "trace_messages": [],
        "plan_exit_content": "",
        "plan_step_title": "",
    }


def validate_run_context(ctx: Any) -> list[str]:
    """校验 RunContext：返回非法键清单（空列表=合法）。

    只做白名单与键名检查；类型错误由各消费方按既有防御式读取兜底（不改变运行行为）。
    """
    if not isinstance(ctx, dict):
        return ["<not-a-dict>"]
    return sorted(set(ctx.keys()) - set(RUN_CONTEXT_KEYS))


@runtime_checkable
class ConfigView(Protocol):
    """运行时对 config 的最小访问面（构建时校验；成员 ⊆ ConfigStore 实际面）。"""

    data: dict[str, Any]
    lock: Any

    def profile(self, model_key: str) -> dict[str, Any]: ...
    def resolve_data_dir(self, raw: str | None = None) -> Any: ...
    def resolve_workspace_dir(self, raw: str | None = None) -> Any: ...
    def resolve_managed_skills_dir(self, raw: str | None = None) -> Any: ...
    def get_agent(self, agent_id: str) -> dict[str, Any] | None: ...
    def default_agent_id(self) -> str: ...
    def workspace_dir_for_group(self, name: str) -> str: ...
    def get_hidden_skill_ids(self) -> list[str]: ...
    def public_agents(self) -> list[dict[str, Any]]: ...
    def update_settings(self, body: dict[str, Any]) -> dict[str, Any]: ...
    def save(self) -> None: ...


@runtime_checkable
class AppContext(Protocol):
    """组装根的最小访问面（NaibaChatApp 实例即满足；运行时模块按此注入而非 Any）。"""

    config: ConfigView
    paths: Any
    storage: Any
    runs: Any
    tasks: Any
    jobs: Any
    plans: Any
    catalog: Any
    vision: Any
    web_search: Any
    models: Any
    executor: Any
    tool_registry: Any
    updater: Any
    capabilities: Any
    mcp: Any
    listener_host: str
    update_restart_callback: Any
    data_migration: dict[str, Any]


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


class EventPayload(TypedDict, total=False):
    """流式事件负载契约（各事件 type 字段的并集；wire 版式零变化，仅类型标注）。

    实际负载键与前端消费键的一致性由 tests/test_contracts 以 golden 基线反查校验：
    任何 emit 负载出现新键而本契约未登记，即判定为漂移；新增事件字段必须先加进这里。
    """

    type: str
    message: str
    content: str
    reasoning: str
    text: str
    # 对话流 / 状态
    run_id: str
    sequence: int
    status: str
    step: int
    attempt: int
    limit: int
    used: int
    budget: int
    reason: str
    # 工具流
    tool: str
    tool_name: str
    tool_desc: str
    arguments: dict[str, Any]
    confirm_id: str
    success: bool
    tools: list[dict[str, Any]]
    skills: list[dict[str, Any]]
    choices: list[str]
    choice_groups: list[dict[str, Any]]
    plan: dict[str, Any]
    todos: list[dict[str, Any]]
    aborted_message: dict[str, Any]
    # 视觉 / 诊断
    backend: str
    image_count: int
    started_at: int
    label: str
    lines: list[str]
    # Job 流
    kind: str
    phase: str
    handle: str
    next_check_in: int
    shot: int
    total: int
    files: list[str]
    index: int
    prompt_id: str
    line: str
    # 子 Agent / 计划
    current_step: str
    response: str
    error: str
    result: str


# 已迁往 naiba.core.messages（收官线 ③；保留 re-export 兼容，既有导入零改动）。
from naiba.core.messages import MESSAGE_METADATA_KEYS, MetadataKeys  # noqa: E402,F401
