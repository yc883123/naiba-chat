# -*- coding: utf-8 -*-
"""护栏：reset_context（模型主动重置上下文）与「新会话」种子消息。

语义：模型先把交接文档写进工作区，再调用 reset_context(handoff_path)——
1. 工具侧校验「路径非空 + 绝对 + 文件存在 + 非空」，任一不满足即拒绝且上下文不动；
2. 通过后置位 ``run_context["context_reset"]``：本轮立即收尾，收尾路径把标记写到
   本条 AI 回复的 metadata（`session_start`），下一条消息起只从分割线之后重算上下文；
3. 前端把种子消息（可配置模板）填进输入框，由用户确认后发送。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.tools.providers.core import (  # noqa: E402
    CoreToolProvider,
    ToolContext,
    _reset_context_ok,
    _tool_reset_context,
)


def _context(workspace: Path, tasks=None) -> ToolContext:
    extra = {}
    if tasks is not None:
        extra["active_background_tasks"] = tasks
    return ToolContext(
        workspace=workspace,
        python_executable=sys.executable,
        command_timeout=120,
        mcp_registry=None,
        extra=extra,
    )


class ResetContextToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.handoff = self.root / "交接.md"
        self.handoff.write_text("任务目标：…\n已完成：…\n待办：…", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _call(self, args, ctx=None, run_context=None):
        return _tool_reset_context(ctx or _context(self.root), args,
                                   [], run_context if run_context is not None else {})

    def test_valid_handoff_sets_request_and_returns_ok(self):
        run_context = {}
        payload = json.loads(self._call({"handoff_path": str(self.handoff), "note": "第一阶段完成"}, run_context=run_context))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["handoff_path"], str(self.handoff.resolve()))
        info = run_context["context_reset"]
        self.assertEqual(info["source"], "tool")
        self.assertEqual(info["handoff_path"], str(self.handoff.resolve()))
        self.assertEqual(info["note"], "第一阶段完成")
        self.assertTrue(info["at"])
        self.assertEqual(info["tasks"], [])

    def test_rejects_missing_relative_missing_file_and_empty_file(self):
        empty = self.root / "空.md"
        empty.write_text("", encoding="utf-8")
        cases = {
            "空路径": {"handoff_path": "   "},
            "相对路径": {"handoff_path": "交接.md"},
            "文件不存在": {"handoff_path": str(self.root / "没有这个文件.md")},
            "文件为空": {"handoff_path": str(empty)},
        }
        for label, args in cases.items():
            with self.subTest(case=label):
                run_context = {}
                payload = json.loads(self._call(args, run_context=run_context))
                self.assertFalse(payload["ok"], f"{label} 必须被拒绝")
                self.assertTrue(payload["error"])
                self.assertNotIn("context_reset", run_context, f"{label} 被拒时不得置位重置请求")

    def test_directory_is_rejected(self):
        payload = json.loads(self._call({"handoff_path": str(self.root)}))
        self.assertFalse(payload["ok"])

    def test_second_call_in_same_turn_rejected(self):
        run_context = {}
        first = json.loads(self._call({"handoff_path": str(self.handoff)}, run_context=run_context))
        second = json.loads(self._call({"handoff_path": str(self.handoff)}, run_context=run_context))
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertIn("重复", second["error"])

    def test_background_tasks_snapshot_in_marker(self):
        def getter(conversation_id):
            self.assertEqual(conversation_id, "conv-1")
            return [
                {"id": "job_1", "kind": "job", "status": "running", "message": "渲染第 3 批"},
                {"id": "job_2", "kind": "run", "status": "queued", "message": "  等  待  中  "},
            ]

        run_context = {"conversation_id": "conv-1"}
        payload = json.loads(self._call({"handoff_path": str(self.handoff)},
                                        ctx=_context(self.root, tasks=getter), run_context=run_context))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["background_tasks"], 2)
        tasks = run_context["context_reset"]["tasks"]
        self.assertEqual([task["id"] for task in tasks], ["job_1", "job_2"])
        self.assertEqual(tasks[1]["title"], "等 待 中", "任务标题要去多余空白")

    def test_task_snapshot_failure_does_not_block_reset(self):
        def boom(_conversation_id):
            raise RuntimeError("storage 挂了")

        run_context = {"conversation_id": "conv-1"}
        payload = json.loads(self._call({"handoff_path": str(self.handoff)},
                                        ctx=_context(self.root, tasks=boom), run_context=run_context))
        self.assertTrue(payload["ok"], "后台任务快照失败不能阻断重置")
        self.assertEqual(run_context["context_reset"]["tasks"], [])

    def test_current_run_is_not_counted_as_background_task(self):
        """用户实测：把发起重置的这一轮 chat run 自己也列进了"仍在运行的后台任务"。"""
        def getter(_conversation_id):
            return [
                {"id": "run-self", "kind": "chat", "status": "running", "message": "你现在写一份交接报告"},
                {"id": "job-child", "kind": "job", "status": "running", "message": "渲染第 3 批"},
            ]

        run_context = {"conversation_id": "conv-1", "run_id": "run-self"}
        payload = json.loads(self._call({"handoff_path": str(self.handoff)},
                                        ctx=_context(self.root, tasks=getter), run_context=run_context))
        self.assertTrue(payload["ok"])
        tasks = run_context["context_reset"]["tasks"]
        self.assertEqual([task["id"] for task in tasks], ["job-child"],
                         "发起重置的这一轮自己不能算作后台任务")
        self.assertEqual(payload["background_tasks"], 1)

    def test_subagent_job_excludes_its_own_job_id(self):
        def getter(_conversation_id):
            return [
                {"id": "job-self", "kind": "job", "status": "running", "message": "当前子任务"},
                {"id": "job-other", "kind": "job", "status": "running", "message": "别的任务"},
            ]

        run_context = {"conversation_id": "conv-1", "run_id": "run-parent", "job_id": "job-self"}
        payload = json.loads(self._call({"handoff_path": str(self.handoff)},
                                        ctx=_context(self.root, tasks=getter), run_context=run_context))
        self.assertTrue(payload["ok"])
        self.assertEqual([task["id"] for task in run_context["context_reset"]["tasks"]], ["job-other"])

    def test_result_success_uses_ok_field(self):
        self.assertTrue(_reset_context_ok('{"ok": true}'))
        self.assertFalse(_reset_context_ok('{"ok": false, "error": "x"}'))
        self.assertFalse(_reset_context_ok("不是 JSON"))


class ResetContextWiringTests(unittest.TestCase):
    """注册表 / Provider 绑定 / 权限策略 / 系统提示 / 收尾落标记。"""

    def test_registry_declares_tool_and_media(self):
        from naiba.tools.registry import MEDIA_DECLARATIONS, build_tool_registry

        registry = build_tool_registry()
        spec = registry.get("reset_context")
        self.assertIsNotNone(spec)
        self.assertTrue(spec.side_effect, "重置上下文属于有副作用操作")
        self.assertFalse(spec.retryable)
        self.assertIn("reset_context", MEDIA_DECLARATIONS, "装配期要求每个内置工具都有媒体声明")
        self.assertEqual(MEDIA_DECLARATIONS["reset_context"]["extract"], "none")
        self.assertIn("handoff_path", spec.parameters["required"])
        self.assertLessEqual(len(spec.description), 100, "工具描述必须保持一行钩子")
        from naiba.config import tool_catalog_entries

        catalog = tool_catalog_entries(registry.schemas())
        group = next(row for row in catalog if row["name"] == "reset_context")["group"]
        self.assertEqual(group, "长会话")

    def test_provider_binds_custom_execute_and_confirm_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = CoreToolProvider(_context(Path(tmp)))
            spec = next(item for item in provider.tools() if item.name == "reset_context")
        self.assertIsNotNone(spec.execute)
        self.assertIsNotNone(spec.policy)
        self.assertEqual(spec.policy("reset_context", {}, [], "confirm", None), "重置会话上下文")
        self.assertEqual(spec.policy("reset_context", {}, [], "auto", None), "", "auto 模式免确认")
        # 执行器把 ok=false 判为失败（模型不会把被拒当成功）
        success, result = spec.execute({"handoff_path": "相对路径.md"}, [], {})
        self.assertFalse(success)
        self.assertFalse(json.loads(result)["ok"])

    def test_agent_loop_ends_turn_on_reset(self):
        source = (ROOT / "naiba" / "skills" / "agent.py").read_text(encoding="utf-8")
        start = source.index('reset_info = (run_context or {}).get("context_reset")')
        block = source[start:start + 1400]
        self.assertIn('run_context["trace_messages"] = messages[trace_start:]', block,
                      "收尾必须把 trace 定稿（含最终确认语）")
        self.assertIn('event({"type": "status"', block, "要告诉用户上下文已重置")
        self.assertIn("return content, runs, reasonings", block, "成功后本轮立即结束")

    def test_run_chat_writes_marker_into_message_metadata(self):
        source = (ROOT / "naiba" / "run" / "chat.py").read_text(encoding="utf-8")
        block = source[source.index('reset_info = (run_context or {}).get("context_reset")'):]
        block = block[: block.index("\n", block.index("metadata[MetadataKeys.SESSION_START]"))]
        self.assertIn("metadata[MetadataKeys.SESSION_START] = dict(reset_info)", block,
                      "标记必须落到本条 AI 回复的 metadata 上（前端据此画分割线）")

    def test_system_prompt_guide_is_tool_gated(self):
        source = (ROOT / "naiba" / "run" / "chat.py").read_text(encoding="utf-8")
        self.assertIn('if "reset_context" in allowed_tools:', source,
                      "重置规则只在工具集含 reset_context 时注入（与 web_search/PDF 同口径）")
        block = source[source.index('if "reset_context" in allowed_tools:'):]
        block = block[: block.index(".strip()")]
        self.assertIn("交接文档", block)
        self.assertIn("handoff_path", block)
        self.assertIn("本轮立即结束", block)


class SeedTemplateTests(unittest.TestCase):
    """种子消息模板：默认值、可配置、前端渲染契约。"""

    def test_config_default_and_update(self):
        from server import ConfigStore

        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            default = store.data["context_reset_seed_template"]
            self.assertIn("{handoff_path}", default)
            self.assertIn("{task_list}", default)
            self.assertEqual(store.public()["context_reset_seed_template"], default)
            store.update_settings({"context_reset_seed_template": "自定义 {handoff_path}"})
            self.assertEqual(store.data["context_reset_seed_template"], "自定义 {handoff_path}")
            self.assertEqual(ConfigStore(Path(tmp) / "config.json").data["context_reset_seed_template"],
                             "自定义 {handoff_path}", "必须持久化")
            store.update_settings({"context_reset_seed_template": "x" * 5000})
            self.assertEqual(len(store.data["context_reset_seed_template"]), 2000, "模板长度上限 2000")

    def test_frontend_contract(self):
        js = (ROOT / "public" / "js" / "04-messages.js").read_text(encoding="utf-8")
        for snippet in (
            "export function contextResetSeedText(info = {})",
            "export function fillContextResetSeed(info = {})",
            "DEFAULT_CONTEXT_RESET_SEED",
            "data-fill-reset-seed",
            "{handoff_path}",
            "{task_count}",
            "{task_list}",
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, js)
        # 无后台任务时含占位符的整行去掉；填种子不自动发送
        seed = js[js.index("export function contextResetSeedText"):]
        seed = seed[: seed.index("\n}")]
        self.assertIn("filter((line) => !(", seed)
        fill = js[js.index("export function fillContextResetSeed"):]
        fill = fill[: fill.index("\n}")]
        self.assertNotIn("sendMessage(", fill, "只填输入框，不自动发送")
        self.assertIn("notifyComposerChanged(input)", fill)
        html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('id="contextResetSeedTemplate"'), 1, "运行设置里要有模板输入框")
        settings = (ROOT / "public" / "js" / "09-settings.js").read_text(encoding="utf-8")
        self.assertIn("context_reset_seed_template", settings, "设置页要回填与提交模板")
        binds = (ROOT / "public" / "js" / "15-bind-events.js").read_text(encoding="utf-8")
        self.assertIn("data-fill-reset-seed", binds)
        chat = (ROOT / "public" / "js" / "12-chat-input.js").read_text(encoding="utf-8")
        self.assertIn("fillContextResetSeed(resetInfo)", chat, "模型重置后自动预填种子消息")
        css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".session-divider-seed", css)


if __name__ == "__main__":
    unittest.main()
