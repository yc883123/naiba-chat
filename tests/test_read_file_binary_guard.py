# -*- coding: utf-8 -*-
"""护栏：read_file 二进制防护（嗅探 NUL/控制字符比例 → 明确报错 + 类型引导）。

保护对象：
- PDF（含 NUL）→ 引导 read_pdf / pdf_render_pages + vision_analyze；
- 图片（含 NUL）→ 引导 vision_analyze；
- 其它二进制 → 通用引导（pwsh 等）；
- GBK/UTF-8 中文文本不被误伤（无 NUL、无控制字符）。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.mcp import MCPRegistry  # noqa: E402
from naiba.tools.providers import core as core_provider  # noqa: E402


def _ctx(workspace: Path):
    return core_provider.ToolContext(
        workspace=workspace,
        python_executable=sys.executable,
        command_timeout=60,
        mcp_registry=MCPRegistry([]),
        mcp_register=None,
    )


class ReadFileBinaryGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba-binread-"))
        self.ctx = _ctx(self.tmp)

    def test_pdf_binary_guides_to_pdf_tools(self) -> None:
        target = self.tmp / "doc.pdf"
        target.write_bytes(b"%PDF-1.4\n\x00\x00\x00\x00\n%%EOF\n")
        result = core_provider._tool_read_file(self.ctx, {"path": str(target)}, None)
        self.assertIn("PDF", result)
        self.assertIn("read_pdf", result)
        self.assertIn("pdf_render_pages", result)
        self.assertIn("vision_analyze", result)

    def test_image_binary_guides_to_vision(self) -> None:
        target = self.tmp / "photo.png"
        target.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00")
        result = core_provider._tool_read_file(self.ctx, {"path": str(target)}, None)
        self.assertIn("图片", result)
        self.assertIn("vision_analyze", result)

    def test_generic_binary_guides_to_pwsh(self) -> None:
        target = self.tmp / "raw.bin"
        target.write_bytes(b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09" * 100)
        result = core_provider._tool_read_file(self.ctx, {"path": str(target)}, None)
        self.assertIn("二进制", result)
        self.assertIn("pwsh", result)

    def test_chinese_text_not_misclassified(self) -> None:
        target = self.tmp / "中文文件.txt"
        target.write_text("这是一段中文文本，包含标点符号与数字 123。\n" * 200, encoding="utf-8")
        result = core_provider._tool_read_file(self.ctx, {"path": str(target)}, None)
        self.assertIn("这是一段中文文本", result)

    def test_gbk_text_not_misclassified(self) -> None:
        target = self.tmp / "gbk.txt"
        target.write_bytes("GBK 编码的中文文本内容测试。\n".encode("gbk"))
        result = core_provider._tool_read_file(self.ctx, {"path": str(target)}, None)
        # GBK 字节无 NUL、无控制字符：不报二进制（按 utf-8 replace 显示乱码是既有行为）
        self.assertNotIn("二进制", result)


if __name__ == "__main__":
    unittest.main()
