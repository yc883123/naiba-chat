# -*- coding: utf-8 -*-
"""护栏：会话文件访问边界 _conv_file_* 行为规格。

保护对象：阶段 1 将 _conv_file_allow/open/save 迁出 server.py 到 core/conv_files.py 时的
行为等价性。安全边界：文件面板只允许"本会话改动过 或 位于会话工作区内"的文件，
写回必须同时满足两者，越界路径（含 ../ 穿越、绝对路径、http:// 前缀）一律拒绝。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import _conv_file_allow, _conv_file_open, _conv_file_save  # noqa: E402


class StubConfig:
    def __init__(self, base: Path):
        self.base = base

    def resolve_workspace_dir(self, raw=None):
        candidate = Path(str(raw or "").strip()) if str(raw or "").strip() else Path("ws")
        target = candidate if candidate.is_absolute() else self.base / candidate
        return target.resolve()


class ConversationFileBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.config = StubConfig(self.root)
        self.touched = str(self.ws / "a.txt")
        self.conv = {
            "workspace_dir": str(self.ws),
            "messages": [
                {"metadata": {"files": [{"path": self.touched}]}},
            ],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_touched_file_inside_workspace_allows_open_and_save(self):
        target = Path(self.touched)
        target.write_text("原始内容", encoding="utf-8")
        file_path, listed, within_root, root = _conv_file_allow(self.conv, self.config, self.touched)
        self.assertTrue(file_path)
        self.assertTrue(listed)
        self.assertTrue(within_root)
        opened = _conv_file_open(self.conv, self.config, self.touched)
        self.assertEqual(opened["kind"], "text")
        saved = _conv_file_save(self.conv, self.config, self.touched, "新的内容")
        self.assertEqual(saved["size"], len("新的内容".encode("utf-8")))
        self.assertEqual(target.read_text(encoding="utf-8"), "新的内容")
        self.assertFalse(any(Path(self.ws).glob(".*.naiba-tmp")), "临时文件残留")

    def test_inside_but_not_touched_cannot_save(self):
        other = self.ws / "b.txt"
        other.write_text("x", encoding="utf-8")
        _file_path, listed, within_root, _root = _conv_file_allow(self.conv, self.config, str(other))
        self.assertFalse(listed)
        self.assertTrue(within_root)
        # 只读可开，写回必须"改动过+在工作区内"同时成立。
        self.assertEqual(_conv_file_open(self.conv, self.config, str(other))["kind"], "text")
        with self.assertRaisesRegex(ValueError, "无权保存"):
            _conv_file_save(self.conv, self.config, str(other), "覆盖")

    def test_absolute_path_outside_workspace_rejected(self):
        outside = self.root / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        _file_path, listed, within_root, _root = _conv_file_allow(self.conv, self.config, str(outside))
        self.assertFalse(within_root)
        self.assertFalse(listed)
        with self.assertRaisesRegex(ValueError, "无权访问"):
            _conv_file_open(self.conv, self.config, str(outside))

    def test_traversal_out_of_workspace_rejected(self):
        traversal = str(self.ws / ".." / "escape.txt")
        _file_path, listed, within_root, _root = _conv_file_allow(self.conv, self.config, traversal)
        self.assertFalse(within_root)
        with self.assertRaisesRegex(ValueError, "无权访问"):
            _conv_file_open(self.conv, self.config, traversal)

    def test_http_prefix_rejected(self):
        _file_path, listed, within_root, _root = _conv_file_allow(
            self.conv, self.config, "https://example.com/a.txt"
        )
        self.assertIsNone(_file_path)
        self.assertFalse(within_root)

    def test_empty_conversation_uses_default_workspace(self):
        default_file = self.root / "ws" / "def.txt"
        default_file.write_text("d", encoding="utf-8")
        _file_path, listed, within_root, _root = _conv_file_allow(None, self.config, "def.txt")
        self.assertTrue(_file_path)
        self.assertTrue(within_root)
        self.assertFalse(listed)


if __name__ == "__main__":
    unittest.main()
