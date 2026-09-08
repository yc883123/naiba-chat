# -*- coding: utf-8 -*-
"""待发送附件列表（输入框上方的竖直列表）的接线守门。

背景：原先是横向 chip 条，文件名一长就一屏显示不全、还要横向拖滚动条（用户实测）。
现在改为**固定高度、可纵向滚动的一行一文件**，文件名一行截断、图片保留预览缩略图。
关键不变量：
1. `#pendingFiles` 渲染竖直行（`.pending-item` + `.pending-name` + 移除按钮）；
2. 列表容器必须把网格列约束成 `minmax(0, 1fr)`——否则默认 `auto` 列会被最长的
   不可断行文件名撑宽（实测行宽 1338px > 容器 870px，文件名反而不截断）；
3. 图片行给预览缩略图（带 `data-large-url`，沿用灯箱交互），非图片给占位图标；
4. 空列表隐藏（`hidden`），不再残留空白条。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class PendingFilesMarkupTests(unittest.TestCase):
    def _upload(self) -> str:
        return (ROOT / "public/js/10-upload.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def test_renders_vertical_rows(self) -> None:
        source = self._upload()
        self.assertIn('class="pending-item', source)
        self.assertIn('class="pending-name"', source)
        self.assertIn("data-remove-file=", source)
        # 文件名必须带 title（截断后仍可看全名）。
        self.assertIn('title="${escapeHtml(file.name)}"', source)
        # 不再渲染旧的横向 chip。
        self.assertNotIn('class="file-chip"', source)

    def test_image_rows_keep_preview(self) -> None:
        source = self._upload()
        self.assertIn('class="pending-thumb"', source)
        self.assertIn("data-large-url=", source, "图片预览缺少灯箱入口")
        self.assertIn("pending-thumb-file", source, "非图片行缺少占位图标")

    def test_uploading_state_shows_progress(self) -> None:
        source = self._upload()
        self.assertIn('class="pending-status"', source)
        self.assertIn("file.progress", source)

    def test_empty_list_hidden(self) -> None:
        source = self._upload()
        self.assertIn("container.hidden = state.pendingFiles.length === 0;", source)
        index = (ROOT / "public/index.html").read_text(encoding="utf-8")
        self.assertIn('id="pendingFiles" hidden', index, "初始状态应为隐藏")

    def test_css_constrains_column_and_truncates(self) -> None:
        css = self._css()
        rule = css[css.index(".pending-files {"):]
        rule = rule[: rule.index("}")]
        self.assertIn("minmax(0, 1fr)", rule, "网格列未约束 → 长文件名会把整列撑宽")
        self.assertIn("max-height", rule)
        self.assertIn("overflow-y: auto", rule)
        self.assertIn(".pending-files[hidden]", css, "hidden 属性会被 display:grid 覆盖")
        name_rule = css[css.index(".pending-name {"):]
        name_rule = name_rule[: name_rule.index("}")]
        for snippet in ("text-overflow: ellipsis", "white-space: nowrap", "min-width: 0"):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, name_rule)

    def test_rows_stand_out_from_background(self) -> None:
        """行必须与页面背景有明显区分（用户实测："文件框跟背景区分度太差"）。"""
        css = self._css()
        rule = css[css.index(".pending-item {"):]
        rule = rule[: rule.index("}")]
        self.assertIn("background: var(--surface-2)", rule, "行填充色应与页面背景不同")
        self.assertIn("border: 1px solid var(--line-strong)", rule, "描边应加强到 line-strong")
        self.assertIn("box-shadow", rule)
        thumb = css[css.index(".pending-thumb {"):]
        thumb = thumb[: thumb.index("}")]
        self.assertIn("background: var(--surface)", thumb, "缩略图/图标方块应为白色内嵌块")

    def test_remove_binding_still_present(self) -> None:
        bind = (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")
        self.assertIn("$('#pendingFiles').addEventListener('click'", bind)
        self.assertIn("data-remove-file", bind)


if __name__ == "__main__":
    unittest.main()
