from __future__ import annotations

import threading
import time
import traceback
from typing import Any

from plan_runtime import ASK_MODE_PROMPT, CraftToolExecutor, ReadOnlyToolExecutor, resolve_mode_tools
from skill_runtime import SkillAgent, TaskCancelled


class ActiveRunError(RuntimeError):
    def __init__(self, run_id: str):
        super().__init__("当前对话已有运行中的任务")
        self.run_id = run_id


class _RunEventSink:
    """Persist model events while coalescing high-frequency text deltas."""

    def __init__(self, manager: "ConversationRunManager", run_id: str, cancel_event: threading.Event):
        self.manager = manager
        self.run_id = run_id
        self.cancel_event = cancel_event
        self._delta = ""
        self._last_flush = time.monotonic()

    def __call__(self, payload: dict[str, Any]) -> None:
        if self.cancel_event.is_set():
            raise TaskCancelled("任务已取消")
        if str(payload.get("type") or "") == "delta":
            self._delta += str(payload.get("content") or "")
            now = time.monotonic()
            if len(self._delta) >= 4096 or now - self._last_flush >= 0.1:
                self.flush()
            return
        self.flush()
        self.manager.emit(self.run_id, payload)

    def flush(self) -> None:
        if not self._delta:
            return
        content = self._delta
        self._delta = ""
        self._last_flush = time.monotonic()
        self.manager.emit(self.run_id, {"type": "delta", "content": content})


class ConversationRunManager:
    ACTIVE = {"queued", "running", "waiting", "cancelling"}
    TERMINAL = {"completed", "failed", "cancelled"}

    def __init__(self, app: Any):
        self.app = app
        self._lock = threading.RLock()
        self._submit_lock = threading.RLock()
        self._events: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._conditions: dict[str, threading.Condition] = {}

    def _condition(self, run_id: str) -> threading.Condition:
        with self._lock:
            return self._conditions.setdefault(run_id, threading.Condition(self._lock))

    @staticmethod
    def _active_error(exc: RuntimeError) -> ActiveRunError | None:
        text = str(exc)
        if text.startswith("ACTIVE_RUN:"):
            return ActiveRunError(text.split(":", 1)[1])
        return None

    def submit(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.submit_chat(body)

    def submit_chat(self, body: dict[str, Any]) -> dict[str, Any]:
        conversation_id = str(body.get("conversation_id") or "")
        message = str(body.get("message") or "").strip()
        if not conversation_id or not message:
            raise ValueError("conversation_id 和 message 不能为空")
        attachments = body.get("attachments") or []
        if not isinstance(attachments, list):
            raise ValueError("attachments 必须是数组")

        with self._submit_lock:
            conversation = self.app.storage.get_conversation(conversation_id)
            if not conversation:
                raise LookupError("对话不存在")
            active = self.app.storage.active_run(conversation_id)
            if active:
                raise ActiveRunError(str(active["id"]))
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
            mode = str(conversation.get("interaction_mode") or "craft")
            if mode not in ("craft", "plan", "ask"):
                mode = "craft"
            plan_id = ""
            if mode == "plan":
                plan_id = str(self.app.plans.ensure_active_plan(conversation_id, message)["id"])
            snapshot = {
                "agent": agent,
                "conversation_system_prompt": str(conversation.get("system_prompt") or ""),
                "provider_id": str(conversation.get("provider_id") or ""),
                "stream_enabled": bool(conversation.get("stream_enabled", 1)),
                "model_key": str(body.get("model_key") or ""),
                "generation_options": self.app.config.generation_options(),
                "auto_skills": bool(body.get("auto_skills", False)),
                "skill_ids": [str(item) for item in body.get("skill_ids", [])],
                "attachments": attachments,
                "interaction_mode": mode,
                "plan_id": plan_id,
                "allowed_tools": resolve_mode_tools(
                    mode, [str(item) for item in self.app.config.data.get("agent_tools", [])]
                ),
            }
            try:
                run, _ = self.app.storage.create_chat_run(
                    conversation_id,
                    message,
                    attachments,
                    agent,
                    snapshot,
                    mode,
                    plan_id,
                )
            except RuntimeError as exc:
                active_error = self._active_error(exc)
                if active_error:
                    raise active_error from exc
                raise
            self._start(run, self._run_chat)
            return run

    def submit_plan(self, plan_id: str) -> dict[str, Any]:
        with self._submit_lock:
            plan = self.app.plans.validate_execution(plan_id)
            conversation_id = str(plan.get("conversation_id") or "")
            active = self.app.storage.active_run(conversation_id)
            if active:
                raise ActiveRunError(str(active["id"]))
            conversation = self.app.storage.get_conversation(conversation_id)
            if not conversation:
                raise LookupError("发起计划的对话已删除")
            agent = self.app.config.get_agent(str(conversation.get("agent_id") or "")) or {}
            agent = {
                "id": str(agent.get("id") or ""),
                "name": str(agent.get("name") or "Agent"),
                "system_prompt": str(agent.get("system_prompt") or ""),
                "skill_ids": [str(item) for item in agent.get("skill_ids", [])],
            }
            snapshot = {
                "plan_id": plan_id,
                "interaction_mode": "plan",
                "agent": agent,
                "conversation_system_prompt": str(conversation.get("system_prompt") or ""),
                "conversation_messages": conversation.get("messages") or [],
                "provider_id": str(conversation.get("provider_id") or ""),
                "stream_enabled": bool(conversation.get("stream_enabled", 1)),
                "generation_options": self.app.config.generation_options(),
                "allowed_tools": resolve_mode_tools(
                    "craft", [str(item) for item in self.app.config.data.get("agent_tools", [])]
                ),
            }
            try:
                run = self.app.storage.create_run(
                    conversation_id,
                    f"执行计划：{plan.get('title') or '实施计划'}",
                    agent,
                    snapshot,
                    kind="plan_execute",
                    interaction_mode="plan",
                    plan_id=plan_id,
                )
            except RuntimeError as exc:
                active_error = self._active_error(exc)
                if active_error:
                    raise active_error from exc
                raise
            try:
                self.app.plans.prepare_execution(plan_id, run_id=str(run["id"]))
            except Exception:
                self.app.storage.update_background_task(
                    str(run["id"]), status="failed", error="计划启动失败", finished=True
                )
                raise
            self._start(run, self._run_plan)
            return self.app.storage.get_background_task(str(run["id"])) or run

    def _start(self, run: dict[str, Any], target: Any) -> None:
        run_id = str(run["id"])
        cancel_event = threading.Event()
        with self._lock:
            self._events[run_id] = cancel_event
            self._condition(run_id)
        self.emit(run_id, {"type": "run_started", "run_id": run_id})
        thread = threading.Thread(
            target=target,
            args=(run_id, cancel_event),
            name=f"naiba-run-{run_id[:8]}",
            daemon=True,
        )
        with self._lock:
            self._threads[run_id] = thread
        thread.start()

    def emit(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = self.app.storage.append_run_event(run_id, payload)
        kind = str(payload.get("type") or "")
        detail: dict[str, Any] | None = None
        status: str | None = None
        if kind == "skills":
            detail = {"message": "已启用 Skill", "skills": payload.get("skills") or []}
        elif kind == "status":
            detail = {"message": str(payload.get("message") or "正在执行")}
            status = "running"
        elif kind == "tool_start":
            detail = {
                "message": f"正在执行 {payload.get('tool') or '工具'}",
                "tool": str(payload.get("tool") or ""),
            }
            status = "running"
        elif kind == "tool_confirm":
            detail = {
                "message": "等待工具确认",
                "tool": str(payload.get("tool_name") or ""),
                "tool_desc": str(payload.get("tool_desc") or ""),
                "arguments": payload.get("arguments") or {},
                "confirm_id": str(payload.get("confirm_id") or ""),
            }
            status = "waiting"
        elif kind == "tool_result":
            detail = {"message": f"工具 {payload.get('tool') or ''} 执行完毕"}
            status = "running"
        current = self.app.storage.get_background_task(run_id)
        if current and current.get("status") == "cancelling":
            status = None
        if detail is not None or status is not None:
            self.app.storage.update_background_task(run_id, status=status, detail=detail)
        condition = self._condition(run_id)
        with condition:
            condition.notify_all()
        return event

    def _run_chat(self, run_id: str, cancel_event: threading.Event) -> None:
        run = self.app.storage.get_background_task(run_id)
        snapshot = self.app.storage.get_run_snapshot(run_id) or {}
        if not run:
            return
        conversation_id = str(run["conversation_id"])
        mode = str(snapshot.get("interaction_mode") or run.get("interaction_mode") or "craft")
        plan_id = str(snapshot.get("plan_id") or run.get("plan_id") or "")
        sink = _RunEventSink(self, run_id, cancel_event)
        skills: list[dict[str, str]] = []

        def event(payload: dict[str, Any]) -> None:
            nonlocal skills
            if payload.get("type") == "skills" and isinstance(payload.get("skills"), list):
                skills = [
                    {"id": str(item.get("id") or ""), "name": str(item.get("name") or "")}
                    for item in payload["skills"]
                    if isinstance(item, dict)
                ]
            sink(payload)

        try:
            if cancel_event.is_set():
                raise TaskCancelled("任务已取消")
            self.app.storage.update_background_task(
                run_id, status="running", started=True, detail={"message": "任务开始执行"}
            )
            event({"type": "status", "message": "任务开始执行"})
            if cancel_event.is_set():
                raise TaskCancelled("任务已取消")
            from server import _detect_choice_groups, build_model_history, extract_attachments

            history = build_model_history(snapshot.get("conversation_messages") or [])
            message = str(run.get("message") or "")
            uploads = snapshot.get("attachments") or []
            extra = [f"[用户上传文件：{item.get('path')}]" for item in uploads if item.get("path")]
            effective = message + (("\n" + "\n".join(extra)) if extra else "")
            model_key = str(snapshot.get("model_key") or "")
            if not model_key and snapshot.get("provider_id"):
                model_key = f"online:{snapshot['provider_id']}"
            profile = self.app.config.profile(model_key)
            options = dict(snapshot.get("generation_options") or self.app.config.generation_options())
            options["stream"] = bool(snapshot.get("stream_enabled", True))
            agent = snapshot.get("agent") or {}
            selected = list(dict.fromkeys(agent.get("skill_ids", []) + snapshot.get("skill_ids", [])))
            prompt = "\n\n".join(
                item for item in (
                    str(agent.get("system_prompt") or "").strip(),
                    str(snapshot.get("conversation_system_prompt") or "").strip(),
                ) if item
            )
            if mode == "ask":
                prompt = (prompt + "\n\n" + ASK_MODE_PROMPT).strip()
            elif mode == "plan":
                prompt = (prompt + "\n\n" + self.app.plans.prepare_prompt(self.app.plans.get(plan_id))).strip()
            executor = CraftToolExecutor(self.app.executor) if mode == "craft" else ReadOnlyToolExecutor(self.app.executor)
            worker = SkillAgent(self.app.catalog, executor, self.app.models.complete)
            response, runs, reasonings, usage = worker.run(
                effective,
                history,
                profile,
                options,
                bool(snapshot.get("auto_skills")),
                selected,
                prompt,
                [str(item) for item in snapshot.get("allowed_tools") or []],
                event,
                lambda tool, args, result, success: self.app.storage.log_tool_run(
                    conversation_id, tool, args, result, success
                ),
                cancel_event,
                max_steps=int(self.app.config.data.get("agent_max_steps", 32)),
                tool_registry=self.app.tool_registry,
            )
            sink.flush()
            if cancel_event.is_set():
                raise TaskCancelled("任务已取消")
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
                "run_id": run_id,
                "background_task_id": run_id,
                "agent_id": str(agent.get("id") or ""),
                "agent_name": str(agent.get("name") or "Agent"),
                "interaction_mode": mode,
                "plan_id": plan_id,
                "plan_status": plan_status,
            }
            saved = self.app.storage.add_message(conversation_id, "assistant", response, metadata)
            self.app.storage.update_background_task(
                run_id,
                status="completed",
                detail={"message": "任务已完成", "message_id": saved["id"]},
                finished=True,
            )
            if choice_groups:
                self.emit(
                    run_id,
                    {"type": "choice", "choices": choice_groups[0]["choices"], "choice_groups": choice_groups},
                )
            self.emit(run_id, {"type": "done", "message": saved})
        except TaskCancelled:
            sink.flush()
            if mode == "plan" and plan_id:
                try:
                    self.app.plans.cancel(plan_id)
                except (LookupError, ValueError):
                    pass
            self.app.storage.update_background_task(
                run_id, status="cancelled", detail={"message": "任务已取消"}, finished=True
            )
            self.emit(run_id, {"type": "cancelled", "message": "任务已取消"})
        except Exception as exc:
            sink.flush()
            traceback.print_exc()
            error_message = str(exc)
            try:
                self.app.storage.add_message(
                    conversation_id,
                    "error",
                    f"请求失败：{error_message}",
                    {"error": True, "skills": skills, "run_id": run_id},
                )
            except Exception:
                traceback.print_exc()
            self.app.storage.update_background_task(
                run_id,
                status="failed",
                detail={"message": "任务执行失败"},
                error=error_message,
                finished=True,
            )
            self.emit(run_id, {"type": "error", "message": error_message})
        finally:
            self._finish(run_id)

    def _run_plan(self, run_id: str, cancel_event: threading.Event) -> None:
        run = self.app.storage.get_background_task(run_id)
        if not run:
            return
        plan_id = str(run.get("plan_id") or "")
        sink = _RunEventSink(self, run_id, cancel_event)
        try:
            if cancel_event.is_set():
                raise TaskCancelled("计划已取消")
            self.app.storage.update_background_task(
                run_id, status="running", started=True, detail={"message": "计划开始执行"}
            )
            self.emit(run_id, {"type": "status", "message": "计划开始执行"})
            snapshot = self.app.storage.get_run_snapshot(run_id) or {}
            plan = self.app.plans.run_execution(plan_id, cancel_event, sink, snapshot)
            sink.flush()
            status = str((plan or {}).get("status") or "failed")
            if status == "finished":
                self.app.storage.update_background_task(
                    run_id, status="completed", detail={"message": "计划已完成"}, finished=True
                )
                self.emit(run_id, {"type": "done", "plan": plan})
            elif status == "cancelled":
                self.app.storage.update_background_task(
                    run_id, status="cancelled", detail={"message": "计划已取消"}, finished=True
                )
                self.emit(run_id, {"type": "cancelled", "message": "计划已取消"})
            else:
                error = str((plan or {}).get("error") or "计划执行失败")
                self.app.storage.update_background_task(
                    run_id, status="failed", detail={"message": "计划执行失败"}, error=error, finished=True
                )
                self.emit(run_id, {"type": "error", "message": error})
        except TaskCancelled:
            sink.flush()
            self.app.storage.update_background_task(
                run_id, status="cancelled", detail={"message": "计划已取消"}, finished=True
            )
            self.emit(run_id, {"type": "cancelled", "message": "计划已取消"})
        except Exception as exc:
            sink.flush()
            traceback.print_exc()
            self.app.storage.update_background_task(
                run_id, status="failed", detail={"message": "计划执行失败"}, error=str(exc), finished=True
            )
            self.emit(run_id, {"type": "error", "message": str(exc)})
        finally:
            self._finish(run_id)

    def _finish(self, run_id: str) -> None:
        condition = self._condition(run_id)
        with condition:
            condition.notify_all()
        with self._lock:
            self._events.pop(run_id, None)
            self._threads.pop(run_id, None)
            self._conditions.pop(run_id, None)

    def list(self, conversation_id: str = "", active_only: bool = False) -> list[dict[str, Any]]:
        return self.app.storage.list_background_tasks(conversation_id, active_only)

    def get(self, run_id: str) -> dict[str, Any] | None:
        return self.app.storage.get_background_task(run_id)

    def events_after(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self.app.storage.list_run_events(run_id, after)

    def wait_for_events(self, run_id: str, after: int, timeout: float = 15.0) -> list[dict[str, Any]]:
        events = self.events_after(run_id, after)
        if events:
            return events
        run = self.get(run_id)
        if not run or run.get("status") in self.TERMINAL:
            return []
        condition = self._condition(run_id)
        with condition:
            condition.wait(timeout=max(0.1, timeout))
        return self.events_after(run_id, after)

    def cancel(self, run_id: str) -> dict[str, Any] | None:
        run = self.get(run_id)
        if not run:
            return None
        if run.get("status") in self.ACTIVE:
            with self._lock:
                event = self._events.get(run_id)
            if event:
                event.set()
            return self.app.storage.update_background_task(
                run_id,
                status="cancelling",
                cancel_requested=True,
                detail={"message": "正在取消任务"},
            )
        return run

    def cancel_plan(self, plan_id: str) -> dict[str, Any] | None:
        run = next(
            (item for item in self.list(active_only=True) if str(item.get("plan_id") or "") == plan_id),
            None,
        )
        return self.cancel(str(run["id"])) if run else None

    def owns_confirmation(self, run_id: str, confirm_id: str) -> bool:
        run = self.get(run_id)
        return bool(
            run
            and run.get("status") == "waiting"
            and str((run.get("detail") or {}).get("confirm_id") or "") == confirm_id
        )

    def shutdown(self, timeout: float = 10.0) -> None:
        with self._lock:
            events = list(self._events.values())
            threads = list(self._threads.values())
        for event in events:
            event.set()
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)


# Compatibility for imports used by earlier builds and tests.
BackgroundTaskManager = ConversationRunManager
