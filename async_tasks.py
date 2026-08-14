from __future__ import annotations

import threading
import traceback
from pathlib import Path
from typing import Any

from model_runtime import ModelRuntime
from plan_runtime import ASK_MODE_PROMPT, ReadOnlyToolExecutor, resolve_mode_tools
from skill_runtime import SkillAgent, TaskCancelled


class BackgroundTaskManager:
    ACTIVE = {"queued", "running", "waiting", "cancelling"}

    def __init__(self, app: Any):
        self.app = app
        self._lock = threading.RLock()
        self._events: dict[str, threading.Event] = {}

    def submit(self, body: dict[str, Any]) -> dict[str, Any]:
        conversation_id = str(body.get("conversation_id") or "")
        message = str(body.get("message") or "").strip()
        if not conversation_id or not message:
            raise ValueError("conversation_id 和 message 不能为空")
        attachments = body.get("attachments") or []
        if not isinstance(attachments, list):
            raise ValueError("attachments 必须是数组")
        conversation = self.app.storage.get_conversation(conversation_id)
        if not conversation:
            raise LookupError("对话不存在")
        agent = self.app.config.get_agent(str(conversation.get("agent_id") or ""))
        if not agent:
            agent = self.app.config.get_agent(self.app.config.default_agent_id()) or {
                "id": "", "name": "Agent", "system_prompt": "", "skill_ids": []
            }
        agent = {
            "id": str(agent.get("id") or ""),
            "name": str(agent.get("name") or "Agent"),
            "system_prompt": str(agent.get("system_prompt") or ""),
            "skill_ids": [str(item) for item in agent.get("skill_ids", [])],
        }
        # 提交时固化交互模式与计划 ID：运行中切换 UI 不影响已开始的任务
        interaction_mode = str(conversation.get("interaction_mode") or "craft")
        if interaction_mode not in ("craft", "plan", "ask"):
            interaction_mode = "craft"
        plan_id = ""
        if interaction_mode == "plan":
            plan_id = str(self.app.plans.ensure_active_plan(conversation_id, message)["id"])
        snapshot = {
            "agent": agent,
            "conversation_system_prompt": str(conversation.get("system_prompt") or ""),
            "provider_id": str(conversation.get("provider_id") or ""),
            "stream_enabled": bool(conversation.get("stream_enabled", 1)),
            "model_key": str(body.get("model_key") or ""),
            "auto_skills": bool(body.get("auto_skills", False)),
            "skill_ids": [str(item) for item in body.get("skill_ids", [])],
            "attachments": attachments,
            "interaction_mode": interaction_mode,
            "plan_id": plan_id,
        }
        self.app.storage.add_message(conversation_id, "user", message, {
            "attachments": attachments, "background_task": True, "agent_id": agent["id"]
        })
        task = self.app.storage.create_background_task(conversation_id, message, agent, snapshot)
        event = threading.Event()
        with self._lock:
            self._events[task["id"]] = event
        threading.Thread(target=self._run, args=(task["id"], snapshot, event), daemon=True).start()
        return task

    def list(self, conversation_id: str = "", active_only: bool = False) -> list[dict[str, Any]]:
        return self.app.storage.list_background_tasks(conversation_id, active_only)

    def cancel(self, task_id: str) -> dict[str, Any] | None:
        task = self.app.storage.get_background_task(task_id)
        if not task:
            return None
        if task["status"] in self.ACTIVE:
            with self._lock:
                event = self._events.get(task_id)
            if event:
                event.set()
            return self.app.storage.update_background_task(
                task_id, status="cancelling", cancel_requested=True,
                detail={"message": "正在取消任务"},
            )
        return task

    def _run(self, task_id: str, snapshot: dict[str, Any], cancel_event: threading.Event) -> None:
        task = self.app.storage.get_background_task(task_id)
        if not task:
            return
        conversation_id = task["conversation_id"]
        detail: dict[str, Any] = {"message": "任务已排队"}
        skills: list[dict[str, str]] = []

        def check() -> None:
            if cancel_event.is_set():
                raise TaskCancelled("任务已取消")

        def event(payload: dict[str, Any]) -> None:
            nonlocal detail, skills
            check()
            kind = str(payload.get("type") or "")
            if kind == "skills":
                skills = [{"id": str(x.get("id") or ""), "name": str(x.get("name") or "")} for x in payload.get("skills", []) if isinstance(x, dict)]
                detail = {"message": "已启用 Skill", "skills": skills}
            elif kind == "status":
                detail = {"message": str(payload.get("message") or "正在执行")}
            elif kind == "tool_start":
                detail = {"message": f"正在执行 {payload.get('tool') or '工具'}", "tool": str(payload.get("tool") or "")}
            elif kind == "tool_confirm":
                detail = {"message": "等待工具确认", "tool": str(payload.get("tool_name") or ""), "confirm_id": str(payload.get("confirm_id") or "")}
                self.app.storage.update_background_task(task_id, status="waiting", detail=detail)
                return
            self.app.storage.update_background_task(task_id, status="cancelling" if cancel_event.is_set() else "running", detail=detail)

        try:
            self.app.storage.update_background_task(task_id, status="running", started=True, detail={"message": "任务开始执行"})
            check()
            conversation = self.app.storage.get_conversation(conversation_id)
            if not conversation:
                raise RuntimeError("发起任务的对话已删除")
            message = task["message"]
            uploads = snapshot.get("attachments") or []
            extra = [f"[用户上传文件：{x.get('path')}]" for x in uploads if x.get("path")]
            effective = message + (("\n" + "\n".join(extra)) if extra else "")
            from server import build_model_history

            history = build_model_history(conversation.get("messages", []))
            model_key = str(snapshot.get("model_key") or "")
            if not model_key and snapshot.get("provider_id"):
                model_key = f"online:{snapshot['provider_id']}"
            profile = self.app.config.profile(model_key)
            options = self.app.config.generation_options()
            options["stream"] = bool(snapshot.get("stream_enabled", True))
            agent = snapshot["agent"]
            selected = list(dict.fromkeys(agent.get("skill_ids", []) + snapshot.get("skill_ids", [])))
            prompt = "\n\n".join(x for x in (agent.get("system_prompt", "").strip(), snapshot.get("conversation_system_prompt", "").strip()) if x)
            mode = str(snapshot.get("interaction_mode") or "craft")
            plan_id = str(snapshot.get("plan_id") or "")
            allowed_tools = resolve_mode_tools(mode, [str(x) for x in self.app.config.data.get("agent_tools", [])])
            executor = self.app.executor if mode == "craft" else ReadOnlyToolExecutor(self.app.executor)
            if mode == "ask":
                prompt = (prompt + "\n\n" + ASK_MODE_PROMPT).strip()
            elif mode == "plan":
                plan = self.app.plans.get(plan_id) if plan_id else None
                prompt = (prompt + "\n\n" + self.app.plans.prepare_prompt(plan)).strip()
            runtime = ModelRuntime()
            worker = SkillAgent(self.app.catalog, executor, runtime.complete)
            response, runs, reasonings, usage = worker.run(
                effective, history, profile, options, bool(snapshot.get("auto_skills")), selected,
                int(self.app.config.data.get("max_agent_steps", 8)), prompt,
                allowed_tools, event,
                lambda tool, args, result, success: self.app.storage.log_tool_run(conversation_id, tool, args, result, success),
                cancel_event,
            )
            check()
            from server import _detect_choice_groups, extract_attachments

            plan_status = ""
            if mode == "plan" and plan_id:
                response, plan = self.app.plans.process_response(plan_id, response)
                plan_status = str((plan or {}).get("status") or "")
            choice_groups = _detect_choice_groups(response)
            metadata = {
                "skills": skills,
                "tool_runs": runs,
                "reasoning": reasonings,
                "usage": usage,
                "attachments": extract_attachments(runs),
                "choices": choice_groups[0]["choices"] if choice_groups else [],
                "choice_groups": choice_groups,
                "background_task_id": task_id,
                "agent_id": agent["id"],
                "agent_name": agent["name"],
                "interaction_mode": mode,
                "plan_id": plan_id,
                "plan_status": plan_status,
            }
            saved = self.app.storage.add_message(conversation_id, "assistant", response, metadata)
            self.app.storage.update_background_task(task_id, status="completed", detail={"message": "任务已完成", "message_id": saved["id"]}, finished=True)
        except TaskCancelled:
            self.app.storage.update_background_task(task_id, status="cancelled", detail={"message": "任务已取消"}, finished=True)
        except Exception as exc:
            traceback.print_exc()
            self.app.storage.update_background_task(task_id, status="failed", detail={"message": "任务执行失败"}, error=str(exc), finished=True)
        finally:
            with self._lock:
                self._events.pop(task_id, None)
