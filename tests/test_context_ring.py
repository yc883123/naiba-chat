# -*- coding: utf-8 -*-
"""守门：上下文圆环逐请求刷新 + 达阈值弹窗提醒。

保护对象：
1. `public/js/12-chat-input.js::handleUsageEvent`——流式 usage 事件（每完成一次模型请求
   发射一次）必须立即刷新圆环；
2. `public/js/03-media.js::setContextUsage`——`state.contextUsage` 的唯一写入点，
   历史渲染与终态（done/error/取消）路径统一经 `updateContextUsage` 复用它；
3. 圆环百分比仍只由 `renderContextUsage` 写入；
4. `public/js/03-media.js::maybeWarnContextUsage`——达到「运行设置 → 上下文提醒阈值」
   时弹窗一次（以会话为单位，回落/换会话重新武装）；阈值 0 = 关闭；
5. `naiba/config.py`——`context_warning_percent` 的默认值、持久化与取值校验。
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import ConfigStore  # noqa: E402


def _read(name: str) -> str:
    return (ROOT / "public" / "js" / name).read_text(encoding="utf-8")


def _function_body(source: str, signature: str) -> str:
    """取函数体（到下一个顶层 function/export function 之前）。"""
    start = source.index(signature)
    rest = source[start:]
    match = re.search(r"\n(?:export )?function ", rest[1:])
    return rest if match is None else rest[: match.start() + 1]


class ContextRingRefreshTests(unittest.TestCase):
    def test_usage_event_refreshes_ring_immediately(self) -> None:
        body = _function_body(_read("12-chat-input.js"), "function handleUsageEvent")
        self.assertIn(
            "setContextUsage(event.usage",
            body,
            "usage 事件（每次模型请求）必须立即刷新上下文圆环",
        )
        self.assertIn("usageMarkup(event.usage", body, "消息尾端用量框仍要更新")

    def test_context_usage_state_has_single_writer(self) -> None:
        source = _read("03-media.js")
        writes = re.findall(r"state\.contextUsage\s*=", source)
        self.assertEqual(
            len(writes), 1, f"state.contextUsage 只允许一个写入点，实际 {len(writes)} 处"
        )
        setter = _function_body(source, "export function setContextUsage")
        self.assertIn("state.contextUsage", setter)
        self.assertIn("renderContextUsage()", setter)

    def test_update_context_usage_delegates_to_setter(self) -> None:
        body = _function_body(_read("03-media.js"), "export function updateContextUsage")
        self.assertIn(
            "setContextUsage(",
            body,
            "历史渲染与终态路径必须复用同一写入点（否则两套口径会漂移）",
        )

    def test_live_usage_survives_history_rerender_while_busy(self) -> None:
        """回归：运行中历史重渲染（轮询）曾把实时圆环擦回「暂无数据」。"""
        body = _function_body(_read("03-media.js"), "export function updateContextUsage")
        self.assertIn("state.chatBusy", body, "运行中不得用历史值覆盖实时值")
        self.assertIn(
            "contextUsageConversationId",
            body,
            "只有会话真正切换时才允许覆盖",
        )
        setter = _function_body(_read("03-media.js"), "export function setContextUsage")
        self.assertIn("contextUsageConversationId", setter, "写入点要记录实时值所属会话")

    def test_ring_percent_written_only_by_renderer(self) -> None:
        source = _read("03-media.js")
        body = _function_body(source, "export function renderContextUsage")
        start = source.index("export function renderContextUsage")
        span = (start, start + len(body))
        positions = [m.start() for m in re.finditer(r"--context-percent", source)]
        self.assertTrue(positions, "圆环百分比写入点丢失")
        for pos in positions:
            self.assertTrue(
                span[0] <= pos <= span[1],
                "圆环百分比只能由 renderContextUsage 写入",
            )


class ContextWarningTests(unittest.TestCase):
    """达阈值弹窗提醒（阈值可配、每会话只提醒一次）。"""

    def test_threshold_reads_from_runtime_settings(self) -> None:
        source = _read("03-media.js")
        body = _function_body(source, "export function contextWarningPercent")
        self.assertIn("context_warning_percent", body, "阈值必须来自运行设置")
        self.assertIn("state.bootstrap", body, "阈值从 bootstrap 设置读取（保存后即时生效）")

    def test_ring_render_triggers_warning_check(self) -> None:
        body = _function_body(_read("03-media.js"), "export function renderContextUsage")
        self.assertIn("maybeWarnContextUsage(", body, "圆环渲染后必须做阈值判定")

    def test_warning_fires_once_per_conversation(self) -> None:
        body = _function_body(_read("03-media.js"), "export function maybeWarnContextUsage")
        self.assertIn("contextWarningArmed", body, "必须用 armed 标记保证只提醒一次")
        self.assertIn("contextWarningConversationId", body, "换会话要重新武装")
        self.assertIn("threshold > 0", body, "阈值 0 = 关闭提醒")
        self.assertIn("showContextWarning(", body)

    def test_idle_conversation_does_not_popup_on_render(self) -> None:
        """空闲会话（只是切到旧会话）不得弹窗——留给点击发送时判定。"""
        body = _function_body(_read("03-media.js"), "export function maybeWarnContextUsage")
        self.assertIn(
            "if (!state.chatBusy || !state.contextWarningArmed) return;",
            body,
            "渲染路径必须要求运行中才弹窗",
        )

    def test_send_path_checks_pending_warning(self) -> None:
        source = _read("11-run-stream.js")
        body = _function_body(source, "export async function sendChatMessage")
        self.assertIn("pendingContextWarning()", body, "发送前必须做阈值判定")
        self.assertIn("skipContextWarning", body, "「继续发送」要能跳过判定")
        self.assertIn("showContextWarning(", body)
        # 判定必须在清空草稿之前，否则被拦下时输入框会被清空
        self.assertLess(
            body.index("pendingContextWarning()"),
            body.index("input.value = ''"),
            "判定要在清空输入框之前",
        )
        pending = _function_body(_read("03-media.js"), "export function pendingContextWarning")
        self.assertIn("state.chatBusy", pending, "运行中不走发送前判定")
        self.assertIn("state.contextPercent", pending, "用最近一次渲染出的百分比判定")
        self.assertIn("contextWarningArmed", pending, "提醒过一次就不再拦")

    def test_continue_button_wiring_and_reset(self) -> None:
        html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('id="contextWarningContinue"'), 1)
        self.assertIn('id="contextWarningContinue" type="button" hidden', html, "按钮默认隐藏")
        binds = _read("15-bind-events.js")
        self.assertIn("contextWarningContinue", binds, "「继续发送」必须有绑定")
        self.assertIn("continueAfterContextWarning", binds)
        self.assertIn("resetContextWarningResume", binds, "关闭弹窗要清掉待续动作")
        media = _read("03-media.js")
        self.assertIn("let contextWarningResume = null;", media)

    def test_dialog_markup_and_settings_field_exist(self) -> None:
        html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(
            html.count('id="contextWarningDialog"'), 1, "提醒弹窗必须存在且 id 唯一"
        )
        self.assertEqual(
            html.count('id="contextWarningPercent"'), 1, "运行设置里的阈值输入框必须存在且 id 唯一"
        )
        self.assertIn('data-close="contextWarningDialog"', html, "弹窗必须有可关闭入口")
        self.assertIn(
            'data-settings-panel="runtime"', html, "阈值必须落在运行设置面板里"
        )

    def test_warning_dialog_is_compact_and_centered(self) -> None:
        html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('class="context-warning-dialog"', html, "提醒弹窗要用专用紧凑样式")
        self.assertIn("建议让模型撰写交接文档，并开始新会话。", html, "正文文案")
        block = css.split(".context-warning-dialog {", 1)[1].split("}", 1)[0]
        # dialog 的 UA 居中依赖确定高度：height:auto 会把弹窗顶到上方、auto 外边距失效。
        self.assertIn("height: fit-content", block, "必须给确定高度才能居中")
        self.assertIn("margin: auto", block, "必须显式居中")
        self.assertIn("inset: 0", block)
        actions = css.split(".context-warning-dialog .form-actions", 1)[1].split("}", 1)[0]
        self.assertIn("justify-content: center", actions, "「知道了」按钮必须水平居中")

    def test_warning_advice_text_appears_once(self) -> None:
        body = _function_body(_read("03-media.js"), "export function showContextWarning")
        self.assertIn("contextWarningDetail", body)
        self.assertNotIn("建议新建对话后继续", body, "建议文案只在弹窗正文出现，不重复")

    def test_runtime_settings_populate_and_save_threshold(self) -> None:
        source = _read("09-settings.js")
        populate = _function_body(source, "export function populateRuntimeSettings")
        self.assertIn("contextWarningPercent", populate, "打开设置页要回填当前阈值")
        save = _function_body(source, "export async function saveRuntimeSettings")
        self.assertIn("context_warning_percent", save, "保存运行设置要提交阈值")


class ContextWarningConfigTests(unittest.TestCase):
    """后端：context_warning_percent 默认值 / 持久化 / 校验。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_is_80_and_public(self) -> None:
        store = ConfigStore(self.path)
        self.assertEqual(store.data["context_warning_percent"], 80)
        self.assertEqual(store.public()["context_warning_percent"], 80)

    def test_update_persists_across_reload(self) -> None:
        store = ConfigStore(self.path)
        store.update_settings({"context_warning_percent": 55})
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))["context_warning_percent"], 55
        )
        self.assertEqual(
            ConfigStore(self.path).data["context_warning_percent"], 55
        )

    def test_zero_disables_and_out_of_range_rejected(self) -> None:
        store = ConfigStore(self.path)
        store.update_settings({"context_warning_percent": 0})
        self.assertEqual(store.data["context_warning_percent"], 0)
        for bad in (101, -1, "abc"):
            with self.assertRaises(ValueError, msg=f"{bad!r} 必须被拒绝"):
                store.update_settings({"context_warning_percent": bad})


if __name__ == "__main__":
    unittest.main()
