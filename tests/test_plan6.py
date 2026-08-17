"""PLAN6 视觉链路修复验收测试。

覆盖：
- _run_chat 先解析 profile 再调用 prepare_history（profile 不再未定义）。
- supports_images 能力字段：DeepSeek 官方默认 false，gemini/claude/vl 推断 true，显式覆盖优先。
- 纯文本大脑：无论 auto_route 开关，image 部件都被移除，原始 image_url 绝不落入文本接口。
- 视觉大脑：保留原始 image 部件不变。
- probe() 发送真实最小图片请求并沿 failover 链返回可识别后端名称与失败原因。
"""
from __future__ import annotations

import json
import threading
import tempfile
import types
from pathlib import Path
from unittest import TestCase
from unittest import mock

import server
import vision_runtime
import async_tasks


class SupportsImagesTest(TestCase):
    def _provider(self, **overrides) -> dict:
        base = {
            "id": "p1",
            "kind": "online",
            "name": "P1",
            "model": "deepseek-chat",
            "request_format": "openai_chat",
            "base_url": "https://api.deepseek.com",
            "api_key": "x",
        }
        base.update(overrides)
        return base

    def test_deepseek_official_default_false(self):
        self.assertFalse(server._infer_supports_images(self._provider()))

    def test_deepseek_explicit_true(self):
        self.assertTrue(server._infer_supports_images(self._provider(supports_images=True)))

    def test_deepseek_explicit_false(self):
        self.assertFalse(server._infer_supports_images(self._provider(supports_images=False)))

    def test_gemini_inferred_true(self):
        self.assertTrue(server._infer_supports_images(self._provider(model="gemini-2.0", base_url="https://x")))

    def test_claude_inferred_true(self):
        self.assertTrue(server._infer_supports_images(self._provider(model="claude-3", base_url="https://x")))

    def test_vl_model_inferred_true(self):
        self.assertTrue(server._infer_supports_images(self._provider(model="qwen2.5-vl-72b", base_url="https://x")))

    def test_model_profiles_carries_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = {
                "providers": [
                    self._provider(id="ds", model="deepseek-chat", base_url="https://api.deepseek.com"),
                    self._provider(id="vl", model="qwen2.5-vl-72b", base_url="https://x"),
                ]
            }
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            cfg = server.ConfigStore(path)
            profiles = {p["id"]: p for p in cfg.model_profiles()}
            self.assertFalse(profiles["ds"]["supports_images"])
            self.assertTrue(profiles["vl"]["supports_images"])

    def test_profile_carries_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = {"providers": [self._provider(id="ds", model="deepseek-chat", base_url="https://api.deepseek.com")]}
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            cfg = server.ConfigStore(path)
            self.assertFalse(cfg.profile("online:ds")["supports_images"])

    def test_context_window_uses_known_capability_only(self):
        self.assertEqual(128000, server._infer_context_window(self._provider()))
        self.assertEqual(
            0,
            server._infer_context_window(
                self._provider(base_url="https://api.example.com/v1", model="unknown")
            ),
        )

    def test_local_context_window_comes_from_provider_config(self):
        provider = self._provider(
            kind="local",
            request_format="lm_studio",
            local_backend="lm_studio",
            base_url="http://127.0.0.1:1234/v1",
            context_size=65536,
        )
        self.assertEqual(65536, server._infer_context_window(provider))

    def test_upsert_persists_and_clears_explicit_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = server.ConfigStore(Path(tmp) / "config.json")
            saved = cfg.upsert_model_profile(
                {
                    "id": "custom",
                    "kind": "online",
                    "name": "Custom",
                    "base_url": "https://api.example.com/v1",
                    "model": "custom-model",
                    "api_key": "x",
                    "request_format": "openai_chat",
                    "supports_images": True,
                }
            )
            self.assertTrue(saved["supports_images"])
            self.assertTrue(saved["supports_images_explicit"])
            self.assertTrue(cfg.profile("online:custom")["supports_images"])

            cleared = cfg.upsert_model_profile(
                {
                    "id": "custom",
                    "kind": "online",
                    "name": "Custom",
                    "base_url": "https://api.example.com/v1",
                    "model": "custom-model",
                    "api_key": "",
                    "request_format": "openai_chat",
                    "supports_images": None,
                }
            )
            self.assertIsNone(cleared["supports_images_explicit"])
            self.assertFalse(cleared["supports_images"])


class PrepareHistoryCleaningTest(TestCase):
    def _router(self, auto_route: bool = True) -> "vision_runtime.VisionRouter":
        app = types.SimpleNamespace(
            config=types.SimpleNamespace(
                data={"vision": {"auto_route": auto_route}},
                resolve_workspace_dir=lambda: Path(tempfile.gettempdir()),
            )
        )
        return vision_runtime.VisionRouter(app)

    def _img_history(self) -> list[dict]:
        return [{
            "role": "user",
            "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image", "media_type": "image/png", "data": "RAWIMAGEURL", "name": "a.png"},
            ],
        }]

    def test_text_brain_strips_images_with_auto_route(self):
        router = self._router(auto_route=True)
        history = self._img_history()
        # 视觉后端不可用，prepare_history 应降级为文本占位，绝不含原始 image_url。
        with mock.patch.object(router, "describe_parts", side_effect=RuntimeError("offline")):
            out, note = router.prepare_history(
                history,
                {"model": "deepseek-chat", "base_url": "https://api.deepseek.com"},
            )
        self.assertIn("已自动识图", note)
        content = out[0]["content"]
        self.assertIsInstance(content, list)
        self.assertTrue(all(part.get("type") != "image" for part in content))
        blob = json.dumps(content, ensure_ascii=False)
        self.assertNotIn("RAWIMAGEURL", blob)
        self.assertIn("自动识图失败", blob)

    def test_text_brain_strips_images_with_auto_route_off(self):
        router = self._router(auto_route=False)
        history = self._img_history()
        out, note = router.prepare_history(history, {"model": "deepseek-chat", "base_url": "https://api.deepseek.com"})
        self.assertIn("已移除", note)
        content = out[0]["content"]
        self.assertTrue(all(part.get("type") != "image" for part in content))
        blob = json.dumps(content, ensure_ascii=False)
        self.assertNotIn("RAWIMAGEURL", blob)

    def test_vision_brain_keeps_images(self):
        router = self._router(auto_route=True)
        history = self._img_history()
        out, note = router.prepare_history(history, {"model": "gemini-2.0", "request_format": "gemini"})
        self.assertEqual(note, "")
        self.assertIs(out, history)
        self.assertTrue(any(part.get("type") == "image" for part in out[0]["content"]))

    def test_explicit_supports_images_true_keeps_images(self):
        router = self._router(auto_route=True)
        history = self._img_history()
        out, note = router.prepare_history(history, {"model": "my-model", "supports_images": True})
        self.assertEqual(note, "")
        self.assertIs(out, history)

    def test_explicit_false_overrides_vision_name_heuristic(self):
        router = self._router(auto_route=False)
        history = self._img_history()
        out, _note = router.prepare_history(
            history,
            {
                "model": "deepseek-vision",
                "base_url": "https://api.deepseek.com",
                "supports_images": False,
            },
        )
        self.assertFalse(any(part.get("type") == "image" for part in out[0]["content"]))

    def test_invalid_max_images_still_strips_images(self):
        router = self._router(auto_route=False)
        router.app.config.data["vision"]["max_images"] = None
        out, _note = router.prepare_history(
            self._img_history(),
            {"model": "deepseek-chat", "supports_images": False},
        )
        self.assertFalse(any(part.get("type") == "image" for part in out[0]["content"]))

    def test_fail_closed_strip_removes_all_images(self):
        out, removed = vision_runtime.VisionRouter.strip_images_for_text_model(
            self._img_history(), "boom"
        )
        self.assertEqual(1, removed)
        self.assertNotIn("RAWIMAGEURL", json.dumps(out, ensure_ascii=False))


class VisionProbeTest(TestCase):
    def _router(self) -> "vision_runtime.VisionRouter":
        app = types.SimpleNamespace(
            config=types.SimpleNamespace(
                data={"vision": {}},
                resolve_workspace_dir=lambda: Path(tempfile.gettempdir()),
            )
        )
        return vision_runtime.VisionRouter(app)

    def test_probe_sends_real_image_and_reports_backend(self):
        router = self._router()
        captured = {}

        def fake_call(profile, image_parts, question, max_tokens=2048, **kwargs):
            captured["profile"] = profile
            captured["image_parts"] = image_parts
            captured["max_tokens"] = max_tokens
            captured["kwargs"] = kwargs
            # 校验：真实最小图片请求，media_type 为 image/png，且确实带图。
            self.assertEqual(image_parts[0]["media_type"], "image/png")
            self.assertTrue(image_parts[0]["data"])
            return "OK"

        with mock.patch.object(router, "_call_backend", side_effect=fake_call):
            result = router.probe()
        self.assertTrue(result["ok"])
        self.assertIn("backend", result)
        self.assertIn("latency_ms", result)
        self.assertEqual(captured["max_tokens"], 16)
        self.assertEqual(captured["kwargs"]["attempts"], 1)
        self.assertLessEqual(captured["kwargs"]["timeout_seconds"], 30)
        self.assertTrue(captured["kwargs"]["connection_test"])

    def test_probe_failover_reports_reason_and_backend(self):
        router = self._router()
        # 让所有后端失败，验证返回聚合原因 + 最后尝试的后端名称。
        with mock.patch.object(router, "_call_backend", side_effect=RuntimeError("boom")):
            result = router.probe()
        self.assertFalse(result["ok"])
        self.assertIn("boom", result["reason"])
        self.assertIn("backend", result)

    def test_explicit_provider_does_not_fall_back_to_anonymous_ovh(self):
        profile = {
            "kind": "online",
            "request_format": "openai_chat",
            "base_url": "https://vision.example.com/v1",
            "model": "vision-model",
            "name": "Selected Vision",
        }
        app = types.SimpleNamespace(
            config=types.SimpleNamespace(
                data={"vision": {}},
                profile=lambda key: profile if key == "online:selected" else None,
                resolve_workspace_dir=lambda: Path(tempfile.gettempdir()),
            )
        )
        router = vision_runtime.VisionRouter(app)

        backends = router.vision_backends("online:selected")

        self.assertEqual(["Selected Vision"], [item["name"] for item in backends])
        self.assertFalse(any("OVH" in item["name"] for item in backends))

    def test_backend_passes_timeout_and_attempt_overrides_to_runtime(self):
        router = self._router()
        captured = {}

        def fake_complete(_profile, _messages, options, _status):
            captured.update(options)
            return "OK"

        with mock.patch.object(router._runtime, "complete", side_effect=fake_complete):
            result = router._call_backend(
                {"model": "vision", "base_url": "https://api.example.com"},
                [{"type": "image", "media_type": "image/png", "data": "x"}],
                "test",
                timeout_seconds=7,
                attempts=1,
                connection_test=True,
            )
        self.assertEqual("OK", result)
        self.assertEqual(7, captured["request_timeout_seconds"])
        self.assertEqual(1, captured["request_attempts"])
        self.assertTrue(captured["connection_test"])


class RunChatVisionFailClosedTest(TestCase):
    def test_prepare_history_exception_never_reaches_agent_as_image(self):
        raw_history = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image", "media_type": "image/png", "data": "SECRET_IMAGE"},
            ],
        }]

        class Storage:
            def __init__(self):
                self.run = {
                    "id": "run1",
                    "conversation_id": "conversation1",
                    "message": "看图",
                    "interaction_mode": "craft",
                    "status": "queued",
                    "detail": {},
                }
                self.snapshot = {
                    "model_key": "online:deepseek",
                    "conversation_messages": [],
                    "generation_options": {},
                    "stream_enabled": False,
                    "agent": {},
                    "allowed_tools": [],
                }
                self.events = []

            def get_background_task(self, _run_id):
                return dict(self.run)

            def get_run_snapshot(self, _run_id):
                return dict(self.snapshot)

            def update_background_task(self, _run_id, **values):
                self.run.update({key: value for key, value in values.items() if value is not None})
                return dict(self.run)

            def append_run_event(self, _run_id, payload):
                self.events.append(payload)
                return payload

            def add_message(self, _conversation_id, _role, _content, _metadata):
                return {"id": "message1"}

            def log_tool_run(self, *_args):
                return None

        class BrokenVision:
            def prepare_history(self, _history, _profile):
                raise RuntimeError("router failed")

            strip_images_for_text_model = staticmethod(
                vision_runtime.VisionRouter.strip_images_for_text_model
            )

        captured = {}

        class FakeSkillAgent:
            def __init__(self, *_args):
                pass

            def run(self, _message, history, *_args, **_kwargs):
                captured["history"] = history
                return "OK", [], [], {}

        config = types.SimpleNamespace(
            data={"agent_max_steps": 32},
            profile=lambda _key: {
                "name": "deepseek",
                "model": "deepseek-chat",
                "base_url": "https://api.deepseek.com",
                "request_format": "openai_chat",
                "supports_images": False,
            },
            generation_options=lambda: {},
        )
        app = types.SimpleNamespace(
            storage=Storage(),
            config=config,
            vision=BrokenVision(),
            web_search=types.SimpleNamespace(is_available=lambda: False),
            executor=types.SimpleNamespace(),
            catalog=types.SimpleNamespace(),
            models=types.SimpleNamespace(complete=lambda *_args: "OK"),
            plans=types.SimpleNamespace(),
            tool_registry=types.SimpleNamespace(),
        )
        manager = async_tasks.ConversationRunManager(app)
        with (
            mock.patch.object(server, "build_model_history", return_value=raw_history),
            mock.patch.object(async_tasks, "SkillAgent", FakeSkillAgent),
        ):
            manager._run_chat("run1", threading.Event())

        blob = json.dumps(captured["history"], ensure_ascii=False)
        self.assertNotIn("SECRET_IMAGE", blob)
        self.assertNotIn('"type": "image"', blob)


class VisionSettingsFrontendTest(TestCase):
    def test_visual_provider_uses_full_model_key(self):
        app_js = (Path(__file__).parents[1] / "public" / "app.js").read_text(encoding="utf-8")
        self.assertIn("provider.model_key || provider.id", app_js)

    def test_web_search_button_is_immediately_left_of_send(self):
        index_html = (Path(__file__).parents[1] / "public" / "index.html").read_text(
            encoding="utf-8"
        )
        search_position = index_html.index('id="webSearchButton"')
        send_position = index_html.index('id="sendButton"')
        self.assertLess(search_position, send_position)
        between = index_html[search_position:send_position]
        self.assertNotIn("<textarea", between)
        self.assertNotIn("</form>", between)


if __name__ == "__main__":
    import unittest

    unittest.main()
