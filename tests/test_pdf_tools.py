# -*- coding: utf-8 -*-
"""PDF 工具守门：文本提取/页渲染/局部放大/上限/幂等/越界报错（fixture 动态生成）。"""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from naiba.mcp import MCPRegistry
from naiba.tools.providers import core as core_provider
from naiba.tools.providers.documents import DocumentToolProvider


def _ctx(workspace: Path) -> core_provider.ToolContext:
    return core_provider.ToolContext(
        workspace=workspace,
        python_executable=sys.executable,
        command_timeout=60,
        mcp_registry=MCPRegistry([]),
        mcp_register=None,
    )


def _make_pdf(path: Path, pages: int = 3, text_prefix: str = "Page") -> None:
    import pymupdf

    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), f"{text_prefix} {index + 1} content line", fontsize=12)
    doc.save(str(path))
    doc.close()


def _make_scanned_pdf(path: Path) -> None:
    """无文本层：只有一张嵌入图片。"""
    import pymupdf
    from PIL import Image

    doc = pymupdf.open()
    page = doc.new_page()
    buf = io.BytesIO()
    Image.new("RGB", (200, 100), "white").save(buf, format="PNG")
    page.insert_image(page.rect, stream=buf.getvalue())
    doc.save(str(path))
    doc.close()


class PdfServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba-pdf-"))
        # workspace 独立子目录：self.pdf 位于工作区之外（验证越界确认策略）。
        self.workspace = self.tmp / "ws"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.data_dir = self.tmp / "data"
        self.pdf = self.tmp / "sample.pdf"
        _make_pdf(self.pdf, pages=3)

    def _provider(self):
        ctx = _ctx(self.workspace)
        return DocumentToolProvider(ctx, lambda: self.data_dir)

    def _spec(self, name: str):
        specs = self._provider().tools()
        return next(spec for spec in specs if spec.name == name)

    def test_extract_text_and_page_markers(self) -> None:
        ok, out = self._spec("read_pdf").execute({"path": str(self.pdf)}, [], None)
        self.assertTrue(ok, out)
        self.assertIn("Page 1 content line", out)
        self.assertIn("== 第 2 页 ==", out)
        self.assertIn("== 第 3 页 ==", out)

    def test_extract_range(self) -> None:
        ok, out = self._spec("read_pdf").execute(
            {"path": str(self.pdf), "start_page": 2, "end_page": 3}, [], None
        )
        self.assertTrue(ok, out)
        self.assertNotIn("Page 1 content", out)
        self.assertIn("Page 2 content", out)

    def test_extract_out_of_range_errors(self) -> None:
        ok, out = self._spec("read_pdf").execute({"path": str(self.pdf), "start_page": 99}, [], None)
        self.assertFalse(ok)
        self.assertIn("越界", out)

    def test_extract_scanned_guides_to_render(self) -> None:
        scanned = self.tmp / "scan.pdf"
        _make_scanned_pdf(scanned)
        ok, out = self._spec("read_pdf").execute({"path": str(scanned)}, [], None)
        self.assertTrue(ok, out)  # 业务提示仍为成功（结果是"建议转图"）
        self.assertIn("pdf_render_pages", out)
        self.assertIn("vision_analyze", out)

    def test_extract_missing_file_errors(self) -> None:
        ok, out = self._spec("read_pdf").execute({"path": str(self.tmp / "nope.pdf")}, [], None)
        self.assertFalse(ok)
        self.assertIn("不存在", out)

    def test_render_pages_power_of_two(self) -> None:
        ok, out = self._spec("pdf_render_pages").execute({"path": str(self.pdf)}, [], None)
        self.assertTrue(ok, out)
        payload = json.loads(out)
        self.assertEqual(payload["total_pages"], 3)
        self.assertEqual(len(payload["rendered"]), 3)
        for item in payload["rendered"]:
            self.assertTrue(Path(item["path"]).is_file(), item["path"])
            self.assertTrue(Path(item["path"]).stat().st_size > 0)
            self.assertIn("pdf_pages", item["path"])
        # 幂等：再渲染返回同一路径
        ok2, out2 = self._spec("pdf_render_pages").execute({"path": str(self.pdf)}, [], None)
        payload2 = json.loads(out2)
        self.assertEqual([p["path"] for p in payload2["rendered"]], [p["path"] for p in payload["rendered"]])

    def test_render_pages_over_budget_errors(self) -> None:
        big = self.tmp / "big.pdf"
        _make_pdf(big, pages=25)
        ok, out = self._spec("pdf_render_pages").execute(
            {"path": str(big), "pages": "1-21"}, [], None
        )
        self.assertFalse(ok)
        self.assertIn("最多", out)

    def test_zoom_region_and_cache(self) -> None:
        ok, out = self._spec("pdf_zoom_region").execute(
            {"path": str(self.pdf), "page": 1, "region": "50,0,100,50", "scale": 3}, [], None
        )
        self.assertTrue(ok, out)
        payload = json.loads(out)
        image = Path(payload["path"])
        self.assertTrue(image.is_file())
        self.assertGreater(image.stat().st_size, 0)
        # 幂等
        ok2, out2 = self._spec("pdf_zoom_region").execute(
            {"path": str(self.pdf), "page": 1, "region": "50,0,100,50", "scale": 3}, [], None
        )
        self.assertEqual(json.loads(out2)["path"], payload["path"])

    def test_zoom_keyword_region(self) -> None:
        ok, out = self._spec("pdf_zoom_region").execute(
            {"path": str(self.pdf), "page": 1, "region": "top"}, [], None
        )
        self.assertTrue(ok, out)
        payload = json.loads(out)
        self.assertEqual(payload["region"], "0,0,100,50")

    def test_zoom_invalid_arguments_error(self) -> None:
        ok, out = self._spec("pdf_zoom_region").execute(
            {"path": str(self.pdf), "page": 1, "region": "0,0,3,3"}, [], None
        )
        self.assertFalse(ok)
        self.assertIn("区域过小", out)
        ok, out = self._spec("pdf_zoom_region").execute(
            {"path": str(self.pdf), "page": 1, "region": "top", "scale": 8}, [], None
        )
        self.assertFalse(ok)
        self.assertIn("scale", out)
        ok, out = self._spec("pdf_zoom_region").execute(
            {"path": str(self.pdf), "page": 99, "region": "top"}, [], None
        )
        self.assertFalse(ok)
        self.assertIn("越界", out)
        ok, out = self._spec("pdf_zoom_region").execute(
            {"path": str(self.pdf), "page": 99, "region": "top"}, [], None
        )
        self.assertFalse(ok)
        self.assertIn("越界", out)

    def test_document_provider_policy_bound(self) -> None:
        specs = {spec.name: spec for spec in self._provider().tools()}
        self.assertIsNotNone(specs["read_pdf"].policy)
        self.assertIsNotNone(specs["pdf_render_pages"].policy)
        self.assertIsNotNone(specs["pdf_zoom_region"].policy)
        # 渲染/放大无确认（托管缓存）
        self.assertEqual(specs["pdf_render_pages"].policy("pdf_render_pages", {}, [], "confirm", None, self.tmp), "")
        # 读有路径确认策略（工作区外必确认）
        reason = specs["read_pdf"].policy("read_pdf", {"path": str(self.pdf)}, [], "confirm", None, self.workspace)
        self.assertIn("工作区外", reason)
        inside = self.workspace / "inside.pdf"
        inside.write_bytes(b"%PDF-1.4\n%%EOF")
        reason = specs["read_pdf"].policy("read_pdf", {"path": str(inside)}, [], "auto", None, self.workspace)
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
