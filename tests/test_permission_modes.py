from __future__ import annotations

import gc
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path

from async_tasks import ConversationRunManager
from skill_runtime import ToolExecutor
from storage import ChatStorage
from tool_registry import ToolRegistry, ToolSpec


class PermissionModeStorageTests(unittest.TestCase):
    def test_old_database_adds_confirm_permission_mode(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "chat.db"
            db = sqlite3.connect(path)
            try:
                db.execute(
                    "CREATE TABLE conversations ("
                    "id TEXT PRIMARY KEY, title TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'online', "
                    "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
                )
                db.execute(
                    "INSERT INTO conversations(id, title, mode, created_at, updated_at) "
                    "VALUES ('legacy', 'Legacy', 'online', 1, 1)"
                )
                db.commit()
            finally:
                db.close()

            storage = ChatStorage(path)
            conversation = storage.get_conversation("legacy", include_messages=False)
            self.assertEqual("confirm", conversation["permission_mode"])
            del storage
            gc.collect()

    def test_conversations_persist_independent_permission_modes(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = ChatStorage(Path(root) / "chat.db")
            first = storage.create_conversation(permission_mode="confirm")
            second = storage.create_conversation(permission_mode="auto")
            storage.update_conversation_settings(first["id"], permission_mode="full")

            rows = {item["id"]: item for item in storage.list_conversations()}
            self.assertEqual("full", rows[first["id"]]["permission_mode"])
            self.assertEqual("auto", rows[second["id"]]["permission_mode"])
            with self.assertRaisesRegex(ValueError, "permission_mode"):
                storage.update_conversation_settings(first["id"], permission_mode="invalid")
            del storage
            gc.collect()


class SnapshotRunManager(ConversationRunManager):
    def _start(self, run, target):
        return None


def make_run_app(storage: ChatStorage, plan: dict | None = None):
    config = types.SimpleNamespace(
        data={"agent_tools": [], "agent_max_steps": 32},
        get_agent=lambda _agent_id: {
            "id": "general",
            "name": "General",
            "system_prompt": "",
            "skill_ids": [],
            "tool_scope": [],
        },
        default_agent_id=lambda: "general",
        generation_options=lambda: {},
        resolve_workspace_dir=lambda: Path(tempfile.gettempdir()),
    )
    plans = types.SimpleNamespace(
        ensure_active_plan=lambda _conversation_id, _message: {"id": "plan-prepare"},
        validate_execution=lambda _plan_id: dict(plan or {}),
        prepare_execution=lambda _plan_id, run_id="": None,
    )
    return types.SimpleNamespace(
        storage=storage,
        config=config,
        plans=plans,
        tool_registry=types.SimpleNamespace(readonly_mcp_tools=lambda: []),
        web_search=types.SimpleNamespace(is_available=lambda: False),
    )


class PermissionModeSnapshotTests(unittest.TestCase):
    def test_chat_run_freezes_conversation_permission_mode(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = ChatStorage(Path(root) / "chat.db")
            conversation = storage.create_conversation(permission_mode="auto")
            manager = SnapshotRunManager(make_run_app(storage))

            run = manager.submit_chat({
                "conversation_id": conversation["id"],
                "message": "hello",
                "model_key": "online:test",
            })
            storage.update_conversation_settings(conversation["id"], permission_mode="full")

            self.assertEqual("auto", storage.get_run_snapshot(run["id"])["permission_mode"])
            del manager
            del storage
            gc.collect()

    def test_plan_execution_freezes_current_permission_mode(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = ChatStorage(Path(root) / "chat.db")
            conversation = storage.create_conversation(permission_mode="full")
            plan = {
                "id": "plan-1",
                "conversation_id": conversation["id"],
                "title": "Plan",
                "status": "ready",
            }
            manager = SnapshotRunManager(make_run_app(storage, plan))

            run = manager.submit_plan(plan["id"])
            storage.update_conversation_settings(conversation["id"], permission_mode="confirm")

            self.assertEqual("full", storage.get_run_snapshot(run["id"])["permission_mode"])
            del manager
            del storage
            gc.collect()


class FakeMCP:
    connections = {}

    def tool_guide(self):
        return ""


class PerRunExecutorTests(unittest.TestCase):
    def test_run_executors_have_independent_modes_and_confirmation_state(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = ChatStorage(Path(root) / "chat.db")
            first_conversation = storage.create_conversation()
            second_conversation = storage.create_conversation()
            first_run = storage.create_run(
                first_conversation["id"], "one", {}, {"permission_mode": "confirm"}
            )
            second_run = storage.create_run(
                second_conversation["id"], "two", {}, {"permission_mode": "auto"}
            )
            base = ToolExecutor(Path(root), "python", 30, FakeMCP())
            app = types.SimpleNamespace(storage=storage, executor=base)
            manager = ConversationRunManager(app)

            first = manager.executor_for_run(first_run["id"])
            second = manager.executor_for_run(second_run["id"])
            self.assertIsNot(first, second)
            self.assertEqual("confirm", first.permission_mode)
            self.assertEqual("auto", second.permission_mode)
            self.assertIsNot(first.pending_confirmation, second.pending_confirmation)
            self.assertIsNot(first._confirmation_lock, second._confirmation_lock)
            del manager
            del storage
            gc.collect()

    def test_confirmation_and_rejection_only_reach_owning_run(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = ChatStorage(Path(root) / "chat.db")
            first_conversation = storage.create_conversation()
            second_conversation = storage.create_conversation()
            first_run = storage.create_run(first_conversation["id"], "one", {}, {"permission_mode": "confirm"})
            second_run = storage.create_run(second_conversation["id"], "two", {}, {"permission_mode": "confirm"})
            manager = ConversationRunManager(
                types.SimpleNamespace(
                    storage=storage,
                    executor=ToolExecutor(Path(root), "python", 30, FakeMCP()),
                )
            )
            first = manager.executor_for_run(first_run["id"])
            second = manager.executor_for_run(second_run["id"])
            _, first_need = first.execute("write_file", {"path": "one.txt", "content": "one"}, [])
            _, second_need = second.execute("write_file", {"path": "two.txt", "content": "two"}, [])
            first_id = first_need.split(":", 3)[1]
            second_id = second_need.split(":", 3)[1]
            storage.update_background_task(first_run["id"], status="waiting", detail={"confirm_id": first_id})
            storage.update_background_task(second_run["id"], status="waiting", detail={"confirm_id": second_id})

            self.assertIsNone(manager.confirm_tool(first_run["id"], second_id))
            confirmed = manager.confirm_tool(first_run["id"], first_id)
            self.assertTrue(confirmed[0])
            self.assertIn(str(Path(root, "one.txt").resolve()), confirmed[1])
            rejected = manager.reject_tool(second_run["id"], second_id)
            self.assertFalse(rejected[0])
            self.assertFalse(Path(root, "two.txt").exists())
            del manager
            del storage
            gc.collect()


class ToolRegistryRunOverrideTests(unittest.TestCase):
    def test_run_context_executor_overrides_global_executor(self) -> None:
        calls: list[str] = []

        class Executor:
            def __init__(self, name):
                self.name = name

            def execute(self, tool, arguments, active_skills):
                calls.append(self.name)
                return True, self.name

        registry = ToolRegistry()
        registry.register(ToolSpec("read_file", "read", {"type": "object"}))
        registry.bind_executor(Executor("global"))

        result = registry.execute("read_file", {}, [], {"executor": Executor("run")})
        self.assertEqual((True, "run"), result)
        self.assertEqual(["run"], calls)


class PermissionModeFrontendTests(unittest.TestCase):
    def test_approval_control_is_in_composer_not_agent_settings(self) -> None:
        html = Path("public/index.html").read_text(encoding="utf-8")
        composer = html[html.index('<div class="composer-meta">'):html.index("</section>", html.index('<div class="composer-meta">'))]
        agent = html[html.index('<section data-settings-panel="agent"'):html.index('<section data-settings-panel="runtime"')]
        self.assertIn('id="permissionModeSwitch"', composer)
        self.assertNotIn("permissionModeSwitch", agent)
        self.assertNotIn('name="permissionMode"', html)
        self.assertIn("保存工具范围", agent)

    def test_interaction_modes_use_plan_checkbox(self) -> None:
        html = Path("public/index.html").read_text(encoding="utf-8")
        self.assertIn('id="planModeSwitch"', html)
        self.assertIn('type="checkbox"', html)
        self.assertNotIn('id="modeSwitch"', html)
        self.assertNotIn('value="ask"', html)

    def test_frontend_persists_conversation_permission_mode(self) -> None:
        source = Path("public/app.js").read_text(encoding="utf-8")
        self.assertIn("body: { permission_mode: mode }", source)
        self.assertIn("renderPermissionModeSwitch()", source)
        self.assertIn("mode === 'full' && !confirm(", source)


if __name__ == "__main__":
    unittest.main()
