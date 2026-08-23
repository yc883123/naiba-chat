from __future__ import annotations

import gc
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import model_runtime
from async_tasks import ConversationRunManager
from model_runtime import ModelRuntime
from skill_runtime import SkillAgent, normalize_skill_policy
from storage import ChatStorage
from subagent import job_tool_handler_factory, subagent_handler_factory


class RunStorageTests(unittest.TestCase):
    def make_storage(self, root: str) -> ChatStorage:
        return ChatStorage(Path(root) / "chat.db")

    def test_chat_run_creation_is_atomic_and_freezes_history(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = self.make_storage(root)
            conversation = storage.create_conversation()
            storage.add_message(conversation["id"], "assistant", "earlier", {})
            run, history = storage.create_chat_run(
                conversation["id"],
                "new request",
                [{"name": "image.png", "path": "workspace/image.png", "size": 12}],
                {"id": "agent", "name": "Agent"},
                {"interaction_mode": "craft", "marker": "frozen"},
                "craft",
            )

            self.assertEqual("queued", run["status"])
            self.assertEqual("new request", history[-1]["content"])
            self.assertEqual(run["id"], history[-1]["metadata"]["run_id"])
            frozen = storage.get_run_snapshot(run["id"])
            self.assertEqual("frozen", frozen["marker"])
            self.assertEqual(history, frozen["conversation_messages"])

            storage.add_message(conversation["id"], "assistant", "later", {})
            self.assertEqual(history, storage.get_run_snapshot(run["id"])["conversation_messages"])
            del storage
            gc.collect()

    def test_one_active_run_per_conversation_but_other_conversations_can_run(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = self.make_storage(root)
            first = storage.create_conversation()
            second = storage.create_conversation()
            run = storage.create_run(first["id"], "one", {}, {})

            with self.assertRaisesRegex(RuntimeError, f"ACTIVE_RUN:{run['id']}"):
                storage.create_run(first["id"], "two", {}, {})

            other = storage.create_run(second["id"], "parallel", {}, {})
            self.assertEqual(second["id"], other["conversation_id"])
            self.assertEqual(2, len(storage.list_background_tasks(active_only=True)))
            del storage
            gc.collect()

    def test_run_events_are_persistent_and_replay_from_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = self.make_storage(root)
            conversation = storage.create_conversation()
            run = storage.create_run(conversation["id"], "event test", {}, {})
            first = storage.append_run_event(run["id"], {"type": "status", "message": "start"})
            second = storage.append_run_event(run["id"], {"type": "delta", "content": "hello"})

            self.assertEqual(1, first["sequence"])
            self.assertEqual(2, second["sequence"])
            self.assertEqual([second], storage.list_run_events(run["id"], after=1))
            del storage
            gc.collect()

    def test_clear_terminal_tasks_preserves_active_runs(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = self.make_storage(root)
            conversation = storage.create_conversation()
            completed = storage.create_run(conversation["id"], "completed", {}, {})
            storage.update_background_task(completed["id"], status="completed", finished=True)
            active = storage.create_run(conversation["id"], "active", {}, {})

            self.assertEqual(1, storage.clear_terminal_background_tasks())
            self.assertIsNone(storage.get_background_task(completed["id"]))
            self.assertIsNotNone(storage.get_background_task(active["id"]))
            del storage
            gc.collect()

    def test_clear_conversation_messages_retains_settings(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = self.make_storage(root)
            conversation = storage.create_conversation()
            storage.update_conversation_settings(
                conversation["id"], title="保留的标题", model_key="local:llama"
            )
            storage.add_message(conversation["id"], "user", "remove me", {})
            storage.log_tool_run(conversation["id"], "vision_describe", {}, "result", True)

            self.assertEqual(1, storage.clear_conversation_messages(conversation["id"]))
            cleared = storage.get_conversation(conversation["id"])
            self.assertEqual([], cleared["messages"])
            self.assertEqual("保留的标题", cleared["title"])
            self.assertTrue(cleared["title_customized"])
            self.assertEqual("local:llama", cleared["model_key"])
            del storage
            gc.collect()

    def test_restart_marks_active_run_interrupted_and_appends_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = self.make_storage(root)
            conversation = storage.create_conversation()
            run = storage.create_run(conversation["id"], "interrupted", {}, {})
            storage.append_run_event(run["id"], {"type": "status", "message": "running"})
            del storage
            gc.collect()

            reopened = self.make_storage(root)
            recovered = reopened.get_background_task(run["id"])
            events = reopened.list_run_events(run["id"])
            self.assertEqual("interrupted", recovered["status"])
            self.assertIn("服务重启", recovered["error"])
            self.assertEqual("error", events[-1]["type"])
            self.assertIn("服务重启", events[-1]["message"])
            del reopened
            gc.collect()

    def test_confirmation_must_belong_to_waiting_run(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = self.make_storage(root)
            conversation = storage.create_conversation()
            run = storage.create_run(conversation["id"], "confirm", {}, {})
            storage.update_background_task(
                run["id"], status="waiting", detail={"confirm_id": "confirm-1"}
            )
            manager = ConversationRunManager(SimpleNamespace(storage=storage))

            self.assertTrue(manager.owns_confirmation(run["id"], "confirm-1"))
            self.assertFalse(manager.owns_confirmation(run["id"], "other"))
            storage.update_background_task(run["id"], status="running")
            self.assertFalse(manager.owns_confirmation(run["id"], "confirm-1"))
            del manager
            del storage
            gc.collect()

    def test_pending_run_guidance_is_not_consumed_until_guided(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            storage = self.make_storage(root)
            conversation = storage.create_conversation()
            run = storage.create_run(conversation["id"], "work", {}, {})
            pending = storage.add_run_interjection(
                conversation["id"], run["id"], "change the direction"
            )

            self.assertFalse(pending["metadata"]["interjection_guided"])
            self.assertEqual([], storage.list_run_interjections(run["id"]))
            self.assertTrue(storage.delete_run_interjection(
                conversation["id"], run["id"], pending["id"]
            ))

            guided = storage.add_run_interjection(
                conversation["id"], run["id"], "use a different ending"
            )
            guided = storage.guide_run_interjection(conversation["id"], run["id"], guided["id"])
            self.assertTrue(guided["metadata"]["interjection_guided"])
            self.assertEqual([guided["id"]], [
                item["id"] for item in storage.list_run_interjections(run["id"])
            ])
            with self.assertRaisesRegex(LookupError, "已被处理"):
                storage.guide_run_interjection(conversation["id"], run["id"], guided["id"])
            self.assertFalse(storage.delete_run_interjection(
                conversation["id"], run["id"], guided["id"]
            ))
            storage.mark_run_interjections_consumed(run["id"], [guided["id"]])
            self.assertEqual([], storage.list_run_interjections(run["id"]))
            del storage
            gc.collect()


class SkillPolicyTests(unittest.TestCase):
    skills = [{"id": "a"}, {"id": "b"}, {"id": "fixed"}]

    def test_missing_policy_defaults_to_auto_and_keeps_agent_fixed_skills(self) -> None:
        self.assertEqual(
            {"mode": "auto", "skill_ids": ["fixed"]},
            normalize_skill_policy(None, fixed_ids=["fixed"], catalog=self.skills),
        )

    def test_legacy_selection_migrates_to_pinned(self) -> None:
        self.assertEqual(
            {"mode": "pinned", "skill_ids": ["fixed", "a"]},
            normalize_skill_policy(
                None, legacy_auto=True, legacy_ids=["a"], fixed_ids=["fixed"], catalog=self.skills
            ),
        )

    def test_exclusive_accepts_multiple_and_ignores_agent_fixed_skills(self) -> None:
        self.assertEqual(
            {"mode": "exclusive", "skill_ids": ["a", "b"]},
            normalize_skill_policy(
                {"mode": "exclusive", "skill_ids": ["a", "b"]},
                fixed_ids=["fixed"],
                catalog=self.skills,
            ),
        )

    def test_exclusive_rejects_empty_and_unknown_skills(self) -> None:
        with self.assertRaisesRegex(ValueError, "至少需要"):
            normalize_skill_policy({"mode": "exclusive", "skill_ids": []}, catalog=self.skills)
        with self.assertRaisesRegex(ValueError, "未知 Skill"):
            normalize_skill_policy({"mode": "exclusive", "skill_ids": ["missing"]}, catalog=self.skills)

    def test_exclusive_loads_all_selected_skills_without_auto_routing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            paths = []
            skills = []
            for skill_id in ("a", "b"):
                path = Path(root) / f"{skill_id}.md"
                path.write_text(f"# {skill_id}", encoding="utf-8")
                paths.append(path)
                skills.append({
                    "id": skill_id,
                    "name": skill_id,
                    "description": skill_id,
                    "path": str(path),
                    "root": root,
                    "requires_mcp": False,
                })

            catalog = SimpleNamespace(scan=lambda: skills)
            mcp = SimpleNamespace(connections={}, acquire=lambda: None, release=lambda: None)
            executor = SimpleNamespace(mcp_registry=mcp, mcp_tool_guide=lambda: "")
            events = []
            agent = SkillAgent(catalog, executor, lambda *_args: "done")
            with patch.object(agent, "_route_skills", side_effect=AssertionError("must not route")):
                content, _runs, _reasoning, _usage = agent.run(
                    "work", [], {}, {},
                    {"mode": "exclusive", "skill_ids": ["a", "b"]}, [], "", [],
                    lambda event: events.append(event), lambda *_args: None,
                    run_context={},
                )

            enabled = next(event["skills"] for event in events if event.get("type") == "skills")
            self.assertEqual("done", content)
            self.assertEqual(["a", "b"], [skill["id"] for skill in enabled])

    def test_frozen_auto_policy_keeps_agent_fixed_skill(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "fixed.md"
            path.write_text("# fixed", encoding="utf-8")
            skill = {
                "id": "fixed", "name": "fixed", "description": "fixed",
                "path": str(path), "root": root, "requires_mcp": False,
            }
            catalog = SimpleNamespace(scan=lambda: [skill])
            mcp = SimpleNamespace(connections={}, acquire=lambda: None, release=lambda: None)
            executor = SimpleNamespace(mcp_registry=mcp, mcp_tool_guide=lambda: "")
            events = []
            agent = SkillAgent(catalog, executor, lambda *_args: "done")

            content, _runs, _reasoning, _usage = agent.run(
                "work", [], {}, {}, {"mode": "auto", "skill_ids": ["fixed"]}, [], "", [],
                lambda event: events.append(event), lambda *_args: None, run_context={},
            )

            enabled = next(event["skills"] for event in events if event.get("type") == "skills")
            self.assertEqual("done", content)
            self.assertEqual(["fixed"], [item["id"] for item in enabled])

    def test_plain_auto_chat_does_not_spend_a_skill_router_request(self) -> None:
        catalog = SimpleNamespace(scan=lambda: [{
            "id": "video", "name": "video-maker", "description": "Generate videos",
            "path": __file__, "root": str(Path(__file__).parent), "requires_mcp": False,
        }])
        mcp = SimpleNamespace(connections={}, acquire=lambda: None, release=lambda: None)
        executor = SimpleNamespace(mcp_registry=mcp, mcp_tool_guide=lambda: "")
        calls = []

        def complete(_profile, _messages, _options, _event):
            calls.append(1)
            return "done"

        agent = SkillAgent(catalog, executor, complete)
        with patch.object(agent, "_route_skills", side_effect=AssertionError("legacy router must not run")):
            content, _runs, _reasoning, usage = agent.run(
                "用一句话描述图片", [], {}, {}, {"mode": "auto", "skill_ids": []},
                [], "", ["capability_inventory", "activate_skill"],
                lambda _event: None, lambda *_args: None, run_context={},
            )

        self.assertEqual("done", content)
        self.assertEqual(1, len(calls))
        self.assertEqual({}, usage)

    def test_progressive_tool_visibility_keeps_plain_chat_compact(self) -> None:
        allowed = {
            "read_file", "write_file", "run_command", "web_search",
            "capability_inventory", "activate_skill", "install_skill",
            "run_in_background", "subagent",
        }
        schemas = [{"name": name, "description": name, "parameters": {}} for name in allowed]

        plain = SkillAgent._visible_tool_names("用一句话描述图片", allowed, schemas, [])
        coding = SkillAgent._visible_tool_names("读取并修改这个代码文件", allowed, schemas, [])

        self.assertEqual({"capability_inventory", "activate_skill"}, plain)
        self.assertIn("read_file", coding)
        self.assertIn("write_file", coding)
        self.assertNotIn("run_in_background", coding)


class NativeToolAndMcpTests(unittest.TestCase):
    def test_openai_request_contains_native_tool_schema(self) -> None:
        captured = {}

        class Response:
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"done"}}]}'

        def fake_urlopen(request, timeout):
            del timeout
            captured.update(json.loads(request.data.decode("utf-8")))
            return Response()

        with patch.object(model_runtime.urllib.request, "urlopen", fake_urlopen):
            content, _reasoning, _usage = ModelRuntime._complete_online(
                {"base_url": "https://example.test", "model": "m", "request_format": "openai_chat"},
                [{"role": "user", "content": "work"}],
                {
                    "stream": False,
                    "tools": [{
                        "name": "read_file",
                        "description": "read",
                        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                    }],
                },
            )

        self.assertEqual("done", content)
        self.assertEqual("read_file", captured["tools"][0]["function"]["name"])
        self.assertEqual("auto", captured["tool_choice"])
        self.assertFalse(captured["parallel_tool_calls"])

    def test_provider_native_tool_schema_shapes(self) -> None:
        tool = [{
            "name": "read_file",
            "description": "read",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }]

        codex = ModelRuntime._tool_schemas(tool, "codex_responses")[0]
        gemini = ModelRuntime._tool_schemas(tool, "gemini")[0]
        claude = ModelRuntime._tool_schemas(tool, "claude")[0]

        self.assertEqual("read_file", codex["name"])
        self.assertEqual("read_file", gemini["name"])
        self.assertEqual(tool[0]["parameters"], gemini["parameters"])
        self.assertEqual(tool[0]["parameters"], claude["input_schema"])

    def test_provider_native_tool_results_are_serialized(self) -> None:
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call-1", "name": "read_file", "arguments": {"path": "a"}
            }]},
            {"role": "tool", "tool_call_id": "call-1", "name": "read_file", "content": '{"ok":true}'},
        ]

        openai = ModelRuntime._openai_messages(messages)
        codex = ModelRuntime._responses_input(messages)
        gemini = [ModelRuntime._gemini_message(item) for item in messages]
        claude = [ModelRuntime._claude_message(item) for item in messages]
        ollama = ModelRuntime._ollama_messages(messages)

        self.assertEqual("call-1", openai[1]["tool_call_id"])
        self.assertEqual("function_call_output", codex[1]["type"])
        self.assertEqual("read_file", gemini[1]["parts"][0]["functionResponse"]["name"])
        self.assertEqual("tool_result", claude[1]["content"][0]["type"])
        self.assertEqual("tool", ollama[1]["role"])

    def test_codex_gemini_and_claude_payloads_receive_native_tools(self) -> None:
        payloads = {}
        urls = {}
        responses = {
            "codex_responses": {"output_text": "done"},
            "gemini": {"candidates": [{"content": {"parts": [{"text": "done"}]}}]},
            "claude": {"content": [{"type": "text", "text": "done"}]},
            "lm_studio": {"choices": [{"message": {"content": "done"}}]},
        }

        class Response:
            headers = {"Content-Type": "application/json"}

            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(self.body).encode("utf-8")

        current = {"format": ""}

        def fake_urlopen(request, timeout, cancel_event=None):
            del timeout, cancel_event
            payloads[current["format"]] = json.loads(request.data.decode("utf-8"))
            urls[current["format"]] = request.full_url
            return Response(responses[current["format"]])

        tool = [{
            "name": "read_file",
            "description": "read",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }]
        with patch.object(ModelRuntime, "_urlopen_cancelable", side_effect=fake_urlopen):
            for request_format in responses:
                current["format"] = request_format
                ModelRuntime._complete_online(
                    {"base_url": "https://example.test", "model": "m", "request_format": request_format},
                    [{"role": "user", "content": "work"}],
                    {"stream": False, "tools": tool},
                )

        self.assertEqual("read_file", payloads["codex_responses"]["tools"][0]["name"])
        self.assertEqual(
            "read_file",
            payloads["gemini"]["tools"][0]["functionDeclarations"][0]["name"],
        )
        self.assertEqual("read_file", payloads["claude"]["tools"][0]["name"])
        self.assertEqual("read_file", payloads["lm_studio"]["tools"][0]["function"]["name"])
        self.assertTrue(urls["lm_studio"].endswith("/v1/chat/completions"))

    def test_codex_gemini_and_claude_native_calls_become_agent_actions(self) -> None:
        codex, _ = ModelRuntime._online_response("codex_responses", {
            "output": [{"type": "function_call", "name": "read_file", "arguments": '{"path":"a"}'}]
        })
        gemini, _ = ModelRuntime._online_response("gemini", {
            "candidates": [{"content": {"parts": [{"functionCall": {"name": "read_file", "args": {"path": "b"}}}]}}]
        })
        claude, _ = ModelRuntime._online_response("claude", {
            "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "c"}}]
        })

        self.assertEqual("a", json.loads(codex)["arguments"]["path"])
        self.assertEqual("b", json.loads(gemini)["arguments"]["path"])
        self.assertEqual("c", json.loads(claude)["arguments"]["path"])

    def test_multiple_native_calls_are_preserved_for_the_agent_loop(self) -> None:
        result = {
            "choices": [{"message": {"tool_calls": [
                {"function": {"name": "read_file", "arguments": '{"path":"a"}'}},
                {"function": {"name": "read_file", "arguments": '{"path":"b"}'}},
            ]}}]
        }

        content, _ = ModelRuntime._online_response("openai_chat", result)
        action = json.loads(content)

        self.assertEqual("tools", action["type"])
        self.assertEqual(["a", "b"], [call["arguments"]["path"] for call in action["calls"]])

    def test_openai_compatible_proxy_can_fall_back_when_tools_are_rejected(self) -> None:
        import io
        calls = []

        class Response:
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"done"}}]}'

        def fake_urlopen(request, timeout, cancel_event=None):
            del timeout, cancel_event
            payload = json.loads(request.data.decode("utf-8"))
            calls.append(payload)
            if len(calls) == 1:
                raise model_runtime.urllib.error.HTTPError(
                    request.full_url,
                    400,
                    "Bad Request",
                    {"Content-Type": "application/json"},
                    io.BytesIO(b'{"error":"unsupported tools field"}'),
                )
            return Response()

        with patch.object(ModelRuntime, "_urlopen_cancelable", side_effect=fake_urlopen):
            content, _reasoning, _usage = ModelRuntime._complete_online(
                {"base_url": "https://proxy.test", "model": "deepseek-chat", "request_format": "openai_chat"},
                [{"role": "user", "content": "work"}],
                {"stream": False, "tools": [{"name": "read_file", "parameters": {"type": "object"}}]},
            )

        self.assertEqual("done", content)
        self.assertIn("tools", calls[0])
        self.assertNotIn("tools", calls[1])

    def test_direct_mcp_tools_are_not_discovered_from_registry(self) -> None:
        registry = SimpleNamespace(
            names=lambda: ["mcp__demo__read", "mcp__demo__write"],
            readonly_mcp_tools=lambda: ["mcp__demo__read"],
        )
        app = SimpleNamespace(
            config=SimpleNamespace(data={"agent_tools": ["call_mcp"]}),
            tool_registry=registry,
            web_search=SimpleNamespace(is_available=lambda: False),
        )
        manager = ConversationRunManager(app)
        agent = {"tool_scope": ["call_mcp"]}

        craft = manager._resolve_allowed_tools("craft", agent, False)
        plan = manager._resolve_allowed_tools("plan", agent, False)

        self.assertNotIn("mcp__demo__read", craft)
        self.assertNotIn("mcp__demo__write", craft)
        self.assertNotIn("mcp__demo__read", plan)
        self.assertNotIn("mcp__demo__write", plan)

    def test_agent_continues_with_native_tool_result_context(self) -> None:
        calls = []

        def complete(_profile, messages, options, _event):
            calls.append((messages, options))
            if len(calls) == 1:
                return json.dumps({"type": "tool", "tool": "read_file", "arguments": {"path": "a"}})
            self.assertEqual("tool", messages[-1]["role"])
            self.assertEqual("read_file", messages[-1]["name"])
            return "done"

        registry = SimpleNamespace(
            schemas=lambda: [{
                "name": "read_file", "description": "read",
                "parameters": {"type": "object", "properties": {}},
            }],
            execute=lambda tool, arguments, active, context: (True, "file content"),
            retryable=lambda tool: False,
        )
        mcp = SimpleNamespace(connections={}, acquire=lambda: None, release=lambda: None)
        executor = SimpleNamespace(mcp_registry=mcp, mcp_tool_guide=lambda: "")
        agent = SkillAgent(SimpleNamespace(scan=lambda: []), executor, complete)

        content, runs, _reasoning, _usage = agent.run(
            "work", [], {}, {}, {"mode": "auto", "skill_ids": []}, [], "",
            ["read_file"], lambda _event: None, lambda *_args: None,
            tool_registry=registry, run_context={},
        )

        self.assertEqual("done", content)
        self.assertEqual(1, len(runs))
        self.assertEqual("read_file", calls[0][1]["tools"][0]["name"])


class ChildPolicyInheritanceTests(unittest.TestCase):
    class Jobs:
        def __init__(self):
            self.specs = []

        def list(self, owner=""):
            del owner
            return []

        def start(self, spec, owner=""):
            del owner
            self.specs.append(spec)
            return "job-1"

    def test_subagent_inherits_skill_policy_and_cannot_expand_tools(self) -> None:
        jobs = self.Jobs()
        app = SimpleNamespace(jobs=jobs)
        handler = subagent_handler_factory(app)
        context = {
            "conversation_id": "conversation",
            "run_id": "run",
            "owner_session_id": "conversation",
            "depth": 0,
            "allowed_tools": ["read_file", "write_file", "subagent"],
            "skill_policy": {"mode": "exclusive", "skill_ids": ["a", "b"]},
        }

        ok, _ = handler(
            {"instruction": "work", "allowed_tools": ["read_file", "run_command"]},
            [], context,
        )

        self.assertTrue(ok)
        self.assertEqual(["read_file"], jobs.specs[0].params["allowed_tools"])
        self.assertEqual(context["skill_policy"], jobs.specs[0].params["skill_policy"])

    def test_background_job_inherits_frozen_policy_and_tools(self) -> None:
        jobs = self.Jobs()
        handlers = job_tool_handler_factory(SimpleNamespace(jobs=jobs))
        context = {
            "conversation_id": "conversation",
            "run_id": "run",
            "owner_session_id": "conversation",
            "allowed_tools": ["read_file", "job_wait"],
            "skill_policy": {"mode": "exclusive", "skill_ids": ["a", "b"]},
        }

        ok, _ = handlers["run_in_background"](
            {"spec": {"kind": "shell", "params": {"command": "echo ok"}}}, [], context
        )

        self.assertTrue(ok)
        self.assertEqual(context["allowed_tools"], jobs.specs[0].params["allowed_tools"])
        self.assertEqual(context["skill_policy"], jobs.specs[0].params["skill_policy"])


class RunFrontendTests(unittest.TestCase):
    def test_frontend_detaches_and_resumes_conversation_owned_runs(self) -> None:
        source = Path("public/app.js").read_text(encoding="utf-8")

        self.assertIn("function detachRunSubscription()", source)
        self.assertIn("async function resumeConversationRun(conversationId)", source)
        self.assertIn("/api/runs?conversation_id=", source)
        self.assertIn("/events?after=${state.runSequence}", source)
        self.assertIn("body: { run_id: runId, confirm_id: confirmId }", source)
        self.assertNotIn("data-cancel-task", source)
        self.assertNotIn("data-confirm-task", source)
        self.assertNotIn("data-reject-task", source)

    def test_frontend_does_not_render_available_tool_schemas(self) -> None:
        source = Path("public/app.js").read_text(encoding="utf-8")

        self.assertNotIn("${toolAvailabilityMarkup(metadata.allowed_tools)}", source)
        self.assertNotIn("wrapper.innerHTML = toolAvailabilityMarkup(event.tools || [])", source)

    def test_frontend_defaults_to_auto_and_sends_skill_policy(self) -> None:
        source = Path("public/app.js").read_text(encoding="utf-8")
        html = Path("public/index.html").read_text(encoding="utf-8")

        self.assertIn("const initialSkillMode", source)
        self.assertIn("skill_policy: {", source)
        self.assertIn("mode: state.skillMode", source)
        self.assertIn('value="exclusive"', html)
        self.assertNotIn("completion_contract", source)

    def test_frontend_clears_provisional_output_and_empty_thinking_blocks(self) -> None:
        source = Path("public/app.js").read_text(encoding="utf-8")

        self.assertIn("function clearStreamingAnswer(answer)", source)
        self.assertIn("clearStreamingAnswer(answer);", source)
        self.assertIn("if (!String(event.content || '').trim()) return;", source)
        self.assertIn("if (!(block.querySelector('.reasoning-content')?.dataset.raw || '').trim()) block.remove();", source)

    def test_frontend_includes_task_and_conversation_cleanup_controls(self) -> None:
        source = Path("public/app.js").read_text(encoding="utf-8")
        html = Path("public/index.html").read_text(encoding="utf-8")

        self.assertIn("clearTerminalTasks", source)
        self.assertIn("clearConversationMessages", source)
        self.assertIn("/api/tasks/clear", source)
        self.assertIn("clearTerminalTasks", html)
        self.assertIn("clearConversationMessages", html)


if __name__ == "__main__":
    unittest.main()
