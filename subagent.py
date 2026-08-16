"""同进程子 Agent（Harness 级，Section 5）。

不创建独立进程。每个子 Agent 具备独立会话上下文、独立 Agent Loop、独立工具作用域，
并记录 parent_job_id / parent_session_id、独立取消信号，在 Job Registry 中可观察。

安全限制：
- 最大委派深度默认 2（主运行 depth=0，子 Agent depth=1，子 Agent 不能再派生子 Agent）。
- 单个父 Agent 最多 4 个子 Agent。
- 子 Agent 继承工作目录但不能扩大权限，默认不能继续创建子 Agent。
- 父 Agent 取消时递归取消所有子任务。
"""
from __future__ import annotations

import traceback
from typing import Any, Callable

from job_registry import JobRegistry, JobSpec

MAX_SUBAGENT_DEPTH = 2
MAX_CHILDREN_PER_PARENT = 4

# 子 Agent 默认开放的工具（排除任务/子 Agent 管理类工具，防止递归委派与越权作业管理）
SUBAGENT_ALLOWED_TOOLS = [
    "read_file",
    "list_directory",
    "search_files",
    "write_file",
    "run_command",
    "run_skill_script",
    "http_request",
    "call_mcp",
]

SUBAGENT_BLOCKED_TOOLS = {
    "register_mcp",
    "subagent",
    "run_in_background",
    "job_output",
    "job_status",
    "job_wait",
    "job_kill",
}


def run_subagent_agent(
    app: Any,
    job_id: str,
    spec: JobSpec,
    cancel: Any,
    emit: Callable[[dict[str, Any]], None],
) -> None:
    """子 Agent 运行器：以隔离上下文执行一次 Agent Loop，结果写回 Job。"""
    from server import build_model_history
    from skill_runtime import SkillAgent, TaskCancelled
    from plan_runtime import CraftToolExecutor

    conversation_id = str(spec.conversation_id or "")
    params = spec.params or {}
    instruction = str(params.get("instruction") or "")
    if not instruction:
        app.storage.update_job(job_id, result={"error": "缺少 instruction"})
        return
    conversation = app.storage.get_conversation(conversation_id)
    if not conversation:
        app.storage.update_job(job_id, result={"error": "对话已删除"})
        return
    history = build_model_history(conversation.get("messages", []))
    model_key = str(conversation.get("model_key") or "")
    if not model_key:
        provider_id = str(conversation.get("provider_id") or "")
        model_key = f"online:{provider_id}" if provider_id else ""
    profile = app.config.profile(model_key)
    options = dict(app.config.generation_options())
    options["stream"] = bool(conversation.get("stream_enabled", 1))
    agent = app.config.get_agent(str(conversation.get("agent_id") or "")) or {}
    agent_prompt = str(agent.get("system_prompt") or "").strip() or str(
        app.config.data.get("agent_system_prompt", "")
    )
    conversation_prompt = str(conversation.get("system_prompt") or "").strip()
    combined_prompt = "\n\n".join(item for item in (agent_prompt, conversation_prompt) if item)
    requested = [str(t) for t in (params.get("allowed_tools") or [])]
    allowed_tools = [t for t in requested if t not in SUBAGENT_BLOCKED_TOOLS] or SUBAGENT_ALLOWED_TOOLS
    allowed_tools = [t for t in allowed_tools if t not in SUBAGENT_BLOCKED_TOOLS]

    emit({"type": "job_status", "status": "running", "current_step": "子 Agent 推理中"})
    worker = SkillAgent(app.catalog, CraftToolExecutor(app.executor), app.models.complete)
    run_context = {
        "run_id": job_id,
        "job_id": job_id,
        "conversation_id": conversation_id,
        "owner_session_id": spec.owner_session_id or conversation_id,
        "parent_job_id": spec.parent_job_id or "",
        "depth": 1,
    }
    try:
        content, runs, reasonings, usage = worker.run(
            instruction,
            history,
            profile,
            options,
            False,
            [str(i) for i in agent.get("skill_ids", [])],
            combined_prompt,
            allowed_tools,
            emit,
            lambda tool, args, result, success: app.storage.log_tool_run(
                conversation_id, tool, args, result, success
            ),
            cancel,
            max_steps=int(app.config.data.get("agent_max_steps", 32)),
            tool_registry=app.tool_registry,
            run_context=run_context,
        )
        app.storage.update_job(
            job_id,
            result={"response": content, "tool_runs": runs, "usage": usage},
        )
        emit({"type": "subagent_result", "response": content[:2000]})
    except TaskCancelled:
        emit({"type": "subagent_cancelled"})
        raise
    except Exception as exc:
        traceback.print_exc()
        app.storage.update_job(job_id, result={"error": str(exc)})


def subagent_handler_factory(app: Any) -> Callable[..., tuple[bool, str]]:
    """构造 ``subagent`` 系统工具处理器。"""

    def handler(
        arguments: dict[str, Any],
        active_skills: list[dict[str, Any]],
        run_context: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        ctx = run_context or {}
        parent_depth = int(ctx.get("depth", 0) if isinstance(ctx.get("depth"), int) else 0)
        if parent_depth >= MAX_SUBAGENT_DEPTH - 1:
            return False, "子 Agent 不能再创建子 Agent（已达到最大委派深度）"
        conversation_id = str(ctx.get("conversation_id") or "")
        if not conversation_id:
            return False, "无法确定子 Agent 所属对话"
        parent_job_id = str(ctx.get("run_id") or ctx.get("job_id") or "")
        owner = str(ctx.get("owner_session_id") or conversation_id)

        # 单父最多 4 个子 Agent
        children = [
            j for j in app.jobs.list(owner=owner)
            if str(j.get("parent_job_id") or "") == parent_job_id
        ]
        if len(children) >= MAX_CHILDREN_PER_PARENT:
            return False, f"单父 Agent 最多 {MAX_CHILDREN_PER_PARENT} 个子 Agent"

        instruction = str((arguments or {}).get("instruction") or "").strip()
        if not instruction:
            return False, "缺少 instruction 参数"
        label = str((arguments or {}).get("label") or "")[:120] or "子 Agent 任务"
        spec = JobSpec(
            kind="subagent",
            conversation_id=conversation_id,
            params={
                "instruction": instruction,
                "allowed_tools": [str(t) for t in (arguments or {}).get("allowed_tools", [])],
            },
            label=label,
            parent_job_id=parent_job_id or None,
            owner_session_id=owner,
        )
        job_id = app.jobs.start(spec, owner=owner)
        return True, f"子 Agent 已启动，Job ID：{job_id}（可用 job_wait 等待结果）"

    return handler


def job_tool_handler_factory(app: Any) -> dict[str, Callable[..., tuple[bool, str]]]:
    """构造 run_in_background / job_output / job_status / job_wait / job_kill 处理器。"""
    jobs: JobRegistry = app.jobs

    def _owner(ctx: dict[str, Any] | None) -> str:
        ctx = ctx or {}
        return str(ctx.get("owner_session_id") or ctx.get("conversation_id") or "")

    def run_in_background(arguments, active_skills, run_context=None):
        ctx = run_context or {}
        conversation_id = str(ctx.get("conversation_id") or "")
        if not conversation_id:
            return False, "无法确定 Job 所属对话"
        spec_dict = (arguments or {}).get("spec") or {}
        if not isinstance(spec_dict, dict) or not spec_dict.get("kind"):
            return False, "spec 必须包含 kind"
        owner = _owner(ctx)
        spec = JobSpec(
            kind=str(spec_dict["kind"]),
            conversation_id=conversation_id,
            params=dict(spec_dict.get("params", {})),
            label=str(spec_dict.get("label") or "")[:120],
            parent_job_id=str(ctx.get("run_id") or ctx.get("job_id") or "") or None,
            owner_session_id=owner,
            resumable=bool(spec_dict.get("resumable", False)),
            checkpoint=spec_dict.get("checkpoint"),
        )
        job_id = jobs.start(spec, owner=owner)
        return True, f"Job 已提交，Job ID：{job_id}（可用 job_wait 等待结果）"

    def job_output(arguments, active_skills, run_context=None):
        owner = _owner(run_context)
        job_id = str((arguments or {}).get("job_id") or "")
        if not job_id:
            return False, "缺少 job_id"
        out = jobs.read(job_id, int((arguments or {}).get("cursor", 0) or 0), owner=owner)
        events = out.get("events", [])
        text = "\n".join(
            str(e.get("line") or e.get("message") or e.get("content") or "") for e in events
        )
        return True, text or "（暂无增量输出）"

    def job_status(arguments, active_skills, run_context=None):
        owner = _owner(run_context)
        job_id = str((arguments or {}).get("job_id") or "")
        if not job_id:
            return False, "缺少 job_id"
        job = jobs.get(job_id, owner=owner)
        if not job:
            return False, "Job 不存在或无权访问"
        return True, (
            f"Job {job_id}：status={job['status']}, progress={job['progress']:.0f}%, "
            f"step={job['current_step']}, attempt={job['attempt']}"
        )

    def job_wait(arguments, active_skills, run_context=None):
        owner = _owner(run_context)
        job_id = str((arguments or {}).get("job_id") or "")
        if not job_id:
            return False, "缺少 job_id"
        timeout = int((arguments or {}).get("timeout", 600) or 600)
        job = jobs.wait(job_id, timeout, owner=owner)
        if not job:
            return False, "Job 不存在或无权访问"
        if job["status"] == "completed":
            return True, f"Job 已完成：{json_dumps(job.get('result', {}))[:2000]}"
        return False, f"Job 结束（{job['status']}）：{job.get('error', '')}"

    def job_kill(arguments, active_skills, run_context=None):
        owner = _owner(run_context)
        job_id = str((arguments or {}).get("job_id") or "")
        if not job_id:
            return False, "缺少 job_id"
        job = jobs.cancel(job_id, owner=owner, reason=str((arguments or {}).get("reason") or "用户取消"))
        if not job:
            return False, "Job 不存在或无权访问"
        return True, f"已发送取消信号给 Job {job_id}"

    return {
        "run_in_background": run_in_background,
        "job_output": job_output,
        "job_status": job_status,
        "job_wait": job_wait,
        "job_kill": job_kill,
    }


def json_dumps(value: Any) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
