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


class VisionConfigMigrationTest(TestCase):
    def test_new_config_uses_180_second_per_request_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = server.ConfigStore(Path(tmp) / "config.json")
            self.assertEqual(180000, cfg.data["vision"]["timeout_ms"])

    def test_old_default_timeout_migrates_but_custom_value_is_preserved(self):
        for configured, expected in ((120000, 180000), (90000, 90000)):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.json"
                path.write_text(json.dumps({"vision": {"timeout_ms": configured}}), encoding="utf-8")
                cfg = server.ConfigStore(path)
                self.assertEqual(expected, cfg.data["vision"]["timeout_ms"])


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

    def test_vision_budget_allows_unlimited_calls_with_fresh_timeout(self):
        events = []
        budget = vision_runtime.VisionBudget(timeout_seconds=180, event=events.append)

        timeouts = [budget.begin(f"request-{index}") for index in range(10)]

        self.assertEqual([180.0] * 10, timeouts)
        self.assertEqual(10, budget.calls)
        self.assertEqual(10, len([event for event in events if event["type"] == "vision_request"]))
        self.assertTrue(all("max_calls" not in event for event in events))

    def test_each_distinct_backend_call_receives_full_timeout(self):
        router = self._router()
        budget = vision_runtime.VisionBudget(timeout_seconds=180)
        parts = [{"type": "image", "data": "same-image"}]
        received = []

        def backend(_profile, _parts, _prompt, _max_tokens, timeout_seconds=None, **_kwargs):
            received.append(timeout_seconds)
            return f"result-{len(received)}"

        with mock.patch.object(router, "_call_backend", side_effect=backend):
            for index in range(10):
                router._budgeted_backend_call({}, parts, f"describe-{index}", budget=budget)

        self.assertEqual([180.0] * 10, received)

    def test_vision_budget_cache_does_not_consume_another_call(self):
        router = self._router()
        budget = vision_runtime.VisionBudget(timeout_seconds=180)
        parts = [{"type": "image", "data": "same-image"}]
        with mock.patch.object(router, "_call_backend", return_value="cached result") as backend:
            first = router._budgeted_backend_call({}, parts, "describe", budget=budget)
            second = router._budgeted_backend_call({}, parts, "describe", budget=budget)

        self.assertEqual("cached result", first)
        self.assertEqual(first, second)
        self.assertEqual(1, budget.calls)
        backend.assert_called_once()

    def test_automatic_vision_failure_degrades_without_budget_abort(self):
        router = self._router(auto_route=True)
        budget = vision_runtime.VisionBudget(timeout_seconds=180)
        with mock.patch.object(router, "describe_parts", side_effect=RuntimeError("backend timeout")):
            out, note = router.prepare_history(
                self._img_history(),
                {"model": "deepseek-chat", "base_url": "https://api.deepseek.com"},
                vision_budget=budget,
            )

        self.assertIn("已自动识图", note)
        self.assertIn("backend timeout", json.dumps(out, ensure_ascii=False))

    def test_vision_budget_key_includes_tool_and_parameters(self):
        parts = [{"type": "image", "data": "same-image"}]
        key = vision_runtime.VisionRouter._budget_key(parts, "describe", "vision_describe", 1024)
        self.assertNotEqual(
            key,
            vision_runtime.VisionRouter._budget_key(parts, "describe", "vision_ocr", 1024),
        )
        self.assertNotEqual(
            key,
            vision_runtime.VisionRouter._budget_key(parts, "describe", "vision_describe", 2048),
        )

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

    def test_uploaded_basename_resolves_to_current_data_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            upload_dir = data_dir / "uploads"
            upload_dir.mkdir()
            image = upload_dir / "naiba_chat_example.png"
            image.write_bytes(b"image")
            app = types.SimpleNamespace(
                config=types.SimpleNamespace(
                    data={"vision": {}},
                    resolve_data_dir=lambda: data_dir,
                )
            )
            router = vision_runtime.VisionRouter(app)

            resolved = router._resolve_paths({"image": image.name})

            self.assertEqual([str(image.resolve())], resolved)

    def test_probe_sends_real_image_and_reports_backend(self):
        router = self._router()
        captured = {}

        def fake_call(profile, image_parts, question, max_tokens=2048, **kwargs):
            captured["profile"] = profile
            captured["image_parts"] = image_parts
            captured["max_tokens"] = max_tokens
            captured["kwargs"] = kwargs
            # 校验：真实 RGB JPEG 图片请求，且确实带图。
            self.assertEqual(image_parts[0]["media_type"], "image/jpeg")
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

    def test_text_probe_sends_no_image(self):
        router = self._router()
        captured = {}

        def fake_call(profile, image_parts, question, **kwargs):
            captured["image_parts"] = image_parts
            captured["question"] = question
            return "OK"

        with mock.patch.object(router, "_call_backend", side_effect=fake_call):
            result = router.probe_text()
        self.assertTrue(result["ok"])
        self.assertEqual("text", result["probe"])
        self.assertEqual([], captured["image_parts"])
        self.assertIn("OK", captured["question"])

    def test_llama_cpp_image_load_error_keeps_detail_and_mmproj_hint(self):
        router = self._router()
        router.vision_backends = lambda _key=None: [{
            "kind": "local", "local_backend": "llama_cpp", "request_format": "llama_cpp",
            "name": "Local llama", "base_url": "http://127.0.0.1:8080/v1", "model": "vision",
        }]
        message = "API request failed HTTP 400: Failed to load image or audio file"
        with mock.patch.object(router, "_call_backend", side_effect=RuntimeError(message)):
            result = router.probe()
        self.assertFalse(result["ok"])
        self.assertEqual("image_load", result["error_kind"])
        self.assertIn("mmproj", result["hint"])
        self.assertIn("HTTP 400", result["reason"])

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

    def test_backend_propagates_cancel_event(self):
        router = self._router()
        cancel_event = threading.Event()
        captured = {}

        def fake_complete(_profile, _messages, options, _status):
            captured.update(options)
            return "OK"

        with mock.patch.object(router._runtime, "complete", side_effect=fake_complete):
            router._call_backend(
                {"model": "vision", "base_url": "https://api.example.com"},
                [{"type": "image", "media_type": "image/png", "data": "x"}],
                "test",
                cancel_event=cancel_event,
            )
        self.assertIs(captured["cancel_event"], cancel_event)

    def test_local_vision_call_does_not_unload_the_model(self):
        router = self._router()
        profile = {
            "kind": "local",
            "request_format": "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen-vl",
        }
        with (
            mock.patch.object(router._runtime, "complete", return_value="OK"),
            mock.patch.object(vision_runtime.ModelRuntime, "unload_local_model") as unload,
        ):
            self.assertEqual(
                "OK",
                router._call_backend(
                    profile,
                    [{"type": "image", "media_type": "image/png", "data": "x"}],
                    "test",
                ),
            )
        unload.assert_not_called()

    def test_probe_image_is_rgb_jpeg(self):
        from PIL import Image
        import base64
        import io

        image = Image.open(io.BytesIO(base64.b64decode(vision_runtime.PROBE_JPEG_B64)))
        self.assertEqual("JPEG", image.format)
        self.assertEqual("RGB", image.mode)


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

    def test_switching_legacy_openai_llama_endpoint_to_local_keeps_v1_protocol(self):
        app_js = (Path(__file__).parents[1] / "public" / "app.js").read_text(encoding="utf-8")
        self.assertIn("previousFormat === 'openai_chat' ? 'llama_cpp'", app_js)

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
