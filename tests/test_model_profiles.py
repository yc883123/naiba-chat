from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch

import model_runtime
from model_runtime import ModelRuntime
import server
from server import ConfigStore
from storage import ChatStorage


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _MockModelHandler(BaseHTTPRequestHandler):
    routes: dict[str, int] = {}
    last_body: bytes = b""
    last_headers: dict[str, str] = {}

    def log_message(self, *args: Any) -> None:  # 静默
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        _MockModelHandler.last_body = self.rfile.read(length) if length else b""
        _MockModelHandler.last_headers = {key.lower(): value for key, value in self.headers.items()}
        _MockModelHandler.routes[self.path] = _MockModelHandler.routes.get(self.path, 0) + 1
        body = json.dumps({"choices": [{"message": {"content": "mock-ok"}}]}).encode("utf-8")
        if self.path == "/api/chat":  # Ollama 非流式单条 JSON
            body = json.dumps({"message": {"content": "mock-ok", "thinking": ""}, "done": True}).encode("utf-8")
        elif self.path.endswith("/v1/messages"):
            body = json.dumps({
                "id": "msg_mock",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "mock-claude-ok"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }).encode("utf-8")
        elif self.path == "/api/v1/chat":  # LM Studio
            body = json.dumps({"choices": [{"message": {"content": "mock-ok"}}]}).encode("utf-8")
        elif self.path.endswith("/api/generate"):  # Ollama 卸载
            body = json.dumps({"status": "success"}).encode("utf-8")
        elif self.path.endswith("/api/v1/models/unload"):  # LM Studio 卸载
            body = json.dumps({"success": True}).encode("utf-8")
        elif self.path.endswith(("/v1/chat/completions",)) and _MockModelHandler._fail_next:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": "model not found"}}).encode("utf-8"))
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        _MockModelHandler.routes[self.path] = _MockModelHandler.routes.get(self.path, 0) + 1
        if self.path.endswith("/api/tags"):
            body = json.dumps({"models": [{"name": "qwen:latest"}]}).encode("utf-8")
        elif self.path.endswith("/api/v1/models"):
            body = json.dumps({"data": [{"id": "llama"}]}).encode("utf-8")
        elif self.path.endswith("/v1/models"):
            body = json.dumps({"data": [{"id": "m1"}, {"id": "m2"}]}).encode("utf-8")
        else:
            body = json.dumps({}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


_MockModelHandler._fail_next = False


def _start_mock() -> tuple[HTTPServer, int]:
    port = _free_port()
    server_obj = HTTPServer(("127.0.0.1", port), _MockModelHandler)
    thread = threading.Thread(target=server_obj.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)
    return server_obj, port


class ConfigMigrationTests(unittest.TestCase):
    def _store(self, data: dict[str, Any]) -> ConfigStore:
        import tempfile

        path = Path(tempfile.mkdtemp()) / "config.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return ConfigStore(path)

    def test_old_providers_migrated_with_kind(self) -> None:
        cs = self._store(
            {
                "provider_id": "p-online",
                "providers": [
                    {"id": "p-online", "name": "ON", "base_url": "https://x/v1", "model": "m", "request_format": "openai_chat"},
                    {"id": "p-ollama", "name": "OL", "base_url": "http://127.0.0.1:11434/v1", "model": "q", "request_format": "ollama"},
                    {"id": "p-lm", "name": "LM", "base_url": "http://127.0.0.1:1234/v1", "model": "l", "request_format": "lm_studio"},
                ],
            }
        )
        profiles = {p["model_key"]: p for p in cs.model_profiles()}
        self.assertEqual(profiles["online:p-online"]["kind"], "online")
        self.assertEqual(profiles["local:p-ollama"]["kind"], "local")
        self.assertEqual(profiles["local:p-ollama"]["local_backend"], "ollama")
        self.assertEqual(profiles["local:p-lm"]["kind"], "local")
        self.assertEqual(profiles["local:p-lm"]["local_backend"], "lm_studio")

    def test_default_model_key_derived_from_provider_id(self) -> None:
        cs = self._store(
            {
                "provider_id": "p-online",
                "providers": [
                    {"id": "p-online", "name": "ON", "base_url": "https://x/v1", "model": "m", "request_format": "openai_chat"},
                    {"id": "p-ollama", "name": "OL", "base_url": "http://127.0.0.1:11434/v1", "model": "q", "request_format": "ollama"},
                ],
            }
        )
        self.assertEqual(cs.default_model_key(), "online:p-online")

    def test_default_model_key_local_when_provider_id_was_local(self) -> None:
        cs = self._store(
            {
                "provider_id": "p-ollama",
                "providers": [
                    {"id": "p-ollama", "name": "OL", "base_url": "http://127.0.0.1:11434/v1", "model": "q", "request_format": "ollama"},
                ],
            }
        )
        self.assertEqual(cs.default_model_key(), "local:p-ollama")

    def test_existing_kind_preserved(self) -> None:
        cs = self._store(
            {
                "providers": [
                    {"id": "k", "kind": "local", "local_backend": "ollama", "name": "K", "base_url": "http://x/v1", "model": "q", "request_format": "ollama"},
                ]
            }
        )
        prof = cs.model_profiles()[0]
        self.assertEqual(prof["kind"], "local")
        self.assertEqual(prof["local_backend"], "ollama")

    def test_invalid_kind_rejected(self) -> None:
        cs = self._store({"providers": []})
        with self.assertRaises(ValueError):
            cs.upsert_model_profile({"kind": "magic", "name": "X", "base_url": "https://x/v1", "model": "m", "request_format": "openai_chat"})

    def test_invalid_local_backend_rejected(self) -> None:
        cs = self._store({"providers": []})
        with self.assertRaises(ValueError):
            cs.upsert_model_profile({"kind": "local", "local_backend": "vllm", "name": "X", "base_url": "http://x/v1", "model": "m"})

    def test_no_api_key_kept_from_existing(self) -> None:
        cs = self._store(
            {"providers": [{"id": "k", "kind": "online", "name": "K", "base_url": "https://x/v1", "model": "m", "api_key": "secret", "request_format": "openai_chat"}]}
        )
        result = cs.upsert_model_profile({"id": "k", "kind": "online", "name": "K", "base_url": "https://x/v1", "model": "m", "request_format": "openai_chat"})
        self.assertTrue(result["has_api_key"])  # 空 key 提交后保留旧 Key，故仍视为已配置
        # 解析出的配置应保留原 api_key（空值保留旧 Key 的行为）
        prof = cs.profile("online:k")
        self.assertEqual(prof["api_key"], "secret")


class ProfileResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        path = Path(tempfile.mkdtemp()) / "config.json"
        path.write_text(
            json.dumps(
                {
                    "provider_id": "p1",
                    "providers": [
                        {"id": "p1", "kind": "online", "name": "ON", "base_url": "https://x/v1", "model": "m", "request_format": "openai_chat"},
                        {"id": "p2", "kind": "local", "local_backend": "ollama", "name": "OL", "base_url": "http://y/v1", "model": "q", "request_format": "ollama"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.cs = ConfigStore(path)

    def test_online_prefix(self) -> None:
        prof = self.cs.profile("online:p1")
        self.assertEqual(prof["kind"], "online")

    def test_local_prefix(self) -> None:
        prof = self.cs.profile("local:p2")
        self.assertEqual(prof["kind"], "local")

    def test_default_fallback(self) -> None:
        prof = self.cs.profile("")
        self.assertEqual(prof["kind"], "online")

    def test_unknown_model_key_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.cs.profile("online:missing")

    def test_bad_kind_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.cs.profile("sideways:p1")

    def test_set_default(self) -> None:
        self.cs.set_default_model_key("local:p2")
        self.assertEqual(self.cs.default_model_key(), "local:p2")
        self.assertEqual(self.cs.data["provider_id"], "p2")

    def test_delete_model_profile_clears_default(self) -> None:
        self.cs.delete_model_profile("online:p1")
        self.assertEqual(self.cs.default_model_key(), "local:p2")


class RuntimeDispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.port = _start_mock()
        _MockModelHandler.routes.clear()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def setUp(self) -> None:
        _MockModelHandler.routes.clear()
        _MockModelHandler._fail_next = False
        self.base = f"http://127.0.0.1:{self.port}"

    def _online(self) -> dict[str, Any]:
        return {
            "kind": "online",
            "base_url": f"{self.base}/v1",
            "model": "m",
            "api_key": "x",
            "request_format": "openai_chat",
        }

    def _ollama(self) -> dict[str, Any]:
        return {
            "kind": "local",
            "local_backend": "ollama",
            "base_url": self.base,
            "model": "q",
            "request_format": "ollama",
        }

    def _lm_studio(self) -> dict[str, Any]:
        return {
            "kind": "local",
            "local_backend": "lm_studio",
            "base_url": f"{self.base}/v1",
            "model": "l",
            "request_format": "lm_studio",
        }

    def _claude(self) -> dict[str, Any]:
        return {
            "kind": "online",
            "base_url": f"{self.base}/provider/v1",
            "model": "claude-test",
            "api_key": "x",
            "request_format": "claude",
        }

    def _messages(self) -> list[dict[str, Any]]:
        return [{"role": "user", "content": "hi"}]

    def test_online_hits_online_endpoint_only(self) -> None:
        out = ModelRuntime().complete(self._online(), self._messages(), {"stream": False})
        self.assertEqual(out, "mock-ok")
        self.assertIn("/v1/chat/completions", _MockModelHandler.routes)
        self.assertNotIn("/api/chat", _MockModelHandler.routes)
        self.assertTrue(_MockModelHandler.last_headers.get("user-agent", "").startswith("Mozilla/5.0"))

    def test_anthropic_messages_format_and_full_provider_path(self) -> None:
        profile = self._claude()
        self.assertEqual(["m1", "m2"], [m["id"] for m in ModelRuntime.list_online_models(profile)])
        self.assertEqual("mock-claude-ok", ModelRuntime().complete(profile, self._messages(), {"stream": False}))
        self.assertIn("/provider/v1/messages", _MockModelHandler.routes)
        self.assertEqual("x", _MockModelHandler.last_headers.get("x-api-key"))
        self.assertTrue(_MockModelHandler.last_headers.get("user-agent", "").startswith("Mozilla/5.0"))
        full = dict(profile, base_url=f"{self.base}/provider/v1/messages")
        self.assertEqual("mock-claude-ok", ModelRuntime().complete(full, self._messages(), {"stream": False}))
        self.assertEqual(2, _MockModelHandler.routes["/provider/v1/messages"])

    def test_cloudflare_signature_error_is_actionable(self) -> None:
        detail = model_runtime._summarize_http_error(
            json.dumps({"error": {"detail": "browser_signature_banned", "cloudflare_error": True}}),
            "application/json",
            "api.example.com",
        )
        self.assertIn("Cloudflare", detail)
        self.assertIn("手动填写模型名称", detail)

    def test_ollama_hits_local_endpoint_only(self) -> None:
        out = ModelRuntime().complete(self._ollama(), self._messages(), {"stream": False})
        self.assertEqual(out, "mock-ok")
        self.assertIn("/api/chat", _MockModelHandler.routes)
        self.assertNotIn("/v1/chat/completions", _MockModelHandler.routes)

    def test_lm_studio_hits_local_endpoint_only(self) -> None:
        profile = self._lm_studio()
        profile["context_size"] = 32768
        out = ModelRuntime().complete(profile, self._messages(), {"stream": False})
        self.assertEqual(out, "mock-ok")
        self.assertIn("/api/v1/chat", _MockModelHandler.routes)
        self.assertEqual(32768, json.loads(_MockModelHandler.last_body)["context_length"])

    def test_cross_mode_fallback_forbidden(self) -> None:
        bad = self._online()
        bad["request_format"] = "ollama"  # 在线 profile 用了本地协议
        with self.assertRaises(ValueError):
            ModelRuntime().complete(bad, self._messages(), {"stream": False})

    def test_local_with_online_format_forbidden(self) -> None:
        bad = self._ollama()
        bad["request_format"] = "openai_chat"
        with self.assertRaises(ValueError):
            ModelRuntime().complete(bad, self._messages(), {"stream": False})

    def test_missing_model_returns_explicit_error_not_timeout(self) -> None:
        _MockModelHandler._fail_next = True
        with self.assertRaises(RuntimeError) as ctx:
            ModelRuntime().complete(self._online(), self._messages(), {"stream": False})
        self.assertIn("HTTP 404", str(ctx.exception))

    def test_list_models_online_local(self) -> None:
        self.assertEqual(
            [m["id"] for m in ModelRuntime.list_online_models(self._online())], ["m1", "m2"]
        )
        self.assertEqual(
            [m["id"] for m in ModelRuntime.list_online_models(self._ollama())], ["qwen:latest"]
        )
        self.assertEqual(
            [m["id"] for m in ModelRuntime.list_online_models(self._lm_studio())], ["llama"]
        )

    def test_unload_local_model(self) -> None:
        result = ModelRuntime.unload_local_model(self._ollama())
        self.assertEqual(result["provider"], "Ollama")
        self.assertIn("/api/generate", _MockModelHandler.routes)


class ConversationModelKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.db = ChatStorage(Path(tempfile.mkdtemp()) / "chat.db")

    def test_model_key_backfilled_from_provider_id(self) -> None:
        conv = self.db.create_conversation(provider_id="abc")
        self.assertEqual(conv["model_key"], "online:abc")

    def test_explicit_model_key_persisted(self) -> None:
        conv = self.db.create_conversation(provider_id="abc", model_key="local:xyz")
        self.assertEqual(conv["model_key"], "local:xyz")
        reread = self.db.get_conversation(conv["id"], include_messages=False)
        self.assertEqual(reread["model_key"], "local:xyz")
        self.assertEqual(reread["provider_id"], "abc")

    def test_model_key_update_only_this_conversation(self) -> None:
        a = self.db.create_conversation(provider_id="p1")
        b = self.db.create_conversation(provider_id="p1")
        self.db.update_conversation_settings(a["id"], model_key="local:lm")
        self.assertEqual(self.db.get_conversation(a["id"], include_messages=False)["model_key"], "local:lm")
        self.assertEqual(self.db.get_conversation(b["id"], include_messages=False)["model_key"], "online:p1")

    def test_old_conversation_migration(self) -> None:
        conv = self.db.create_conversation(provider_id="p9")
        # 模拟迁移前：model_key 为空的旧记录
        with self.db._connect() as db:
            db.execute("UPDATE conversations SET model_key='' WHERE id=?", (conv["id"],))
        # 重新打开以触发迁移回填
        self.db = ChatStorage(self.db.db_path)
        row = self.db.get_conversation(conv["id"], include_messages=False)
        self.assertEqual(row["model_key"], "online:p9")


if __name__ == "__main__":
    unittest.main(verbosity=2)
