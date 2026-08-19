from __future__ import annotations

import json
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from capability_runtime import CapabilityRuntime
from model_runtime import ModelRuntime, _InlineReasoningParser
from server import ConfigStore
from skill_runtime import SkillCatalog
from tool_registry import build_tool_registry


class _Mcp:
    @staticmethod
    def states():
        return [{
            "id": "demo",
            "status": "connected",
            "connected": True,
            "error": "",
            "tools": [{"name": "render"}],
        }]


class CapabilityRuntimeTests(unittest.TestCase):
    def make_app(self, root: Path):
        skills = root / "skills"
        skills.mkdir()
        config = ConfigStore(root / "config.json")
        config.update_settings({"skills_dirs": [str(skills)]})
        return types.SimpleNamespace(
            config=config,
            catalog=SkillCatalog([skills]),
            tool_registry=build_tool_registry(),
            mcp=_Mcp(),
        )

    def test_inventory_reports_actual_capabilities_and_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = self.make_app(root)
            ok, text = CapabilityRuntime(app).inventory(
                {
                    "tools": ["read_file", "missing_tool"],
                    "mcp_servers": ["demo", "missing"],
                    "executables": ["python"],
                    "paths": [str(root), str(root / "missing")],
                },
                [],
            )
            payload = json.loads(text)
            self.assertTrue(ok)
            self.assertTrue(payload["checks"]["tools"]["read_file"])
            self.assertFalse(payload["checks"]["tools"]["missing_tool"])
            self.assertTrue(payload["checks"]["mcp_servers"]["demo"])
            self.assertFalse(payload["checks"]["mcp_servers"]["missing"])
            self.assertTrue(payload["checks"]["executables"]["python"])
            self.assertTrue(payload["checks"]["paths"][str(root)])
            self.assertFalse(payload["checks"]["paths"][str(root / "missing")])

    def test_install_skill_adds_it_to_active_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("server.APP_DIR", root):
                app = self.make_app(root)
                source = root / "incoming"
                source.mkdir()
                (source / "SKILL.md").write_text(
                    "---\nname: demo-skill\ndescription: A test skill\n---\n\nUse it.",
                    encoding="utf-8",
                )
                active = []
                ok, text = CapabilityRuntime(app).install_skill(
                    {"source_path": str(source)}, active
                )
                payload = json.loads(text)
                self.assertTrue(ok)
                self.assertEqual("demo-skill", payload["name"])
                self.assertEqual(["demo-skill"], [skill["name"] for skill in active])

    def test_exclusive_inventory_hides_other_skills_and_blocks_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = self.make_app(root)
            skills_root = Path(app.catalog.directories[0])
            for name in ("allowed", "hidden"):
                directory = skills_root / name
                directory.mkdir()
                (directory / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: test\n---\n",
                    encoding="utf-8",
                )
            skills = {skill["name"]: skill for skill in app.catalog.scan()}
            context = {
                "skill_policy": {
                    "mode": "exclusive",
                    "skill_ids": [skills["allowed"]["id"]],
                }
            }

            ok, text = CapabilityRuntime(app).inventory({}, [skills["allowed"]], context)
            payload = json.loads(text)
            install_ok, install_text = CapabilityRuntime(app).install_skill(
                {"source_path": str(root / "anything")}, [], context
            )

            self.assertTrue(ok)
            self.assertEqual(["allowed"], [skill["name"] for skill in payload["skills"]])
            self.assertFalse(install_ok)
            self.assertIn("exclusive", install_text)


class CapabilityToolRegistrationTests(unittest.TestCase):
    def test_generic_orchestration_tools_replace_plan_tool(self):
        registry = build_tool_registry()
        self.assertIn("capability_inventory", registry.names())
        self.assertIn("install_skill", registry.names())
        self.assertNotIn("exit_plan_mode", registry.names())
        self.assertFalse(registry.side_effect("capability_inventory"))
        self.assertTrue(registry.side_effect("install_skill"))


class RuntimeSafetyTests(unittest.TestCase):
    def test_inline_thinking_is_split_across_chunks(self):
        parser = _InlineReasoningParser()
        visible = []
        reasoning = []
        for chunk in ("<thi", "nk>分析", "</think>答", "案"):
            text, thought = parser.feed(chunk)
            visible.append(text)
            reasoning.append(thought)
        text, thought = parser.feed("", final=True)
        visible.append(text)
        reasoning.append(thought)
        self.assertEqual("答案", "".join(visible))
        self.assertEqual("分析", "".join(reasoning))

    def test_local_model_calls_are_serialized(self):
        runtime = ModelRuntime()
        active = 0
        maximum = 0
        guard = threading.Lock()

        def fake_complete(*_args):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with guard:
                active -= 1
            return "ok", "", {}

        threads = []
        with patch.object(runtime, "_complete_online", side_effect=fake_complete):
            for _ in range(4):
                thread = threading.Thread(
                    target=runtime.complete,
                    args=({"kind": "local", "request_format": "ollama"}, [], {}),
                )
                threads.append(thread)
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(1, maximum)


if __name__ == "__main__":
    unittest.main()
