from __future__ import annotations

import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import model_runtime
from model_runtime import ModelRuntime
import server
from server import ConfigStore, migrate_legacy_data
import tool_registry
from tool_registry import ToolRegistry
from plan_runtime import resolve_mode_tools


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _LMHandler(BaseHTTPRequestHandler):
    stream = False  # 类级开关：True 时 /api/v1/chat 返回 LM Studio 原生 SSE

    def log_message(self, *args) -> None:  # 静默
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        _LMHandler.last_body = raw
        if self.path == "/api/v1/chat":
            if _LMHandler.stream:
                body = (
                    b"data: {\"type\":\"reasoning.delta\",\"content\":\"think \"}\n\n"
                    b"data: {\"type\":\"message.delta\",\"content\":\"Hello\"}\n\n"
                    b"data: {\"type\":\"chat.end\"}\n\n"
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            # 非流式：LM Studio 原生 output[] 结构
            body = json.dumps(
                {
                    "output": [
                        {"type": "reasoning", "content": "I am reasoning"},
                        {"type": "message", "content": "final answer"},
                    ],
                    "output_text": "final answer",
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(404)
        self.end_headers()


_LMHandler.last_body = b""


def _start() -> tuple[HTTPServer, int]:
    port = _free_port()
    srv = HTTPServer(("127.0.0.1", port), _LMHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.1)
    return srv, port


class LMStudioProtocolTests(unittest.TestCase):
    def test_lm_studio_messages_serialization(self) -> None:
        system, parts = ModelRuntime._lm_studio_messages(
            [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "data": "QUJD", "media_type": "image/png"},
                    ],
                },
            ]
        )
        self.assertEqual(system, "be brief")
        self.assertEqual(parts[0], {"type": "text", "content": "hi"})
        self.assertEqual(
            parts[1], {"type": "image", "data_url": "data:image/png;base64,QUJD"}
        )

    def test_reasoning_params_mapping(self) -> None:
        self.assertEqual(ModelRuntime._reasoning_params("lm_studio", "auto"), {})
        self.assertEqual(ModelRuntime._reasoning_params("lm_studio", "off"), {"reasoning": "off"})
        self.assertEqual(ModelRuntime._reasoning_params("lm_studio", "high"), {"reasoning": "high"})
        self.assertEqual(ModelRuntime._reasoning_params("ollama", "medium"), {"think": "medium"})
        self.assertEqual(ModelRuntime._reasoning_params("openai_chat", "low"), {"reasoning_effort": "low"})
        self.assertEqual(ModelRuntime._reasoning_params("openai_chat", "auto"), {})
        self.assertEqual(ModelRuntime._reasoning_params("openai_chat", "off"), {})
        self.assertEqual(
            ModelRuntime._reasoning_params("codex_responses", "high"),
            {"reasoning": {"effort": "high"}},
        )
        # gemini / claude 首期不发送字段
        self.assertEqual(ModelRuntime._reasoning_params("gemini", "high"), {})
        self.assertEqual(ModelRuntime._reasoning_params("claude", "high"), {})

    def test_lm_studio_non_streaming_output(self) -> None:
        result = {
            "output": [
                {"type": "reasoning", "content": "I am reasoning"},
                {"type": "message", "content": "final answer"},
            ]
        }
        self.assertEqual(ModelRuntime._online_content("lm_studio", result), "final answer")
        self.assertEqual(ModelRuntime._online_reasoning("lm_studio", result), "I am reasoning")

    def test_lm_studio_stream_native_sse(self) -> None:
        lines = [
            b'data: {"type":"reasoning.delta","content":"think "}\n',
            b'data: {"type":"message.delta","content":"Hello"}\n',
            b"data: {\"type\":\"chat.end\"}\n",
        ]

        class _Resp:
            def __init__(self, data):
                self._data = data

            def __iter__(self):
                return iter(self._data)

        deltas: list[str] = []
        out = ModelRuntime._read_lm_studio_stream(
            _Resp(lines), lambda e: deltas.append(e.get("content", "")) if e.get("type") == "delta" else None
        )
        self.assertEqual(out["content"], "Hello")
        self.assertEqual(out["reasoning"], "think ")

    def test_lm_studio_stream_error_event(self) -> None:
        lines = [b'data: {"type":"error","error":{"message":"boom"}}\n']

        class _Resp:
            def __init__(self, data):
                self._data = data

            def __iter__(self):
                return iter(self._data)

        with self.assertRaises(RuntimeError):
            ModelRuntime._read_lm_studio_stream(_Resp(lines), None)

    def test_lm_studio_runtime_stream(self) -> None:
        srv, port = _start()
        try:
            _LMHandler.stream = True
            deltas: list[str] = []
            profile = {
                "kind": "local",
                "local_backend": "lm_studio",
                "base_url": f"http://127.0.0.1:{port}/v1",
                "model": "l",
                "request_format": "lm_studio",
            }
            out = ModelRuntime().complete(
                profile,
                [{"role": "user", "content": "hi"}],
                {"stream": True},
                status=lambda e: deltas.append(e["content"]) if e.get("type") == "delta" else None,
            )
            self.assertEqual(out, "Hello")
            self.assertTrue(any("Hello" in d for d in deltas))
            # 校验请求体采用原生序列化（system_prompt + input 结构化部件）
            sent = json.loads(_LMHandler.last_body)
            self.assertIn("system_prompt", sent)
            self.assertIn("input", sent)
            self.assertNotIn("messages", sent)
        finally:
            _LMHandler.stream = False
            srv.shutdown()


class ToolRegistryAnnotationTests(unittest.TestCase):
    def test_mcp_annotations_drive_side_effect(self) -> None:
        reg = ToolRegistry()
        reg._mcp_registry = reg  # 满足 register_mcp_tools 的前置条件
        reg.register_mcp_tools(
            "comfyui",
            [
                {"name": "get_environment", "description": "x", "input_schema": {}, "annotations": {"readOnlyHint": True}},
                {"name": "list_workflows", "description": "x", "input_schema": {}, "annotations": {"readOnlyHint": True}},
                {"name": "run_workflow", "description": "x", "input_schema": {}, "annotations": {}},
            ],
        )
        self.assertFalse(reg.side_effect("mcp__comfyui__get_environment"))
        self.assertFalse(reg.side_effect("mcp__comfyui__list_workflows"))
        self.assertTrue(reg.side_effect("mcp__comfyui__run_workflow"))
        self.assertEqual(
            reg.readonly_mcp_tools(),
            ["mcp__comfyui__get_environment", "mcp__comfyui__list_workflows"],
        )

    def test_resolve_mode_tools_allows_readonly_mcp(self) -> None:
        ro = ["mcp__comfyui__get_environment", "mcp__comfyui__list_workflows", "mcp__comfyui__run_workflow"]
        ask = resolve_mode_tools("ask", ["read_file", "write_file"], ro)
        self.assertIn("mcp__comfyui__get_environment", ask)
        self.assertIn("mcp__comfyui__list_workflows", ask)
        self.assertNotIn("mcp__comfyui__run_workflow", ask)
        self.assertNotIn("write_file", ask)
        # craft 模式不受只读约束：返回全局 agent_tools（MCP 工具运行时由工具表注入）
        craft = resolve_mode_tools("craft", ["read_file", "write_file"], ro)
        self.assertIn("write_file", craft)
        self.assertNotIn("mcp__comfyui__get_environment", craft)


class WorkspaceAndDataTests(unittest.TestCase):
    def _store(self, data: dict) -> ConfigStore:
        path = Path(tempfile.mkdtemp()) / "config.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return ConfigStore(path)

    def test_reasoning_effort_defaulted(self) -> None:
        cs = self._store(
            {
                "providers": [
                    {"id": "p1", "kind": "online", "name": "ON", "base_url": "https://x/v1", "model": "m", "request_format": "openai_chat"},
                ]
            }
        )
        self.assertEqual(cs.model_profiles()[0]["reasoning_effort"], "auto")

    def test_workspace_default_resolves_to_exe_dir(self) -> None:
        exe_dir = Path(tempfile.mkdtemp())
        with patch.object(server, "EXE_DIR", exe_dir):
            cs = self._store({})
            resolved = cs.resolve_workspace_dir()
        self.assertEqual(resolved, (exe_dir / "workspace").resolve())

    def test_workspace_custom_absolute_used(self) -> None:
        custom = Path(tempfile.mkdtemp()) / "ws"
        with patch.object(server, "EXE_DIR", Path(tempfile.mkdtemp())):
            cs = self._store({})
            resolved = cs.resolve_workspace_dir(str(custom))
        self.assertEqual(resolved, custom.resolve())

    def test_validate_workspace_rejects_dangerous(self) -> None:
        app_dir = Path(tempfile.mkdtemp())
        data_dir = app_dir / "data"
        public_dir = app_dir / "public"
        cs = self._store({})
        with patch.object(server, "APP_DIR", app_dir), patch.object(server, "DATA_DIR", data_dir), patch.object(server, "PUBLIC_DIR", public_dir):
            with self.assertRaises(ValueError):
                cs.validate_workspace_dir(Path("C:\\"))  # 磁盘根目录
            with self.assertRaises(ValueError):
                cs.validate_workspace_dir(Path("C:\\Windows"))  # 系统目录
            ok = Path(tempfile.mkdtemp()) / "ok"
            cs.validate_workspace_dir(ok)  # 普通子目录应通过

    def test_update_settings_workspace_creates_and_validates(self) -> None:
        exe_dir = Path(tempfile.mkdtemp())
        app_dir = Path(tempfile.mkdtemp())
        data_dir = app_dir / "data"
        public_dir = app_dir / "public"
        with patch.object(server, "EXE_DIR", exe_dir), patch.object(server, "APP_DIR", app_dir), patch.object(server, "DATA_DIR", data_dir), patch.object(server, "PUBLIC_DIR", public_dir):
            cs = self._store({})
            target = Path(tempfile.mkdtemp()) / "new_ws"
            cs.update_settings({"workspace_dir": str(target)})
            self.assertTrue(target.is_dir())
            self.assertEqual(cs.data["workspace_dir"], str(target))
            # 恢复默认（解析到 EXE_DIR/workspace，需可写）
            (exe_dir / "workspace").mkdir(parents=True, exist_ok=True)
            cs.update_settings({"workspace_dir": ""})
            self.assertEqual(cs.data["workspace_dir"], "workspace")

    def test_public_exposes_resolved_workspace(self) -> None:
        cs = self._store({})
        self.assertIn("resolved_workspace_dir", cs.public())

    def test_migrate_legacy_data_from_exe_dir(self) -> None:
        exe_dir = Path(tempfile.mkdtemp())
        data_dir = Path(tempfile.mkdtemp())
        config_path = data_dir / "config.json"
        data_sub = data_dir / "data"
        (exe_dir / "config.json").write_text(json.dumps({"port": 9999}), encoding="utf-8")
        (exe_dir / "data").mkdir()
        (exe_dir / "data" / "chat.db").write_text("x", encoding="utf-8")
        with patch.object(server, "EXE_DIR", exe_dir), patch.object(server, "APP_DIR", data_dir), patch.object(server, "CONFIG_PATH", config_path), patch.object(server, "DATA_DIR", data_sub):
            report = migrate_legacy_data()
        self.assertTrue(report["migrated"])
        self.assertTrue(config_path.is_file())
        self.assertTrue((data_sub / "chat.db").is_file())

    def test_migrate_legacy_data_repairs_existing_empty_targets(self) -> None:
        exe_dir = Path(tempfile.mkdtemp())
        app_dir = Path(tempfile.mkdtemp())
        config_path = app_dir / "config.json"
        data_sub = app_dir / "data"
        legacy_data = exe_dir / "data"
        legacy_data.mkdir()
        (exe_dir / "config.json").write_text(
            json.dumps({"providers": [{"id": "legacy", "api_key": "secret"}]}),
            encoding="utf-8",
        )
        source_db = legacy_data / "chat.db"
        with sqlite3.connect(source_db) as db:
            db.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO conversations(id) VALUES ('legacy-conversation')")
        data_sub.mkdir()
        config_path.write_text(json.dumps({"providers": []}), encoding="utf-8")
        target_db = data_sub / "chat.db"
        with sqlite3.connect(target_db) as db:
            db.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY)")

        with patch.object(server, "EXE_DIR", exe_dir), patch.object(server, "APP_DIR", app_dir), patch.object(server, "CONFIG_PATH", config_path), patch.object(server, "DATA_DIR", data_sub):
            report = migrate_legacy_data()

        self.assertTrue(report["migrated"])
        self.assertTrue(report["config"])
        self.assertTrue(report["data"])
        self.assertEqual("secret", json.loads(config_path.read_text(encoding="utf-8"))["providers"][0]["api_key"])
        with sqlite3.connect(target_db) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0])

    def test_migrate_legacy_data_does_not_overwrite_nonempty_targets(self) -> None:
        exe_dir = Path(tempfile.mkdtemp())
        app_dir = Path(tempfile.mkdtemp())
        config_path = app_dir / "config.json"
        data_sub = app_dir / "data"
        (exe_dir / "data").mkdir()
        (exe_dir / "config.json").write_text(
            json.dumps({"providers": [{"id": "legacy"}]}), encoding="utf-8"
        )
        source_db = exe_dir / "data" / "chat.db"
        with sqlite3.connect(source_db) as db:
            db.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO conversations(id) VALUES ('legacy')")
        data_sub.mkdir()
        config_path.write_text(json.dumps({"providers": [{"id": "current"}]}), encoding="utf-8")
        target_db = data_sub / "chat.db"
        with sqlite3.connect(target_db) as db:
            db.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO conversations(id) VALUES ('current')")

        with patch.object(server, "EXE_DIR", exe_dir), patch.object(server, "APP_DIR", app_dir), patch.object(server, "CONFIG_PATH", config_path), patch.object(server, "DATA_DIR", data_sub):
            report = migrate_legacy_data()

        self.assertFalse(report["migrated"])
        self.assertEqual("current", json.loads(config_path.read_text(encoding="utf-8"))["providers"][0]["id"])
        with sqlite3.connect(target_db) as db:
            self.assertEqual("current", db.execute("SELECT id FROM conversations").fetchone()[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
