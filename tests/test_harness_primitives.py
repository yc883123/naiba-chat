import tempfile
import unittest
from pathlib import Path

from skill_runtime import SkillAgent, ToolExecutor
from storage import ChatStorage


class HarnessPrimitiveTests(unittest.TestCase):
    def test_edit_file_requires_unique_match(self):
        root = Path(tempfile.mkdtemp())
        path = root / "sample.txt"
        path.write_text("alpha\nbeta\n", encoding="utf-8")
        executor = ToolExecutor(root, "python", 30, type("MCP", (), {"connections": {}})(), permission_mode="full")
        ok, result = executor.execute("edit_file", {"path": str(path), "old_text": "beta", "new_text": "gamma"}, [])
        self.assertTrue(ok, result)
        self.assertIn("gamma", path.read_text(encoding="utf-8"))

    def test_glob_and_pwsh_are_native_primitives(self):
        root = Path(tempfile.mkdtemp())
        (root / "a.py").write_text("print(1)", encoding="utf-8")
        executor = ToolExecutor(root, "python", 30, type("MCP", (), {"connections": {}})(), permission_mode="full")
        ok, result = executor.execute("glob_files", {"path": str(root), "pattern": "**/*.py"}, [])
        self.assertTrue(ok, result)
        self.assertIn("a.py", result)
        ok, result = executor.execute("pwsh", {"command": "$OutputEncoding = [Console]::OutputEncoding; Write-Output harness"}, [])
        self.assertTrue(ok, result)
        self.assertIn("harness", result)

    def test_actionable_turn_gets_harness_primitives_without_skill(self):
        allowed = {"read_file", "write_file", "edit_file", "glob_files", "pwsh", "run_command", "run_in_background", "job_status", "job_wait", "todo_write", "capability_inventory", "activate_skill"}
        schemas = [{"name": name, "description": name, "parameters": {}} for name in allowed]
        visible = SkillAgent._visible_tool_names("请读取文件并修改脚本，然后运行测试", allowed, schemas, [])
        self.assertTrue({"read_file", "edit_file", "glob_files", "pwsh"}.issubset(visible))
        self.assertIn("run_in_background", visible)
        self.assertIn("todo_write", visible)
        self.assertNotIn("capability_inventory", visible)
        self.assertNotIn("activate_skill", visible)

    def test_parent_run_can_spawn_child_job(self):
        root = Path(tempfile.mkdtemp())
        storage = ChatStorage(root / "chat.db")
        conversation = storage.create_conversation()
        parent = storage.create_run(conversation["id"], "parent", {}, {}, kind="chat")
        child = storage.create_run(
            conversation["id"], "child", {}, {}, kind="shell",
            parent_job_id=parent["id"], owner_session_id=conversation["id"],
        )
        self.assertEqual(parent["id"], child["parent_job_id"])


if __name__ == "__main__":
    unittest.main()
