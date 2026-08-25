from __future__ import annotations

import gc
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

from plan_runtime import (
    CraftToolExecutor,
    PlanManager,
    ReadOnlyToolExecutor,
    extract_plan_block,
    parse_plan_document,
    render_plan_markdown,
    resolve_mode_tools,
)
from skill_runtime import TaskCancelled
from storage import ChatStorage


def make_storage(root: str) -> ChatStorage:
    return ChatStorage(Path(root) / "chat.db")


class InteractionModeStorageTests(unittest.TestCase):
    def test_connection_context_closes_database_handle(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = make_storage(root)
            with storage._connect() as db:
                db.execute("SELECT 1").fetchone()

            with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed"):
                db.execute("SELECT 1")

    def test_conversation_always_uses_craft_mode(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = make_storage(root)
            conversation = storage.create_conversation()
            self.assertEqual("craft", conversation["interaction_mode"])

            planned = storage.create_conversation(interaction_mode="plan")
            self.assertEqual("craft", planned["interaction_mode"])
            self.assertEqual(
                "craft",
                storage.get_conversation(planned["id"], include_messages=False)["interaction_mode"],
            )
            del storage
            gc.collect()

    def test_update_settings_validates_interaction_mode(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = make_storage(root)
            conversation = storage.create_conversation()

            updated = storage.update_conversation_settings(conversation["id"], interaction_mode="ask")
            self.assertEqual("craft", updated["interaction_mode"])
            with self.assertRaisesRegex(ValueError, "interaction_mode"):
                storage.update_conversation_settings(conversation["id"], interaction_mode="turbo")
            with self.assertRaisesRegex(ValueError, "非法的计划状态"):
                storage.update_plan("missing", status="turbo")
            del storage
            gc.collect()

    def test_building_plan_is_recovered_as_cancelled_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = make_storage(root)
            conversation = storage.create_conversation()
            plan = storage.create_plan(conversation["id"], "需求")
            storage.update_plan(
                plan["id"],
                status="building",
                steps=[{"id": 1, "title": "步骤一", "detail": "", "status": "running", "summary": ""}],
            )
            del storage
            gc.collect()

            reopened = make_storage(root)
            recovered = reopened.get_plan(plan["id"])

            self.assertEqual("cancelled", recovered["status"])
            self.assertEqual("pending", recovered["steps"][0]["status"])
            self.assertIn("服务重启", recovered["error"])
            del reopened
            gc.collect()


class ModePermissionTests(unittest.TestCase):
    ALL = [
        "read_file", "write_file", "list_directory", "search_files",
        "run_command", "run_skill_script", "http_request",
    ]

    def test_craft_keeps_all_enabled_tools(self) -> None:
        self.assertEqual(self.ALL, resolve_mode_tools("craft", self.ALL))

    def test_ask_and_plan_keep_only_readonly_tools(self) -> None:
        self.assertEqual(
            ["read_file", "list_directory", "search_files", "http_request"],
            resolve_mode_tools("ask", self.ALL),
        )
        self.assertEqual(
            ["read_file", "list_directory", "search_files", "http_request"],
            resolve_mode_tools("plan", self.ALL),
        )

    def test_mode_permissions_intersect_with_global_agent_tools(self) -> None:
        self.assertEqual(["read_file", "http_request"], resolve_mode_tools("ask", ["read_file", "http_request"]))
        self.assertEqual([], resolve_mode_tools("plan", ["write_file", "run_command"]))
        self.assertEqual(["write_file"], resolve_mode_tools("craft", ["write_file", "unknown_tool"]))

    def test_readonly_executor_blocks_writes_and_mcp(self) -> None:
        class FakeInner:
            def execute(self, tool, arguments, active_skills):
                return True, "ok"

        proxy = ReadOnlyToolExecutor(FakeInner())
        for tool in ("write_file", "run_command", "run_skill_script", "register_mcp", "call_mcp", "comfyui.get_environment"):
            success, result = proxy.execute(tool, {}, [])
            self.assertFalse(success, tool)
            self.assertIn("只读", result)

    def test_readonly_executor_allows_only_get_and_head_http(self) -> None:
        class FakeInner:
            def execute(self, tool, arguments, active_skills):
                return True, f"forwarded:{tool}"

        proxy = ReadOnlyToolExecutor(FakeInner())
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            success, result = proxy.execute("http_request", {"method": method}, [])
            self.assertFalse(success, method)
            self.assertIn("GET/HEAD", result)
        self.assertEqual((True, "forwarded:http_request"), proxy.execute("http_request", {"method": "GET"}, []))
        self.assertEqual((True, "forwarded:http_request"), proxy.execute("http_request", {}, []))
        self.assertEqual((True, "forwarded:read_file"), proxy.execute("read_file", {"path": "x"}, []))

    def test_craft_executor_auto_allows_only_workspace_writes(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root) / "workspace"
            workspace.mkdir()

            class FakeInner:
                permission_mode = "confirm"

                def __init__(self):
                    self.workspace = workspace.resolve()

                def _resolve_tool_path(self, raw):
                    path = Path(str(raw))
                    return (self.workspace / path).resolve() if not path.is_absolute() else path.resolve()

                @staticmethod
                def _path_within(path, parent):
                    try:
                        path.relative_to(parent)
                        return True
                    except ValueError:
                        return False

                def _execute_unchecked(self, tool, arguments, active_skills):
                    return True, f"unchecked:{tool}"

                def execute(self, tool, arguments, active_skills):
                    return False, f"checked:{tool}"

            proxy = CraftToolExecutor(FakeInner())
            self.assertEqual(
                (True, "unchecked:write_file"),
                proxy.execute("write_file", {"path": "notes.txt", "content": "ok"}, []),
            )
            outside = str(Path(root).parent / "outside.txt")
            self.assertEqual(
                (False, "checked:write_file"),
                proxy.execute("write_file", {"path": outside, "content": "no"}, []),
            )
            self.assertEqual(
                (False, "checked:run_command"),
                proxy.execute("run_command", {"command": "echo ok"}, []),
            )


class PlanParsingTests(unittest.TestCase):
    def test_parses_title_and_steps_from_document(self) -> None:
        content = """# 重构登录模块
## 方案概述
使用令牌桶限流。
## 实施步骤
1. 新增限流器：在 auth.py 中实现
2. 接入中间件：修改 server.py
3. 补充测试：tests/test_auth.py
"""
        title, steps = parse_plan_document(content)

        self.assertEqual("重构登录模块", title)
        self.assertEqual(3, len(steps))
        self.assertEqual("新增限流器", steps[0]["title"])
        self.assertEqual("pending", steps[0]["status"])
        self.assertIn("auth.py", steps[0]["detail"])

    def test_extracts_plan_block_and_cleans_response(self) -> None:
        response = "前言说明\n<plan>\n# 标题\n1. 第一步：做某事\n</plan>\n\n后记"
        content, cleaned = extract_plan_block(response)

        self.assertIn("# 标题", content)
        self.assertNotIn("<plan>", cleaned)
        self.assertIn("前言说明", cleaned)
        self.assertIsNone(extract_plan_block("没有计划块")[0])


class StubApp:
    def __init__(self, root: str):
        self.storage = make_storage(root)
        workspace = Path(root) / "workspace"
        workspace.mkdir()
        self.config = types.SimpleNamespace(data={"workspace_dir": str(workspace)})


def wait_for_status(storage: ChatStorage, plan_id: str, statuses: set[str], timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        plan = storage.get_plan(plan_id)
        if plan and plan["status"] in statuses:
            return plan
        time.sleep(0.05)
    raise AssertionError(f"计划未在 {timeout}s 内进入状态 {statuses}，当前：{storage.get_plan(plan_id)['status']}")


class PlanManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.app = StubApp(self._tmp.name)
        self.conversation = self.app.storage.create_conversation(interaction_mode="plan")
        self._manager = None

    def tearDown(self) -> None:
        del self.app
        gc.collect()
        self._join_plan_threads()
        self._tmp.cleanup()

    def make_manager(self, runner=None) -> PlanManager:
        self._manager = PlanManager(self.app, step_runner=runner)
        return self._manager

    def _join_plan_threads(self) -> None:
        # ``PlanManager.execute`` runs the plan on a background daemon thread.
        # Join any still-running thread so its sqlite connections are closed
        # before the temp dir is removed; otherwise Windows (WinError 32) refuses
        # to delete the locked chat.db.
        manager = getattr(self, "_manager", None)
        threads = getattr(manager, "_threads", None) if manager else None
        if not isinstance(threads, dict):
            return
        for thread in list(threads.values()):
            if thread is not None and thread.is_alive():
                thread.join(timeout=5)

    def test_ensure_active_plan_reuses_prepare_and_revises_ready(self) -> None:
        manager = self.make_manager()
        plan = manager.ensure_active_plan(self.conversation["id"], "做一个待办应用")
        self.assertEqual("prepare", plan["status"])
        again = manager.ensure_active_plan(self.conversation["id"], "补充说明")
        self.assertEqual(plan["id"], again["id"])
        self.assertEqual("补充说明", again["question"])

        self.app.storage.update_plan(plan["id"], status="ready")
        revised = manager.ensure_active_plan(self.conversation["id"], "改一下方案")
        self.assertEqual(plan["id"], revised["id"])
        self.assertEqual("prepare", revised["status"])
        self.assertEqual("改一下方案", revised["question"])

        self.app.storage.update_plan(plan["id"], status="finished")
        fresh = manager.ensure_active_plan(self.conversation["id"], "新需求")
        self.assertNotEqual(plan["id"], fresh["id"])

    def test_ensure_active_plan_rejects_while_building(self) -> None:
        manager = self.make_manager()
        plan = manager.ensure_active_plan(self.conversation["id"], "需求")
        self.app.storage.update_plan(plan["id"], status="building")
        with self.assertRaisesRegex(ValueError, "正在执行"):
            manager.ensure_active_plan(self.conversation["id"], "再发一条")

    def test_process_response_turns_plan_block_into_ready_plan(self) -> None:
        manager = self.make_manager()
        plan = manager.ensure_active_plan(self.conversation["id"], "做一个待办应用")
        response = "<plan>\n# 待办应用\n## 实施步骤\n1. 设计数据结构：SQLite 表\n2. 实现接口：增删改查\n</plan>"

        cleaned, updated = manager.process_response(plan["id"], response)

        self.assertEqual("ready", updated["status"])
        self.assertEqual("待办应用", updated["title"])
        self.assertEqual(2, len(updated["steps"]))
        self.assertNotIn("<plan>", cleaned)
        self.assertIn("开始执行", cleaned)
        stored = self.app.storage.get_plan(plan["id"])
        archive = Path(stored["archive_path"])
        self.assertTrue(archive.is_file())
        self.assertIn(".naiba-chat", str(archive))

    def test_process_response_without_block_keeps_prepare(self) -> None:
        manager = self.make_manager()
        plan = manager.ensure_active_plan(self.conversation["id"], "需求")
        cleaned, updated = manager.process_response(plan["id"], "请问目标用户是谁？")
        self.assertEqual("请问目标用户是谁？", cleaned)
        self.assertEqual("prepare", updated["status"])
        self.assertEqual(1, updated["detail"]["clarification_round"])
        self.assertIn("不要继续提问", manager.prepare_prompt(updated))

    def test_direct_plan_answer_is_marked_for_compilation(self) -> None:
        manager = self.make_manager()
        plan = manager.ensure_active_plan(self.conversation["id"], "生成一个 CSV 文件")
        self.assertTrue(PlanManager.needs_plan_compilation(plan, "我会生成 CSV 文件。"))
        self.assertFalse(PlanManager.needs_plan_compilation(plan, "请确认文件保存目录？"))
        messages = PlanManager.plan_compilation_messages(plan, "我会生成 CSV 文件。")
        self.assertEqual("system", messages[0]["role"])
        self.assertIn("<plan>", messages[0]["content"])

    def test_process_response_rejects_plan_without_steps(self) -> None:
        manager = self.make_manager()
        plan = manager.ensure_active_plan(self.conversation["id"], "需求")

        cleaned, updated = manager.process_response(
            plan["id"],
            "<plan>\n# 只有标题\n## 方案概述\n尚未列出步骤\n</plan>",
        )

        self.assertIn("只有标题", cleaned)
        self.assertEqual("prepare", updated["status"])
        self.assertEqual([], updated["steps"])
        self.assertIn("没有识别到", updated["error"])

    def test_edit_plan_preserves_step_progress_and_blocks_building(self) -> None:
        manager = self.make_manager()
        plan = manager.ensure_active_plan(self.conversation["id"], "需求")
        manager.process_response(
            plan["id"],
            "<plan>\n# 标题\n## 实施步骤\n1. 第一步：做 A\n2. 第二步：做 B\n</plan>",
        )
        steps = self.app.storage.get_plan(plan["id"])["steps"]
        steps[0]["status"] = "done"
        steps[0]["summary"] = "已完成 A"
        self.app.storage.update_plan(plan["id"], steps=steps)

        edited = manager.edit_plan(
            plan["id"],
            content="# 标题\n## 实施步骤\n1. 第一步：做 A（改）\n2. 第二步：做 B\n3. 第三步：做 C",
        )
        self.assertEqual("done", edited["steps"][0]["status"])
        self.assertEqual("已完成 A", edited["steps"][0]["summary"])
        self.assertEqual("pending", edited["steps"][2]["status"])
        self.assertEqual("ready", edited["status"])

        self.app.storage.update_plan(plan["id"], status="building")
        with self.assertRaisesRegex(ValueError, "无法编辑"):
            manager.edit_plan(plan["id"], content="x")

    def test_execute_runs_steps_in_order_until_finished(self) -> None:
        calls = []

        def runner(plan, step, index, total, cancel_event, event):
            calls.append(step["id"])
            return f"步骤{step['id']}完成"

        manager = self.make_manager(runner)
        plan = manager.ensure_active_plan(self.conversation["id"], "需求")
        manager.process_response(
            plan["id"],
            "<plan>\n# 标题\n## 实施步骤\n1. 一：A\n2. 二：B\n3. 三：C\n</plan>",
        )
        manager.execute(plan["id"])
        finished = wait_for_status(self.app.storage, plan["id"], {"finished"})

        self.assertEqual([1, 2, 3], calls)
        self.assertTrue(all(step["status"] == "done" for step in finished["steps"]))
        markdown = render_plan_markdown(finished)
        self.assertIn("- [x] 1. 一", markdown)
        self.assertTrue(Path(finished["archive_path"]).is_file())

    def test_execute_resumes_from_first_unfinished_step(self) -> None:
        calls = []

        def runner(plan, step, index, total, cancel_event, event):
            calls.append(step["id"])
            return "ok"

        manager = self.make_manager(runner)
        plan = manager.ensure_active_plan(self.conversation["id"], "需求")
        manager.process_response(
            plan["id"],
            "<plan>\n# 标题\n## 实施步骤\n1. 一：A\n2. 二：B\n</plan>",
        )
        steps = self.app.storage.get_plan(plan["id"])["steps"]
        steps[0]["status"] = "done"
        steps[0]["summary"] = "早已完成"
        self.app.storage.update_plan(plan["id"], steps=steps, status="cancelled")

        manager.execute(plan["id"])
        wait_for_status(self.app.storage, plan["id"], {"finished"})

        self.assertEqual([2], calls)

    def test_failed_execution_marks_step_and_can_resume(self) -> None:
        attempts = {"count": 0}

        def flaky_runner(plan, step, index, total, cancel_event, event):
            if step["id"] == 2 and attempts["count"] == 0:
                attempts["count"] += 1
                raise RuntimeError("模型超时")
            return "ok"

        manager = self.make_manager(flaky_runner)
        plan = manager.ensure_active_plan(self.conversation["id"], "需求")
        manager.process_response(
            plan["id"],
            "<plan>\n# 标题\n## 实施步骤\n1. 一：A\n2. 二：B\n</plan>",
        )
        manager.execute(plan["id"])
        failed = wait_for_status(self.app.storage, plan["id"], {"failed"})

        self.assertIn("模型超时", failed["error"])
        self.assertEqual("done", failed["steps"][0]["status"])
        self.assertEqual("failed", failed["steps"][1]["status"])

        manager.execute(plan["id"])
        resumed = wait_for_status(self.app.storage, plan["id"], {"finished"})
        self.assertEqual("done", resumed["steps"][1]["status"])

    def test_cancel_building_plan_stops_at_current_step(self) -> None:
        def blocking_runner(plan, step, index, total, cancel_event, event):
            if cancel_event.wait(5):
                raise TaskCancelled("计划已取消")
            return "ok"

        manager = self.make_manager(blocking_runner)
        plan = manager.ensure_active_plan(self.conversation["id"], "需求")
        manager.process_response(
            plan["id"],
            "<plan>\n# 标题\n## 实施步骤\n1. 一：A\n2. 二：B\n</plan>",
        )
        manager.execute(plan["id"])
        time.sleep(0.2)
        manager.cancel(plan["id"])
        cancelled = wait_for_status(self.app.storage, plan["id"], {"cancelled"})

        self.assertEqual("pending", cancelled["steps"][0]["status"])
        with self.assertRaisesRegex(ValueError, "已完成"):
            finished_plan = self.app.storage.update_plan(plan["id"], status="finished")
            manager.cancel(finished_plan["id"])

    def test_cancel_ready_plan_marks_cancelled_directly(self) -> None:
        manager = self.make_manager()
        plan = manager.ensure_active_plan(self.conversation["id"], "需求")
        manager.process_response(plan["id"], "<plan>\n# 标题\n## 实施步骤\n1. 一：A\n</plan>")

        cancelled = manager.cancel(plan["id"])

        self.assertEqual("cancelled", cancelled["status"])
        with self.assertRaisesRegex(LookupError, "计划不存在"):
            manager.execute("nonexistent")


class FrontendIntegrationTests(unittest.TestCase):
    def test_plan_ui_and_sync_are_removed(self) -> None:
        html = Path("public/index.html").read_text(encoding="utf-8")
        source = Path("public/app.js").read_text(encoding="utf-8")

        self.assertNotIn('id="planModeSwitch"', html)
        self.assertNotIn('data-mode="ask"', html)
        self.assertNotIn('id="planBar"', html)
        self.assertNotIn('id="planEditDialog"', html)
        self.assertNotIn("async function switchPlanMode(enabled)", source)
        self.assertIn("interaction_mode: 'craft'", source)
        self.assertIn("async function sendChatMessage", source)
        self.assertIn("await sendChatMessage(textOverride)", source)
        self.assertIn("fetch('/api/chat'", source)
        self.assertIn("event.type === 'run_started'", source)
        self.assertIn("/api/chat/cancel", source)
        self.assertNotIn("function startPlanSync()", source)
        self.assertNotIn("planLoadSeq: 0", source)
        self.assertNotIn("if (event.plan?.id)", source)

    def test_plan_api_routes_exist(self) -> None:
        server_source = Path("server.py").read_text(encoding="utf-8")

        self.assertIn('path == "/api/plans"', server_source)
        self.assertIn('path.endswith("/execute")', server_source)
        self.assertIn('path.endswith("/cancel")', server_source)
        self.assertIn("def do_PUT(self)", server_source)
        self.assertIn("APP.runs.submit_plan(", server_source)
        self.assertIn('path == "/api/runs"', server_source)
        self.assertIn('path.endswith("/events")', server_source)
        self.assertIn("APP.plans.edit_plan(", server_source)
        self.assertIn('path == "/api/chat/cancel"', server_source)
        self.assertIn('f"{content_type}; charset=utf-8"', server_source)


if __name__ == "__main__":
    unittest.main()
