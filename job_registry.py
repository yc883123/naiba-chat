"""Harness 级统一 Job Registry 与持续检查/唤醒。

继续使用现有 ``background_tasks`` / ``run_events`` 表（字段已通过增量迁移补齐），
不新建平行任务表。提供计划规定的统一接口：

    start(spec, owner) -> job_id
    get(job_id, owner=None) -> snapshot
    list(owner=None, active_only=False) -> snapshots
    read(job_id, cursor=0, owner=None) -> output
    wait(job_id, timeout, owner=None) -> snapshot
    cancel(job_id, owner=None, reason=None) -> snapshot

状态固定为：queued / running / waiting / stopping / completed / failed /
cancelled / interrupted。

Job Worker 支持：提交一次任务、按固定间隔或指数退避检查、把检查结果写入事件、
识别完成/失败/超时、完成后通过 Condition 唤醒等待中的 Agent、输出增量进度。

ComfyUI 生成任务使用专用 Worker（见 ``_run_comfyui``）。
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import net_io

JOB_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
JOB_ACTIVE = {"queued", "running", "waiting", "stopping"}

# 副作用/不可重试的检查型错误归类（MCP、HTTP、Job 查询）用于 Agent Loop 重试决策
RETRYABLE_ERROR_PREFIXES = (
    "MCP",
    "HTTP 5",
    "HTTP 408",
    "HTTP 429",
    "urllib",
    "Timeout",
    "连接",
    "超时",
)


@dataclass
class JobSpec:
    kind: str
    conversation_id: str
    params: dict[str, Any] = field(default_factory=dict)
    label: str = ""
    parent_job_id: str | None = None
    owner_session_id: str = ""
    resumable: bool = False
    checkpoint: dict[str, Any] | None = None


@dataclass
class CheckSpec:
    interval_seconds: float = 5.0
    timeout_seconds: float = 600.0
    max_attempts: int = 60
    backoff_seconds: float = 0.0
    check_kind: str = "http_poll"


class JobRegistry:
    def __init__(self, app: Any):
        self.app = app
        self._lock = threading.RLock()
        self._conditions: dict[str, threading.Condition] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        # 由 app 接线注入：运行子 Agent 的函数 (job_id, spec, cancel, sink) -> None
        self.agent_runner: Callable[[str, JobSpec, threading.Event, Callable[[dict], None]], None] | None = None
        self._workers: dict[str, Callable[[str, JobSpec, threading.Event], None]] = {
            "shell": self._run_shell,
            "check": self._run_check,
            "http_poll": self._run_check,
            "comfyui": self._run_comfyui,
            "subagent": self._run_subagent,
        }

    # ---- 内部工具 ----
    def _condition(self, job_id: str) -> threading.Condition:
        with self._lock:
            return self._conditions.setdefault(job_id, threading.Condition(self._lock))

    def _now(self) -> int:
        return int(time.time() * 1000)

    def _emit(self, job_id: str, payload: dict[str, Any]) -> None:
        try:
            self.app.storage.append_run_event(job_id, payload)
        except Exception:
            traceback.print_exc()
        condition = self._condition(job_id)
        with condition:
            condition.notify_all()

    def _snapshot(self, job: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": job.get("id"),
            "kind": job.get("kind"),
            "conversation_id": job.get("conversation_id"),
            "parent_job_id": job.get("parent_job_id") or "",
            "owner_session_id": job.get("owner_session_id") or "",
            "status": job.get("status"),
            "progress": float(job.get("progress") or 0),
            "current_step": str(job.get("current_step") or ""),
            "attempt": int(job.get("attempt") or 0),
            "checkpoint": job.get("checkpoint") if isinstance(job.get("checkpoint"), dict) else {},
            "result": job.get("result") if isinstance(job.get("result"), dict) else {},
            "error": job.get("error") or "",
            "created_at": job.get("created_at"),
            "started_at": job.get("started_at"),
            "updated_at": job.get("updated_at"),
            "finished_at": job.get("finished_at"),
        }

    def _set_status(self, job_id: str, status: str, **fields: Any) -> None:
        current = self.app.storage.get_background_task(job_id)
        if current and current.get("cancel_requested") and status in JOB_ACTIVE:
            return
        self.app.storage.update_job(job_id, status=status, **fields)
        self._emit(job_id, {"type": "job_status", "status": status, **fields})

    def _finish(self, job_id: str, status: str, error: str = "", result: dict[str, Any] | None = None, detail: dict[str, Any] | None = None) -> None:
        current = self.app.storage.get_background_task(job_id)
        if current and current.get("cancel_requested") and status != "cancelled":
            status = "cancelled"
            error = error or "用户取消"
        self.app.storage.update_job(
            job_id, status=status, error=error, result=result or {}, detail=detail, finished=True
        )
        self._emit(job_id, {"type": "job_finished", "status": status, "error": error, "result": result or {}})
        condition = self._condition(job_id)
        with condition:
            condition.notify_all()

    # ---- 统一接口 ----
    def start(self, spec: JobSpec, owner: str | None = None) -> str:
        owner = owner or spec.owner_session_id or spec.conversation_id
        snapshot = {
            "job_spec": {"kind": spec.kind, "resumable": spec.resumable},
            "params": spec.params,
            "label": spec.label,
        }
        run = self.app.storage.create_run(
            spec.conversation_id,
            spec.label or f"Job({spec.kind})",
            {"id": "", "name": "Job", "system_prompt": "", "skill_ids": []},
            snapshot,
            kind=spec.kind,
            parent_job_id=spec.parent_job_id or "",
            owner_session_id=owner,
        )
        job_id = str(run["id"])
        self.app.storage.update_job(
            job_id,
            status="queued",
            progress=0,
            attempt=0,
            current_step="已入队",
            checkpoint=spec.checkpoint or {},
        )
        self._emit(job_id, {"type": "job_status", "status": "queued", "kind": spec.kind})
        cancel = threading.Event()
        with self._lock:
            self._cancel[job_id] = cancel
            self._condition(job_id)
        worker = self._workers.get(spec.kind, self._run_unknown)
        thread = threading.Thread(
            target=worker, args=(job_id, spec, cancel), name=f"job-{spec.kind}-{job_id[:8]}", daemon=True
        )
        with self._lock:
            self._threads[job_id] = thread
        thread.start()
        return job_id

    def get(self, job_id: str, owner: str | None = None) -> dict[str, Any] | None:
        job = self.app.storage.get_background_task(job_id)
        if not job:
            return None
        if owner and job.get("owner_session_id") != owner:
            return None
        return self._snapshot(job)

    def list(self, owner: str | None = None, active_only: bool = False) -> list[dict[str, Any]]:
        jobs = self.app.storage.list_background_tasks("", active_only)
        if owner:
            jobs = [j for j in jobs if j.get("owner_session_id") == owner]
        return [self._snapshot(j) for j in jobs]

    def read(self, job_id: str, cursor: int = 0, owner: str | None = None) -> dict[str, Any]:
        if not self.get(job_id, owner):
            return {"events": [], "cursor": cursor}
        events = self.app.storage.list_run_events(job_id, cursor)
        new_cursor = events[-1]["sequence"] if events else cursor
        return {"events": events, "cursor": new_cursor}

    def wait(self, job_id: str, timeout: float, owner: str | None = None) -> dict[str, Any] | None:
        job = self.get(job_id, owner)
        if not job:
            raise LookupError("Job 不存在或无权访问")
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            job = self.get(job_id, owner)
            if job and job["status"] in JOB_TERMINAL:
                return job
            condition = self._condition(job_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            with condition:
                condition.wait(timeout=min(remaining, 1.0))
        return self.get(job_id, owner)

    def cancel(self, job_id: str, owner: str | None = None, reason: str | None = None) -> dict[str, Any] | None:
        job = self.get(job_id, owner)
        if not job:
            return None
        if job["status"] in JOB_TERMINAL:
            return job
        event: threading.Event | None = None
        with self._lock:
            event = self._cancel.get(job_id)
        if event:
            event.set()
        snapshot = self._snapshot(
            self.app.storage.update_job(
                job_id, status="stopping", current_step="正在停止", detail={"message": reason or "用户取消"}
            )
            or {}
        )
        # 父任务取消时递归取消所有子任务（级联）
        self._cancel_children(job_id, owner, reason)
        return snapshot

    def _cancel_children(self, parent_id: str, owner: str | None, reason: str | None) -> None:
        children = [j for j in self.list(owner=owner) if str(j.get("parent_job_id") or "") == parent_id]
        for child in children:
            self.cancel(str(child["id"]), owner=owner, reason=reason or "父任务取消")

    def resume(self, job_id: str, owner: str | None = None, extra_checkpoint: dict[str, Any] | None = None) -> str | None:
        """从 checkpoint 恢复一个可恢复 Job（仅声明 resumable 且非副作用未知的任务）。"""
        job = self.get(job_id, owner)
        if not job:
            return None
        if not job.get("checkpoint"):
            return None
        checkpoint = dict(job["checkpoint"])
        if extra_checkpoint:
            checkpoint.update(extra_checkpoint)
        # Job parameters live in the durable run snapshot.  Do not rely on
        # the final result (which intentionally contains only outputs), or a
        # resumed ComfyUI batch would lose its workflow list after restart.
        persisted = self.app.storage.get_run_snapshot(job_id) or {}
        persisted_params = persisted.get("params") if isinstance(persisted, dict) else {}
        if not isinstance(persisted_params, dict):
            persisted_params = {}
        if not persisted_params:
            persisted_params = dict(job.get("result", {}).get("params", {})) if isinstance(job.get("result"), dict) else {}
        persisted_params = dict(persisted_params)
        # 记录来源 Job：resume/retry 产生的新 Job 可追溯“由谁恢复”，跨对话查询也能核对。
        persisted_params["_resumed_from"] = job_id
        label = job.get("current_step") or f"恢复 Job({job['kind']})"
        spec = JobSpec(
            kind=job["kind"],
            conversation_id=job["conversation_id"],
            params=persisted_params,
            label=label,
            parent_job_id=job["parent_job_id"] or None,
            owner_session_id=job["owner_session_id"],
            resumable=True,
            checkpoint=checkpoint,
        )
        new_id = self.start(spec, owner=job["owner_session_id"])
        if new_id:
            # 在新 Job 的增量输出里写入一条来源说明，UI 与 job_output 均可见。
            try:
                self.app.storage.append_run_event(
                    new_id,
                    {"type": "output", "line": f"（本 Job 由 Job {job_id} 恢复/重试）"},
                )
            except Exception:
                traceback.print_exc()
        return new_id

    def retry(self, job_id: str, owner: str | None = None) -> str | None:
        """用户确认后重试失败步骤（等价于从 checkpoint 恢复，但显式标记 retry_step）。

        仅允许在已结束的非成功状态下调用；要求 Job 带有 checkpoint 且可恢复。
        """
        job = self.get(job_id, owner)
        if not job:
            return None
        if job["status"] in ("running", "waiting", "stopping", "queued"):
            raise ValueError("Job 仍在运行中，无法重试")
        if job["status"] == "completed":
            raise ValueError("Job 已成功完成，无需重试")
        if not job.get("checkpoint"):
            raise ValueError("Job 没有可用 checkpoint，无法重试")
        checkpoint = dict(job["checkpoint"])
        checkpoint["retry_step"] = True
        try:
            return self.resume(job_id, owner=owner, extra_checkpoint=checkpoint)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"重试失败：{exc}") from exc

    def resume_interrupted(self) -> list[str]:
        """Resume durable, explicitly resumable jobs interrupted by restart."""
        resumed: list[str] = []
        for job in self.app.storage.list_background_tasks("", active_only=False, limit=200):
            if str(job.get("status") or "") != "interrupted":
                continue
            snapshot = self.app.storage.get_run_snapshot(str(job.get("id") or "")) or {}
            job_spec = snapshot.get("job_spec") if isinstance(snapshot, dict) else {}
            if not isinstance(job_spec, dict) or not bool(job_spec.get("resumable")):
                continue
            # Only deterministic workers can safely resume automatically.
            if str(job.get("kind") or "") not in {"comfyui", "check", "http_poll"}:
                continue
            try:
                new_id = self.resume(str(job["id"]), owner=str(job.get("owner_session_id") or "") or None)
                if new_id:
                    resumed.append(new_id)
            except Exception:
                traceback.print_exc()
        return resumed

    # ---- 通用 Worker 框架 ----
    def _run_unknown(self, job_id: str, spec: JobSpec, cancel: threading.Event) -> None:
        self._set_status(job_id, "running", current_step=f"未知 Job 类型：{spec.kind}")
        self._finish(job_id, "failed", error=f"不支持的 Job 类型：{spec.kind}")

    def _run_subagent(self, job_id: str, spec: JobSpec, cancel: threading.Event) -> None:
        if not self.agent_runner:
            self._set_status(job_id, "running", current_step="子 Agent 运行器未配置")
            self._finish(job_id, "failed", error="子 Agent 运行器未配置")
            return
        self._set_status(job_id, "running", current_step="子 Agent 已启动")
        try:
            self.agent_runner(job_id, spec, cancel, lambda p: self._emit(job_id, p))
            job = self.get(job_id)
            if job and job["status"] not in JOB_TERMINAL:
                self._finish(job_id, "completed", result={"subagent_job_id": job_id})
        except Exception as exc:
            traceback.print_exc()
            self._finish(job_id, "failed", error=str(exc))

    def _run_shell(self, job_id: str, spec: JobSpec, cancel: threading.Event) -> None:
        params = spec.params or {}
        command = str(params.get("command") or "").strip()
        cwd = str(params.get("cwd") or "").strip() or str(self.app.config.data.get("workspace_dir") or ".")
        timeout = min(max(int(params.get("timeout", 120)), 1), 900)
        if not command:
            self._set_status(job_id, "running", current_step="缺少 command 参数")
            self._finish(job_id, "failed", error="shell Job 缺少 command 参数")
            return
        self._set_status(job_id, "running", current_step="执行命令")
        proc = None
        try:
            proc = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output: list[str] = []
            start = time.monotonic()
            while True:
                if cancel.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                    self._finish(job_id, "cancelled", error="用户取消", result={"exit_code": -1, "output": "\n".join(output)})
                    return
                line = proc.stdout.readline() if proc.stdout else ""
                if line == "" and proc.poll() is not None:
                    break
                if line:
                    output.append(line.rstrip("\n"))
                    self._emit(job_id, {"type": "job_log", "line": line.rstrip("\n")})
                elapsed = time.monotonic() - start
                self.app.storage.update_job(job_id, progress=min(99.0, elapsed / timeout * 100))
                if elapsed >= timeout:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        proc.kill()
                    self._finish(job_id, "failed", error="shell Job 超时", result={"exit_code": -1, "output": "\n".join(output)})
                    return
            rc = proc.wait()
            text = "\n".join(output)
            self._set_status(job_id, "running", progress=100, current_step="命令结束")
            self._finish(
                job_id,
                "completed" if rc == 0 else "failed",
                error="" if rc == 0 else f"exit_code={rc}",
                result={"exit_code": rc, "output": text[:50000]},
            )
        except Exception as exc:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._finish(job_id, "failed", error=f"{type(exc).__name__}: {exc}")

    def _run_check(self, job_id: str, spec: JobSpec, cancel: threading.Event) -> None:
        """通用 HTTP 提交 + 轮询检查 Worker（持续检查/唤醒）。

        params:
          submit: {"type":"http","url":...,"method":...,"json":...,"headers":...}
                  或 {"type":"mcp","server":...,"tool":...,"arguments":...}
          poll:   {"url":..., "handle_path":"data.id", "status_path":"data.status",
                   "done":["completed"], "fail":["failed"]}
          check:  {interval_seconds, timeout_seconds, max_attempts, backoff_seconds, check_kind}
        """
        params = spec.params or {}
        check = CheckSpec(**{k: v for k, v in (params.get("check") or {}).items() if k in CheckSpec.__dataclass_fields__})
        submit = params.get("submit") or {}
        poll = params.get("poll") or {}
        attempt = 0
        self._set_status(job_id, "running", current_step="提交任务", attempt=attempt)
        handle = self._submit(check, submit)
        if handle is None:
            self._finish(job_id, "failed", error="Job 提交失败")
            return
        self.app.storage.update_job(job_id, checkpoint={"handle": handle})
        self._emit(job_id, {"type": "job_check", "phase": "submitted", "handle": handle, "attempt": attempt})
        deadline = time.monotonic() + check.timeout_seconds
        backoff = 0.0
        while True:
            if cancel.is_set():
                self._finish(job_id, "cancelled", error="用户取消", result={"handle": handle})
                return
            if time.monotonic() > deadline or attempt >= check.max_attempts:
                self._finish(job_id, "failed", error="Job 轮询超时", result={"handle": handle})
                return
            time.sleep(check.interval_seconds + backoff)
            attempt += 1
            self.app.storage.update_job(job_id, attempt=attempt, current_step=f"第 {attempt} 次检查")
            status = self._poll_status(poll, handle)
            self._emit(job_id, {"type": "job_check", "phase": "polling", "attempt": attempt, "status": status, "next_check_in": check.interval_seconds + backoff})
            done = poll.get("done", ["completed"])
            fail = poll.get("fail", ["failed"])
            if status in done:
                self._set_status(job_id, "running", progress=100, current_step="完成")
                self._finish(job_id, "completed", result={"handle": handle, "status": status})
                return
            if status in fail:
                self._finish(job_id, "failed", error=f"Job 失败：{status}", result={"handle": handle, "status": status})
                return
            if check.backoff_seconds:
                backoff = min(check.backoff_seconds * (2 ** min(attempt, 5)), 60.0)

    def _submit(self, check: CheckSpec, submit: dict[str, Any]) -> Any | None:
        try:
            if submit.get("type") == "mcp":
                ok, out = self.app.executor.mcp_registry.call(
                    str(submit.get("server") or ""), str(submit.get("tool") or ""), submit.get("arguments") or {}
                )
                if not ok:
                    return None
                return out
            url = str(submit.get("url") or "")
            method = str(submit.get("method") or "POST").upper()
            data = submit.get("json")
            headers = dict(submit.get("headers") or {})
            encoded = None
            if data is not None:
                encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            req = urllib.request.Request(url, data=encoded, headers=headers, method=method)
            with net_io.open(req, timeout=min(int(submit.get("timeout", 60)), 180)) as resp:
                body = resp.read(100000).decode("utf-8", errors="replace")
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return body
        except Exception as exc:
            traceback.print_exc()
            return None

    def _poll_status(self, poll: dict[str, Any], handle: Any) -> str:
        url = str(poll.get("url") or "").replace("{handle}", str(handle))
        if not url:
            return "unknown"
        try:
            with net_io.open(url, timeout=min(int(poll.get("timeout", 30)), 120)) as resp:
                body = resp.read(100000).decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return "unknown"
            status_path = poll.get("status_path", "status")
            node = data
            for key in status_path.split("."):
                if isinstance(node, dict):
                    node = node.get(key)
                else:
                    return "unknown"
            return str(node or "unknown")
        except Exception:
            return "unknown"

    # ---- ComfyUI 专用 Worker（持续检查 + 唤醒 + 可恢复） ----
    def _run_comfyui(self, job_id: str, spec: JobSpec, cancel: threading.Event) -> None:
        params = spec.params or {}
        comfy_url = str(
            params.get("comfyui_url")
            or self.app.config.data.get("comfyui_url")
            or "http://127.0.0.1:8188"
        ).rstrip("/")
        if not comfy_url:
            self._set_status(job_id, "running", current_step="未配置 ComfyUI URL")
            self._finish(job_id, "failed", error="未配置 ComfyUI URL")
            return
        workflow = params.get("workflow") or {}
        shots = int(params.get("shots") or 1)
        checkpoint = dict(spec.checkpoint or {})
        completed_shots = checkpoint.get("completed_shots", [])
        prompt_ids = checkpoint.get("prompt_ids", [])

        # 1. 验证 ComfyUI 可达
        if not self._comfyui_reachable(comfy_url):
            self._finish(job_id, "failed", error="ComfyUI 服务不可达", result={"comfyui_url": comfy_url})
            return

        # High-level batch calls use a list of already prepared API workflows.
        # Keep the legacy single-workflow path below for compatibility.
        workflows = params.get("workflows")
        if isinstance(workflows, list) and workflows:
            self._run_comfyui_batch(job_id, spec, cancel, comfy_url, workflows)
            return

        self._set_status(job_id, "running", current_step=f"开始生成 {shots} 段", progress=0,
                         checkpoint={"completed_shots": completed_shots, "prompt_ids": prompt_ids})
        errors: list[str] = []
        for index in range(len(completed_shots), shots):
            if cancel.is_set():
                self._finish(job_id, "cancelled", error="用户取消",
                             result={"completed_shots": completed_shots, "prompt_ids": prompt_ids, "errors": errors})
                return
            self.app.storage.update_job(job_id, current_step=f"提交第 {index + 1}/{shots} 段",
                                        progress=round(index / shots * 100, 1))
            self._emit(job_id, {"type": "job_check", "phase": "submit_shot", "shot": index + 1, "total": shots})
            prompt_id = self._comfyui_submit(comfy_url, workflow, index)
            if not prompt_id:
                reason = f"第 {index + 1} 段提交失败（工作流或节点错误）"
                errors.append(reason)
                self._emit(job_id, {"type": "job_check", "phase": "shot_failed", "shot": index + 1, "reason": reason})
                # 单镜头失败记录原因，不自动跳过；依赖 checkpoint 供后续恢复
                self._finish(job_id, "failed", error=reason,
                             result={"completed_shots": completed_shots, "prompt_ids": prompt_ids, "errors": errors})
                return
            prompt_ids.append(prompt_id)
            self.app.storage.update_job(job_id, checkpoint={"completed_shots": completed_shots, "prompt_ids": prompt_ids})
            # 2. 轮询历史
            ok, files, reason = self._comfyui_wait_history(comfy_url, prompt_id, cancel, job_id, index, shots)
            if cancel.is_set():
                self._finish(job_id, "cancelled", error="用户取消",
                             result={"completed_shots": completed_shots, "prompt_ids": prompt_ids, "errors": errors})
                return
            if not ok:
                errors.append(reason)
                self._finish(job_id, "failed", error=reason,
                             result={"completed_shots": completed_shots, "prompt_ids": prompt_ids, "errors": errors})
                return
            completed_shots.append({"index": index, "prompt_id": prompt_id, "files": files})
            self.app.storage.update_job(
                job_id,
                progress=round((index + 1) / shots * 100, 1),
                current_step=f"第 {index + 1}/{shots} 段完成",
                checkpoint={"completed_shots": completed_shots, "prompt_ids": prompt_ids},
            )
            self._emit(job_id, {"type": "job_check", "phase": "shot_done", "shot": index + 1, "files": files})
        self._finish(
            job_id, "completed",
            result={"completed_shots": completed_shots, "prompt_ids": prompt_ids, "errors": errors},
        )

    def _run_comfyui_batch(
        self,
        job_id: str,
        spec: JobSpec,
        cancel: threading.Event,
        comfy_url: str,
        workflows: list[dict[str, Any]],
    ) -> None:
        """Fire all prompts first, then poll them as one durable batch."""
        checkpoint = dict(spec.checkpoint or {})
        submitted = list(checkpoint.get("submitted") or [])
        completed = list(checkpoint.get("completed") or [])
        errors: list[dict[str, Any]] = list(checkpoint.get("errors") or [])
        total = len(workflows)
        self._set_status(job_id, "running", current_step=f"提交 {total} 个工作流", progress=0,
                         checkpoint={"submitted": submitted, "completed": completed, "errors": errors})

        # Submit only work not present in the checkpoint, making resume safe.
        for index in range(len(submitted), total):
            if cancel.is_set():
                self._finish(job_id, "cancelled", error="用户取消",
                             result={"prompt_ids": submitted, "completed": completed, "errors": errors})
                return
            prompt_id = self._comfyui_submit(comfy_url, workflows[index], index)
            if not prompt_id:
                errors.append({"index": index, "error": "提交失败"})
                self._finish(job_id, "failed", error=f"第 {index + 1} 段提交失败",
                             result={"prompt_ids": submitted, "completed": completed, "errors": errors})
                return
            submitted.append(prompt_id)
            self.app.storage.update_job(
                job_id,
                progress=round((index + 1) / max(total, 1) * 30, 1),
                current_step=f"已提交 {index + 1}/{total}",
                checkpoint={"submitted": submitted, "completed": completed, "errors": errors},
            )
            self._emit(job_id, {"type": "job_check", "phase": "batch_submitted", "index": index, "prompt_id": prompt_id, "total": total})

        # Poll each prompt independently; completed entries are checkpointed so
        # interruption does not resubmit or lose already finished segments.
        done_indexes = {int(item.get("index")) for item in completed if isinstance(item, dict) and str(item.get("index", "")).isdigit()}
        deadline = time.monotonic() + float(spec.params.get("wait_timeout", 7200) or 7200)
        while len(done_indexes) < total:
            if cancel.is_set():
                self._finish(job_id, "cancelled", error="用户取消",
                             result={"prompt_ids": submitted, "completed": completed, "errors": errors})
                return
            if time.monotonic() > deadline:
                self._finish(job_id, "failed", error="ComfyUI 批量轮询超时",
                             result={"prompt_ids": submitted, "completed": completed, "errors": errors})
                return
            for index, prompt_id in enumerate(submitted):
                if index in done_indexes:
                    continue
                ok, files, reason = self._comfyui_history_once(comfy_url, prompt_id)
                if ok:
                    completed.append({"index": index, "prompt_id": prompt_id, "files": files})
                    done_indexes.add(index)
                    self._emit(job_id, {"type": "job_check", "phase": "batch_done", "index": index, "files": files, "total": total})
                elif reason and reason.startswith("ERROR:"):
                    errors.append({"index": index, "prompt_id": prompt_id, "error": reason[6:]})
                    done_indexes.add(index)
                    self._emit(job_id, {"type": "job_check", "phase": "batch_failed", "index": index, "error": reason[6:]})
            progress = 30 + (len(done_indexes) / max(total, 1) * 70)
            self.app.storage.update_job(
                job_id, progress=round(progress, 1), current_step=f"完成 {len(done_indexes)}/{total}",
                checkpoint={"submitted": submitted, "completed": completed, "errors": errors},
            )
            if len(done_indexes) < total:
                time.sleep(1.0 if total <= 4 else 3.0)
        status = "completed" if not errors else ("failed" if not completed else "completed")
        self._finish(job_id, status, error="" if not errors else "部分工作流失败",
                     result={"prompt_ids": submitted, "completed": completed, "errors": errors, "total": total})

    def _comfyui_history_once(self, base: str, prompt_id: str) -> tuple[bool, list[str], str]:
        """Return (done, output files, reason); empty history means still running."""
        try:
            with net_io.open(f"{base}/history/{prompt_id}", timeout=30) as resp:
                history = json.loads(resp.read(200000).decode("utf-8", errors="replace"))
            entry = history.get(prompt_id) if isinstance(history, dict) else None
            if not entry:
                return False, [], ""
            status = (entry.get("status") or {}).get("status_str", "")
            if status and status not in {"success", "completed"}:
                if status in {"error", "failed"}:
                    return False, [], f"ERROR:ComfyUI 状态 {status}"
                return False, [], ""
            files = self._comfyui_output_urls(base, entry)
            if not files:
                return False, [], "ERROR:未找到输出文件"
            return True, files, ""
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False, [], ""
            return False, [], f"ERROR:HTTP {exc.code}"
        except Exception:
            return False, [], ""

    def _comfyui_reachable(self, base: str) -> bool:
        try:
            with net_io.open(f"{base}/system_stats", timeout=10) as resp:
                return resp.status == 200
        except Exception:
            return False

    @staticmethod
    def _comfyui_output_urls(base: str, entry: dict[str, Any]) -> list[str]:
        files: list[str] = []
        for node in (entry.get("outputs") or {}).values():
            if not isinstance(node, dict):
                continue
            for kind in ("images", "gifs", "videos", "audio"):
                for item in node.get(kind, []) or []:
                    if not isinstance(item, dict) or not item.get("filename"):
                        continue
                    query = urllib.parse.urlencode({
                        "filename": str(item["filename"]),
                        "subfolder": str(item.get("subfolder") or ""),
                        "type": str(item.get("type") or "output"),
                    })
                    files.append(f"{base}/view?{query}")
        return files

    def _comfyui_submit(self, base: str, workflow: dict[str, Any], index: int) -> str | None:
        del index
        payload = {"prompt": workflow, "client_id": uuid.uuid4().hex}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/prompt", data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with net_io.open(req, timeout=60) as resp:
                body = json.loads(resp.read(100000).decode("utf-8", errors="replace"))
            return str(body.get("prompt_id") or "")
        except Exception as exc:
            traceback.print_exc()
            return None

    def _comfyui_wait_history(
        self, base: str, prompt_id: str, cancel: threading.Event, job_id: str, index: int, total: int
    ) -> tuple[bool, list[str], str]:
        deadline = time.monotonic() + 1800
        attempt = 0
        while True:
            if cancel.is_set():
                return False, [], "用户取消"
            if time.monotonic() > deadline:
                return False, [], f"第 {index + 1} 段生成超时"
            attempt += 1
            self.app.storage.update_job(job_id, attempt=attempt)
            try:
                with net_io.open(f"{base}/history/{prompt_id}", timeout=30) as resp:
                    history = json.loads(resp.read(200000).decode("utf-8", errors="replace"))
                entry = history.get(prompt_id) if isinstance(history, dict) else None
                if entry:
                    files = self._comfyui_output_urls(base, entry)
                    # 验证文件存在且非空
                    ok_all = bool(files)
                    for url in files:
                        try:
                            with net_io.open(url, timeout=20) as r:
                                if int(r.headers.get("Content-Length", "0")) == 0:
                                    ok_all = False
                        except Exception:
                            ok_all = False
                    if ok_all:
                        return True, files, ""
                    return False, files, f"第 {index + 1} 段产物缺失或为空"
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    # 尚未完成，继续轮询
                    pass
                else:
                    return False, [], f"第 {index + 1} 段查询错误：HTTP {exc.code}"
            except Exception:
                pass
            time.sleep(1.0)

    def shutdown(self, timeout: float = 10.0) -> None:
        with self._lock:
            events = list(self._cancel.values())
            threads = list(self._threads.values())
        for event in events:
            event.set()
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)


# 兼容别名
BackgroundTaskManager = JobRegistry
