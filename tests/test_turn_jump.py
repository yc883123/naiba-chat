# -*- coding: utf-8 -*-
"""手机端轮次下拉守门（桌面刻度轨的等价形态）。

需求（用户原话归纳）：电脑端消息区右缘那道「每个用户轮次一条小横杠」的刻度轨，手机上换成
顶栏一个轮次下拉——列出每一轮（「第 N 轮 · 用户消息摘要」），选中即滚到该轮；滚动时下拉
自动显示当前所在轮次。电脑端刻度轨原样保留。

不变量：
1. `#turnJumpSelect` 在 `.topbar-actions` 内、带 `.mobile-only`（桌面隐藏）、初始 `hidden`；
2. 实现落在 `04-messages.js` **内部**，复用既有 `collectTurns()` / `turnRailTurns` /
   `turnRailActive` / `scrollToTurn()`——不复制算法、不导出内部状态、不新开模块；
3. options 只在**总轮数**变化时重建（教训 §九.37），轮数没变只就地改文案；
4. 跳转后的平滑滚动期间不回写选中项（否则会被「中途轮次」抢走），落点再同步一次；
5. 跳转到未渲染轮次必须走 `scrollToTurn()`（内部先扩懒加载窗口），不得自算 scrollTop（§九.56）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TurnJumpTests(unittest.TestCase):
    def _index(self) -> str:
        return (ROOT / "public/index.html").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def _messages(self) -> str:
        return (ROOT / "public/js/04-messages.js").read_text(encoding="utf-8")

    def test_select_is_in_topbar_actions_and_mobile_only(self) -> None:
        index = self._index()
        self.assertIn(
            '<select class="mobile-only turn-jump-select" id="turnJumpSelect"',
            index,
            "轮次下拉必须是「顶栏 + 仅手机」的形态",
        )
        actions = index[index.index('class="topbar-actions"'):]
        actions = actions[: actions.index("</header>")]
        self.assertIn('id="turnJumpSelect"', actions, "轮次下拉要放进顶栏操作区")
        start = index.index('<select class="mobile-only turn-jump-select"')
        markup = index[start: index.index(">", start) + 1]
        self.assertIn("hidden", markup, "初始 hidden（不足 2 轮时不显示）")
        css = self._css()
        self.assertIn(".turn-jump-select[hidden] { display: none; }", css, "hidden 必须能压过手机端的 display 规则")
        mobile = css[css.index("@media (max-width: 760px) {"):]
        mobile = mobile[: mobile.index("\n}")]
        self.assertIn(".turn-jump-select {", mobile, "显示规则写在既有 760 块内")

    def test_logic_stays_inside_messages_module(self) -> None:
        source = self._messages()
        for name in ("function renderTurnJump(", "function syncTurnJump(", "function applyTurnJump("):
            with self.subTest(function=name):
                self.assertIn(name, source)
                self.assertNotIn(f"export {name}", source, "内部状态不外泄")
        self.assertIn("renderTurnJump(turnRailTurns, active)", source, "共用刻度轨的 turnRailTurns / active")
        apply_body = source[source.index("function applyTurnJump("):]
        apply_body = apply_body[: apply_body.index("\n}")]
        self.assertIn("scrollToTurn(index)", apply_body, "必须复用 scrollToTurn（内含懒加载扩窗口）")
        self.assertNotIn("scrollTop", apply_body, "不得自算滚动位置")

    def test_options_rebuilt_only_when_turn_count_changes(self) -> None:
        source = self._messages()
        render = source[source.index("function renderTurnJump("):]
        render = render[: render.index("\n// 只挪选中项")]
        self.assertIn("select.options.length !== turns.length", render, "轮数没变就不重建 DOM")
        self.assertIn("select.replaceChildren()", render)
        self.assertIn("第 ${index + 1} 轮", source, "选项文案 = 第 N 轮 · 用户消息摘要")

    def test_hidden_below_two_turns_and_suppressed_while_jumping(self) -> None:
        source = self._messages()
        self.assertIn("function hideTurnJump()", source)
        rail = source[source.index("function renderTurnRail()"):]
        rail = rail[: rail.index("\n}")]
        self.assertIn("hideTurnJump();", rail, "只有一轮时不显示（与刻度轨一致）")
        sync = source[source.index("function syncTurnJump("):]
        sync = sync[: sync.index("\n}")]
        self.assertIn("turnJumpSuppressUntil", sync, "跳转滚动期间不回写选中项")
        init = source[source.index("export function initTurnRail()"):]
        init = init[: init.index("\n}")]
        self.assertIn("addEventListener('change'", init, "change 绑在 initTurnRail 里")
        self.assertNotIn("turnJumpSelect().addEventListener", init, "不要重复取 DOM 各绑一次")


if __name__ == "__main__":
    unittest.main()
