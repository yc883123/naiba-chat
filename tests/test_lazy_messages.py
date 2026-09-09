# -*- coding: utf-8 -*-
"""护栏：消息列表懒加载（默认只渲染最近 N 轮，向上滚动按轮预渲染）。

核心约束：
1. 渲染窗口**恒以「轮」为边界**（轮 = 一条 user 消息 + 其后到下一条 user 之前的助手消息）：
   一条 AI 回复上的「新会话」分割线属于该轮，绝不能出现"分割线在窗口内、锚点消息在窗口外"；
2. 切换会话 → 窗口重置为最近 N 轮；同一会话刷新（轮询/保存）→ 保留窗口与滚动位置；
3. 向上滚动预渲染时，补进来的高度要加回 scrollTop（视口内容不跳）；
4. 刻度轨改为**数据驱动**（state.messages），懒加载下仍覆盖全部轮次，点未渲染的轮次先渲染再跳。
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / "public" / "js" / name).read_text(encoding="utf-8")


class LazyWindowTests(unittest.TestCase):
    def setUp(self):
        self.js = _read("04-messages.js")

    def _body(self, signature: str) -> str:
        start = self.js.index(signature)
        rest = self.js[start:]
        match = re.search(r"\n(?:export )?function ", rest[1:])
        return rest if match is None else rest[: match.start() + 1]

    def test_window_constants(self):
        for snippet in ("const LAZY_TURNS_INITIAL = 10", "const LAZY_TURNS_STEP = 10",
                        "const LAZY_TOP_TRIGGER = 240", "let lazySuppressUntil = 0"):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, self.js)

    def test_window_is_turn_aligned(self):
        for signature in ("function turnStartIndexes(", "function clampTurnStart(",
                          "function initialRenderStart("):
            with self.subTest(signature=signature):
                self.assertIn(signature, self.js)
        starts = self._body("function turnStartIndexes(")
        self.assertIn("message?.role === 'user'", starts, "轮起点 = user 消息下标")
        self.assertIn("starts.unshift(0)", starts, "首条不是 user 时也要有 0 号起点")
        initial = self._body("function initialRenderStart(")
        self.assertIn("LAZY_TURNS_INITIAL", initial)

    def test_switch_resets_window_same_conversation_keeps_it(self):
        body = self._body("export function renderMessages(")
        self.assertIn("const switched = state.messagesConversationId !== String(state.conversationId || '')", body)
        self.assertIn("switched ? initialRenderStart(list) : clampTurnStart(list, Number(state.renderStart) || 0)", body,
                      "切换会话重置窗口，同会话刷新保留窗口")
        self.assertIn("const keepScroll = !switched && !stickToBottom", body)
        self.assertIn("withInstantScroll(container, () => { container.scrollTop = previousScrollTop; })", body,
                      "同会话刷新要保留滚动位置")
        self.assertIn("lazySuppressUntil = Date.now() + 400", body,
                      "渲染引发的 scroll 事件不得当成用户滚到顶")

    def test_divider_travels_with_its_anchor(self):
        body = self._body("function messageRangeFragment(")
        self.assertIn("fragment.append(messageElement(message))", body)
        self.assertIn("const divider = sessionDividerAfter(message)", body)
        self.assertIn("if (divider) fragment.append(divider)", body,
                      "分割线必须紧跟锚点消息一起进出窗口")

    def test_extend_keeps_viewport_and_is_turn_aligned(self):
        body = self._body("export function extendRenderedWindow(")
        self.assertIn("turnStartIndexes(messages)", body, "扩展也必须落在轮起点上")
        self.assertIn("LAZY_TURNS_STEP", body)
        self.assertIn("anchor.before(fragment)", body, "补进的内容插在当前窗口之前")
        self.assertIn("container.scrollTop += container.scrollHeight - beforeHeight", body,
                      "补进来的高度加回 scrollTop，视口不跳")
        self.assertIn("withInstantScroll(", body, "容器是 smooth 滚动，补偿必须瞬时生效")
        self.assertIn("scheduleTurnRail()", body, "窗口变化后刻度轨要跟着刷新")

    def test_instant_scroll_helper(self):
        body = self._body("function withInstantScroll(")
        self.assertIn("container.style.scrollBehavior = 'auto'", body)
        self.assertIn("container.style.scrollBehavior = previous", body, "用完要还原")

    def test_scroll_trigger_guards(self):
        self.assertIn("if (Date.now() < lazySuppressUntil) return;", self.js)
        self.assertIn("if (stickToBottom) return;", self.js, "仍在底部附近不算翻历史")
        self.assertIn("container.scrollTop <= LAZY_TOP_TRIGGER", self.js)
        self.assertIn("extendRenderedWindow();", self.js)


class TurnRailLazyTests(unittest.TestCase):
    def setUp(self):
        self.js = _read("04-messages.js")

    def test_turns_collected_from_state_not_dom(self):
        body = self.js[self.js.index("function collectTurns("):]
        body = body[: body.index("\n}")]
        self.assertIn("const messages = state.messages || []", body,
                      "懒加载下未渲染的轮次没有 DOM，刻度轨必须从数据收集")
        self.assertIn("anchors.set(row.dataset.messageId, row)", body, "已渲染的轮次顺带记下锚点")
        self.assertIn("messageIndex", body, "记录消息下标，供跳转时按需渲染")

    def test_ticks_carry_message_id(self):
        self.assertIn("tick.dataset.turnMessageId = String(turnRailTurns[index]?.messageId || '')", self.js,
                      "刻度带用户消息 id：懒加载后第 N 轮与 DOM 行不再一一对应")

    def test_offsets_tolerate_unrendered_turns(self):
        body = self.js[self.js.index("function turnRailOffsets("):]
        body = body[: body.index("\n}")]
        self.assertIn("null", body)
        self.assertIn("offsets[index] = offsets[index + 1] - 120", body, "未渲染轮次向上均摊估算")

    def test_click_jump_renders_missing_turn_first(self):
        body = self.js[self.js.index("function scrollToTurn("):]
        body = body[: body.index("\n}")]
        self.assertIn("ensureMessageRendered(turn.messageIndex)", body)
        self.assertIn("turnRailTurns = collectTurns()", body, "渲染后重新收集锚点")
        ensure = self.js[self.js.index("function ensureMessageRendered("):]
        ensure = ensure[: ensure.index("\n}")]
        self.assertIn("extendRenderedWindow()", ensure)

    def test_streaming_turn_enters_state(self):
        js = _read("11-run-stream.js")
        self.assertIn("state.messages = [...(state.messages || []), optimisticUser]", js,
                      "乐观插入的用户消息要同步进 state.messages，刻度轨才会立刻多一条")


if __name__ == "__main__":
    unittest.main()
