# -*- coding: utf-8 -*-
"""护栏：ConfigStore 配置加载与迁移行为规格。

保护对象：阶段 3 将 ConfigStore 迁出 server.py 到 naiba/config.py 时的行为等价性。
覆盖：全新安装默认、死工具名映射（run_command→pwsh）、历史默认 MCP 入口移除、
MCP 去重与 comfyui 退役、vision 超时迁移、skills 目录清理、损坏 JSON 兜底、save 语义。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import ConfigStore  # noqa: E402


class ConfigMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self, payload=None):
        if payload is not None:
            self.config_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        return ConfigStore(self.config_path)

    def test_fresh_install_defaults_and_save(self):
        store = self._store()
        # 全新安装：代理默认强制直连（与"跟随系统代理"的旧配置升级语义区分）。
        self.assertEqual(store.data["proxy"]["enabled"], False)
        self.assertTrue(self.config_path.exists())
        reloaded = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(reloaded["port"], 8765)

    def test_legacy_default_tools_migrate_run_command_and_drop_mcp_entries(self):
        store = self._store({
            "agent_tools": [
                "read_file", "write_file", "list_directory", "search_files",
                "run_skill_script", "http_request", "run_command",
                "register_mcp", "call_mcp",
            ]
        })
        tools = store.data["agent_tools"]
        self.assertIn("pwsh", tools)
        self.assertNotIn("run_command", tools)
        self.assertNotIn("register_mcp", tools)
        self.assertNotIn("call_mcp", tools)

    def test_customized_tools_keep_register_mcp(self):
        store = self._store({
            "agent_tools": [
                "read_file", "write_file", "list_directory", "search_files",
                "run_skill_script", "http_request", "run_command",
                "register_mcp", "custom_check",
            ]
        })
        tools = store.data["agent_tools"]
        self.assertIn("pwsh", tools)
        self.assertIn("register_mcp", tools)
        self.assertIn("custom_check", tools)
        self.assertNotIn("run_command", tools)

    def test_mcp_servers_deduped_and_legacy_comfyui_retired(self):
        store = self._store({
            "mcp_servers": [
                {"id": "comfyui", "command": "x"},
                {"id": "comfy-mcp", "command": "y"},
                {"id": "comfy-mcp", "command": "y"},
                {"id": "other", "command": "z"},
            ]
        })
        ids = [item["id"] for item in store.data["mcp_servers"]]
        self.assertEqual(ids, ["comfy-mcp", "other"])

    def test_vision_default_timeout_migrated(self):
        store = self._store({"vision": {"timeout_ms": 120000}})
        self.assertEqual(store.data["vision"]["timeout_ms"], 180000)

    def test_custom_vision_timeout_preserved(self):
        store = self._store({"vision": {"timeout_ms": 130000}})
        self.assertEqual(store.data["vision"]["timeout_ms"], 130000)

    def test_bundled_comfyui_mcp_dir_removed_from_skill_roots(self):
        store = self._store({"skills_dirs": ["skills", "data/comfyui-mcp"]})
        self.assertEqual(store.data["skills_dirs"], ["skills"])

    def test_corrupt_json_falls_back_to_defaults(self):
        self.config_path.write_text("{ 这不是合法的 JSON", encoding="utf-8")
        store = self._store()
        self.assertEqual(store.data["port"], 8765)


if __name__ == "__main__":
    unittest.main()
