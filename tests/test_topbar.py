# -*- coding: utf-8 -*-
"""顶栏样式守门（任务计数 / Skill 标签 / MCP 指示灯 / 刷新按钮归位）。

背景：用户走查提出四处——①「任务」前的数字是圆角胶囊，像奇怪徽标；②按钮显示
「Skill 8 Skill」（计数里又拼了一次 Skill）；③MCP 后面拖「· 已就绪」等文字；
④「刷新」被藏进「⋯」溢出菜单。本测试把四条不变量钉在源码上。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TopbarStyleTests(unittest.TestCase):
    def _index(self) -> str:
        return (ROOT / "public/index.html").read_text(encoding="utf-8")

    def _settings(self) -> str:
        return (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def _bind(self) -> str:
        return (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")

    def test_overflow_menu_removed_and_reload_moved_back(self) -> None:
        index = self._index()
        self.assertNotIn("topbarMoreButton", index, "「⋯」按钮又回来了")
        self.assertNotIn("topbarOverflowMenu", index, "溢出菜单又回来了")
        actions = index[index.index('class="topbar-actions"'):]
        actions = actions[: actions.index("</header>")]
        self.assertIn('id="reloadPage"', actions, "刷新按钮不在顶栏操作区")
        self.assertIn('id="unloadModel"', actions, "卸载模型按钮不在顶栏操作区")
        bind = self._bind()
        self.assertNotIn("topbarMoreButton", bind, "溢出菜单的绑定未清理")
        self.assertIn("$('#reloadPage')?.addEventListener", bind)

    def test_task_count_is_plain_text(self) -> None:
        css = self._css()
        rule = css[css.index("#taskCount {"):]
        rule = rule[: rule.index("}")]
        self.assertNotIn("border-radius", rule, "计数又变回胶囊")
        self.assertNotIn("background", rule, "计数不应有胶囊底色")
        self.assertIn("font-variant-numeric", rule)

    def test_skill_button_has_no_duplicate_prefix(self) -> None:
        self.assertIn("$('#skillCount').textContent = String(state.bootstrap.skills.length);", self._settings())
        self.assertNotIn("`Skill ${state.bootstrap.skills.length}`", self._settings())
        index = self._index()
        self.assertIn('<span id="skillCount">0</span>', index)

    def test_mcp_status_is_color_only(self) -> None:
        source = self._settings()
        render = source[source.index("export function renderMcp()"):]
        render = render[: render.index("\n}\n")]
        self.assertIn("label.textContent = 'MCP';", render, "MCP 文字应恒为「MCP」")
        self.assertNotIn("dot.style.background", render, "状态不应再用内联颜色（走 CSS 类）")
        self.assertIn("disconnected", render, "未连接状态应走 disconnected 类")
        css = self._css()
        self.assertIn(".mcp-button.disconnected i", css)
        self.assertIn(".mcp-button.connected i", css)
        self.assertIn(".mcp-button.error i", css)

    def test_topbar_control_button_icons_are_outlined(self) -> None:
        """搬回操作区的按钮必须有线性 SVG 样式，否则裸 SVG 会渲染成黑色实心块。"""
        css = self._css()
        self.assertIn(".topbar-actions .control-button svg", css)
        rule = css[css.index(".topbar-actions .control-button svg {"):]
        rule = rule[: rule.index("}")]
        self.assertIn("fill: none", rule)
        self.assertIn("stroke: currentColor", rule)


if __name__ == "__main__":
    unittest.main()
