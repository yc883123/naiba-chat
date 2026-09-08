# -*- coding: utf-8 -*-
"""视觉分批守门：vision_analyze 单批 ≤4 张、超限标注、不静默截断（教训 24）。

保护对象：
- _extract_step_image_batches：每个调用独立一批（≤4 张注入），loaded/shown/total_batches
  元数据完整——超限不再静默（旧 parts[:4] 让模型误以为后续批次不存在）；
- registry 双形态最大张数默认 4（装载/分析），schema 提示分多次调用。
"""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.skills.agent import _extract_step_image_batches  # noqa: E402
from naiba.tools.registry import (  # noqa: E402
    VISION_ANALYZE_DESCRIPTION,
    VISION_ANALYZE_LOAD_PARAMETERS,
    VISION_ANALYZE_PARAMETERS,
)


def _png(path: Path) -> None:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "blue").save(buf, format="PNG")
    path.write_bytes(buf.getvalue())


class ExtractStepImageBatchesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba-vision-batch-"))
        self.images = []
        for index in range(5):
            p = self.tmp / f"img_{index}.png"
            _png(p)
            self.images.append({"name": p.name, "path": str(p), "thumb_path": ""})

    def _run(self, tool: str, images: list[dict]) -> dict:
        return {"tool": tool, "result": json.dumps({"note": "x", "images": images}, ensure_ascii=False), "success": True}

    def test_two_calls_yield_two_batches(self) -> None:
        runs = [self._run("vision_analyze", self.images), self._run("vision_analyze", self.images)]
        batches = _extract_step_image_batches(runs, inject=True)
        self.assertEqual(len(batches), 2, "每次调用应独立成批")
        for b in batches:
            self.assertEqual(b["loaded"], 5)
            self.assertEqual(b["shown"], 4, "单批最多注入 4 张")
            self.assertEqual(len(b["parts"]), 4)
        self.assertEqual([b["batch_index"] for b in batches], [1, 2])
        self.assertEqual(batches[0]["total_batches"], 2)
        self.assertEqual(batches[1]["total_batches"], 2)

    def test_within_limit_single_batch(self) -> None:
        batches = _extract_step_image_batches([self._run("vision_analyze", self.images[:3])], inject=True)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["loaded"], 3)
        self.assertEqual(batches[0]["shown"], 3)
        self.assertEqual(len(batches[0]["parts"]), 3)

    def test_text_brain_injects_nothing(self) -> None:
        batches = _extract_step_image_batches([self._run("vision_analyze", self.images)], inject=False)
        self.assertEqual(batches, [], "文本大脑不注入图片，不生成批次")

    def test_non_vision_runs_skipped(self) -> None:
        runs = [self._run("read_file", []), {"tool": "vision_analyze", "result": "not-json", "success": True}]
        batches = _extract_step_image_batches(runs, inject=True)
        self.assertEqual(batches, [])


class VisionBatchSchemaTests(unittest.TestCase):
    def test_load_variant_default_is_four(self) -> None:
        prop = VISION_ANALYZE_LOAD_PARAMETERS["properties"]["max_images"]
        self.assertEqual(prop.get("default"), 4, "装载形态单批默认 4 张")
        self.assertIn("分多次", str(prop.get("description") or ""))

    def test_analyze_variant_default_is_four_and_hinted(self) -> None:
        prop = VISION_ANALYZE_PARAMETERS["properties"]["max_images"]
        self.assertEqual(prop.get("default"), 4, "分析形态单批默认 4 张")
        desc = str(VISION_ANALYZE_DESCRIPTION)
        self.assertIn("4 张", desc)


if __name__ == "__main__":
    unittest.main()
