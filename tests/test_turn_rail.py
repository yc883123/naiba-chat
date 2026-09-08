# -*- coding: utf-8 -*-
"""对话刻度轨（右侧横条）守门。

需求（用户原话归纳）：会话页右侧一列横条，**每个用户轮次一条**；页面滚到对应轮次时该条
**加粗凸起**（按"视口中心落在哪一轮"判定）；横条最多同时显示**视口附近 30 条**（滑动窗口）；
悬停刻度**微微增长加粗并弹出概要**（用户消息 1 行、AI 回复 2 行，超出省略号）；为了不逼仄，
**消息列整体左移**半条轨道宽度。

不变量：
- `#turnRail` 在 `.chat-main` 内，绝对定位到「消息区」那一格（`grid-row: 2`），高度自动跟随；
- 轨道宽度由 `--turn-rail-w` 统一控制；窄屏（≤760px）置 0 并隐藏轨道，消息列恢复居中；
- `.messages` / `.composer-wrap` 的右内边距 = 左内边距 + 轨道宽 → 列左移 rail/2；
- 刻度最多 30 条（`TURN_RAIL_MAX`），窗口随滚动滑动；行区间不变时不重写 DOM（教训 §九.37）；
- 高亮判定 = 视口中心所在轮次，且同一时刻只有一条；
- 点击刻度把该轮滚到视口中心（跳转后高亮不漂移）。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TurnRailTests(unittest.TestCase):
    def _index(self) -> str:
        return (ROOT / "public/index.html").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def _messages(self) -> str:
        return (ROOT / "public/js/04-messages.js").read_text(encoding="utf-8")

    def _bind(self) -> str:
        return (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")

    def test_rail_element_and_positioning(self) -> None:
        index = self._index()
        self.assertIn('id="turnRail"', index)
        self.assertLess(index.index('id="messages"'), index.index('id="turnRail"'), "轨道要放在消息区之后")
        css = self._css()
        rail = css[css.index(".turn-rail {"):]
        rail = rail[: rail.index("}")]
        self.assertIn("position: absolute", rail)
        self.assertIn("grid-row: 2", rail, "绝对定位到消息区那一格，高度才自动跟随")
        self.assertIn("right: 12px", rail, "要让开滚动条")
        self.assertIn("width: var(--turn-rail-w", rail)
        shell = css[css.index(".app-shell {"):]
        shell = shell[: shell.index("}")]
        self.assertIn("--turn-rail-w:", shell, "轨道宽度必须是一个统一变量")

    def test_message_column_shifts_left_by_half_rail(self) -> None:
        css = self._css()
        for selector in (".messages {", ".composer-wrap {"):
            rule = css[css.index(selector):]
            rule = rule[: rule.index("}")]
            with self.subTest(selector=selector):
                self.assertIn("--turn-rail-w", rule, "内边距要按轨道宽度重算，否则列不会左移")
        messages_rule = css[css.index(".messages {"):]
        messages_rule = messages_rule[: messages_rule.index("}")]
        self.assertIn("+ var(--turn-rail-w))", messages_rule, "右内边距 = 居中偏移 + 轨道宽")

    def test_narrow_screen_hides_rail(self) -> None:
        css = self._css()
        mobile = css[css.index("@media (max-width: 760px) {"):]
        mobile = mobile[: mobile.index("\n}")]
        self.assertIn("--turn-rail-w: 0px", mobile)
        self.assertIn(".turn-rail { display: none; }", mobile)

    def test_tick_styles_active_and_hover_grow(self) -> None:
        css = self._css()
        tick = css[css.index(".turn-tick {"):]
        tick = tick[: tick.index("}")]
        self.assertIn("height: 3px", tick)
        self.assertIn("width: 20px", tick)
        hover = css[css.index(".turn-tick:hover {"):]
        hover = hover[: hover.index("}")]
        self.assertIn("width: 26px", hover, "悬停微微增长")
        self.assertIn("height: 5px", hover)
        active = css[css.index(".turn-tick.active {"):]
        active = active[: active.index("}")]
        self.assertIn("width: 26px", active)
        self.assertIn("height: 5px", active)
        self.assertIn("background: var(--text)", active, "当前轮次要最显眼")
        user = css[css.index(".turn-tip-user {"):]
        user = user[: user.index("}")]
        self.assertIn("-webkit-line-clamp: 1", user, "用户消息一行")
        reply = css[css.index(".turn-tip-reply {"):]
        reply = reply[: reply.index("}")]
        self.assertIn("-webkit-line-clamp: 2", reply, "AI 回复两行")

    def test_rail_logic(self) -> None:
        source = self._messages()
        self.assertIn("const TURN_RAIL_MAX = 30;", source, "最多同时显示 30 条")
        self.assertIn("export function initTurnRail()", source)
        # 一个用户轮次 = 一条 user 行 + 其后助手回复
        collect = source[source.index("function collectTurns()"):]
        collect = collect[: collect.index("\n}")]
        self.assertIn("classList.contains('user')", collect)
        self.assertIn("turns[turns.length - 1].reply", collect)
        # 高亮 = 视口中心所在轮次
        active = source[source.index("function turnRailActiveIndex("):]
        active = active[: active.index("\n}")]
        self.assertIn("container.scrollTop + container.clientHeight / 2", active)
        self.assertIn("offsets[index] <= center", active)
        # 窗口滑动 + 行区间不变时不重写 DOM
        render = source[source.index("function renderTurnRail()"):]
        render = render[: render.index("\n}")]
        self.assertIn("turnRailTurns.length < 2", render, "只有一轮时不显示轨道")
        self.assertIn("start === turnRailWindow.start && end === turnRailWindow.end", render)
        self.assertIn("rail.replaceChildren()", render)
        # 点击跳转把目标轮次滚到视口中心
        scroll = source[source.index("function scrollToTurn("):]
        scroll = scroll[: scroll.index("\n}")]
        self.assertIn("(container.clientHeight - rowRect.height) / 2", scroll)
        # 概要内容：用户 + AI 回复
        tip = source[source.index("function showTurnTip("):]
        tip = tip[: tip.index("\n}")]
        self.assertIn("turn-tip-user", tip)
        self.assertIn("turn-tip-reply", tip)
        self.assertIn("escapeHtml", tip, "概要文本必须转义")

    def test_rail_is_initialised_from_bind_events(self) -> None:
        bind = self._bind()
        self.assertIn("initTurnRail", bind.split("\n")[6], "需要在 import 里引入 initTurnRail")
        self.assertIn("initTurnRail();", bind, "bindEvents 里必须调用一次")

    def test_index_html_still_has_no_duplicate_ids(self) -> None:
        ids = re.findall(r'\sid="([^"]+)"', self._index())
        duplicates = {value for value in ids if ids.count(value) > 1}
        self.assertFalse(duplicates, f"index.html 存在重复 id：{sorted(duplicates)}")


if __name__ == "__main__":
    unittest.main()
