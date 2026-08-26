"""PLAN4 实施验收测试：内置 Agent、视觉工具、联网搜索、MCP 去重。"""
from __future__ import annotations

import json
import tempfile
import types
from pathlib import Path
from unittest import TestCase

import server
from async_tasks import ConversationRunManager
from tool_registry import build_tool_registry
from web_search_runtime import WebSearchRuntime
import vision_runtime


class BuiltInAgentsTest(TestCase):
    def test_builtin_present_and_have_scope(self):
        ids = server.built_in_agent_ids()
        self.assertEqual(
            ids, {"dsh-standard", "dsh-code", "dsh-minimal", "dsh-cordis"}
        )
        for agent in server.built_in_agents():
            self.assertIn("tool_scope", agent)
            self.assertTrue(agent["built_in"])
            self.assertTrue(agent["tool_scope"])

    def test_public_agents_includes_builtin(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = server.ConfigStore(Path(tmp) / "config.json")
            merged = cfg.public_agents()
            merged_ids = {a["id"] for a in merged}
            self.assertTrue(server.built_in_agent_ids().issubset(merged_ids))

    def test_get_agent_resolves_builtin(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = server.ConfigStore(Path(tmp) / "config.json")
            agent = cfg.get_agent("dsh-minimal")
            self.assertIsNotNone(agent)
            self.assertEqual(agent["id"], "dsh-minimal")
            self.assertIn("read_file", agent["tool_scope"])
            self.assertNotIn("web_search", agent["tool_scope"])

    def test_upsert_rejects_builtin(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = server.ConfigStore(Path(tmp) / "config.json")
            with self.assertRaises(ValueError):
                cfg.upsert_agent({"id": "dsh-standard", "name": "x", "system_prompt": ""})

    def test_delete_rejects_builtin(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = server.ConfigStore(Path(tmp) / "config.json")
            self.assertFalse(cfg.delete_agent("dsh-code"))

    def test_custom_agent_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = server.ConfigStore(Path(tmp) / "config.json")
            cfg.upsert_agent({"id": "myagent", "name": "我的", "system_prompt": "hi"})
            self.assertIsNotNone(cfg.get_agent("myagent"))

    def test_full_agent_has_generic_capability_tools(self):
        agent = next(a for a in server.built_in_agents() if a["id"] == "dsh-standard")
        self.assertIn("capability_inventory", agent["tool_scope"])
        self.assertIn("install_skill", agent["tool_scope"])
        self.assertNotIn("计划", agent["system_prompt"])


class RunToolScopeTest(TestCase):
    def _manager(self, search_available: bool = True) -> ConversationRunManager:
        app = types.SimpleNamespace(
            config=types.SimpleNamespace(data=server.default_config()),
            tool_registry=build_tool_registry(),
            web_search=types.SimpleNamespace(is_available=lambda: search_available),
        )
        return ConversationRunManager(app)

    def test_plan_execution_keeps_minimal_agent_scope(self):
        agent = next(a for a in server.built_in_agents() if a["id"] == "dsh-minimal")
        allowed = self._manager()._resolve_allowed_tools("craft", agent, True)
        self.assertIn("read_file", allowed)
        self.assertIn("run_command", allowed)
        self.assertNotIn("subagent", allowed)
        self.assertNotIn("vision_describe", allowed)
        self.assertNotIn("web_search", allowed)
        self.assertNotIn("call_mcp", allowed)

    def test_search_available_only_requires_endpoint(self):
        agent = next(a for a in server.built_in_agents() if a["id"] == "dsh-standard")
        # web_search 不再由发送区开关（web_search_enabled）控制，只由端点可用性决定。
        # 端点可用 → 始终声明（无论开关）；端点不可用 → 不声明。
        self.assertIn(
            "web_search", self._manager()._resolve_allowed_tools("craft", agent, False)
        )
        self.assertNotIn(
            "web_search", self._manager(False)._resolve_allowed_tools("craft", agent, True)
        )
        self.assertIn(
            "web_search", self._manager()._resolve_allowed_tools("craft", agent, True)
        )

    def test_multimodal_model_hides_redundant_vision_tools_at_snapshot_boundary(self):
        manager = self._manager()
        manager.app.config.profile = lambda _key: {"model": "qwen-vl", "supports_images": True}
        manager.app.vision = types.SimpleNamespace(brain_supports_images=lambda _profile: True)
        allowed = manager._resolve_allowed_tools("craft", {"tool_scope": []}, True, "local:vl")
        # 多模态大脑本身就是读图模型：不暴露重复的按需 vision_* 工具，但保留
        # vision_read_folder（它专门把文件夹里的多张图作为 image content 注入给多模态大脑）。
        self.assertFalse(
            any(tool.startswith("vision_") and tool != "vision_read_folder" for tool in allowed)
        )


class VisionToolsTest(TestCase):
    def test_vision_tools_registered(self):
        reg = build_tool_registry()
        for name in (
            "vision_describe", "vision_ground", "vision_detect", "vision_crop",
            "vision_ocr", "vision_colors", "vision_pixel_diff",
        ):
            self.assertIn(name, reg.names())
        # 写文件类工具标记为有副作用，只读分析类无副作用。
        self.assertFalse(reg.side_effect("vision_describe"))
        self.assertTrue(reg.side_effect("vision_crop"))

    def test_vision_read_folder_registered(self):
        reg = build_tool_registry()
        self.assertIn("vision_read_folder", reg.names())
        self.assertFalse(reg.side_effect("vision_read_folder"))


class WebSearchToolTest(TestCase):
    def test_search_tool_registered(self):
        reg = build_tool_registry()
        self.assertIn("web_search", reg.names())
        self.assertFalse(reg.side_effect("web_search"))

    def _fake_app(self, search: dict) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            config=types.SimpleNamespace(data={"search": search})
        )

    def test_missing_endpoint_degrades(self):
        app = self._fake_app({"enabled": False})
        rt = WebSearchRuntime(app)
        self.assertFalse(rt.is_available())
        ok, msg = rt.search("naiba-chat")
        self.assertFalse(ok)
        self.assertIn("未配置搜索端点", msg)

    def test_composer_switch_owns_enable_state(self):
        app = self._fake_app({"enabled": False, "endpoint": "https://search.example.com"})
        self.assertTrue(WebSearchRuntime(app).is_available())

    def test_selected_custom_search_profile(self):
        app = self._fake_app({
            "provider_id": "second",
            "profiles": [
                {"id": "first", "name": "First", "endpoint": "https://first.example.com"},
                {"id": "second", "name": "Second", "endpoint": "https://second.example.com", "max_results": 8},
            ],
        })
        cfg = WebSearchRuntime(app).config()
        self.assertEqual("Second", cfg["name"])
        self.assertEqual("https://second.example.com", cfg["endpoint"])
        self.assertEqual(8, cfg["max_results"])

    def test_normalize_results(self):
        payload = {
            "organic": [
                {"title": "A", "link": "https://a.example.com", "snippet": "关于A", "datePublished": "2026-01-01"},
                {"title": "B", "link": "not-a-url", "snippet": "坏链接"},
            ]
        }
        results = WebSearchRuntime._normalize(payload, 10)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "A")
        self.assertEqual(results[0]["url"], "https://a.example.com")
        self.assertEqual(results[0]["published"], "2026-01-01")


class McpDedupTest(TestCase):
    def test_duplicate_server_id_deduped(self):
        data = {
            "mcp_servers": [
                {"id": "comfyui", "command": "py", "args": [], "env": {}, "enabled": True},
                {"id": "comfyui", "command": "py2", "args": [], "env": {}, "enabled": True},
                {"id": "other", "command": "py3", "args": [], "env": {}, "enabled": True},
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            cfg = server.ConfigStore(path)
            ids = [s["id"] for s in cfg.data["mcp_servers"]]
            self.assertEqual(ids, ["other"])


class VisionRoutingTest(TestCase):
    def _router(self) -> "vision_runtime.VisionRouter":
        app = types.SimpleNamespace(
            config=types.SimpleNamespace(
                data={"vision": {}},
                resolve_workspace_dir=lambda: Path(tempfile.gettempdir()),
            )
        )
        return vision_runtime.VisionRouter(app)

    def test_brain_supports_image_skips_route_when_auto_route_is_off(self):
        router = self._router()
        router.app.config.data["vision"]["auto_route"] = False
        history = [
            {"role": "user", "content": [
                {"type": "text", "text": "看这张"},
                {"type": "image", "media_type": "image/png", "data": "x"},
            ]}
        ]
        brain = {"model": "gemini-2.0", "request_format": "gemini"}
        out, note = router.prepare_history(history, brain)
        self.assertEqual(note, "")
        self.assertIs(out, history)

    def test_path_memory_replace(self):
        router = self._router()
        router._path_cache["C:/img/1.png"] = "这是一只猫"
        text, hit = router._replace_upload_placeholders(
            "参考 [用户上传文件：C:/img/1.png]", router._path_cache
        )
        self.assertTrue(hit)
        self.assertIn("这是一只猫", text)
        self.assertIn("不可信证据", text)


if __name__ == "__main__":
    import unittest

    unittest.main()
