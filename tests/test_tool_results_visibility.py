# -*- coding: utf-8 -*-
"""护栏：工具结果对模型可见性（core.tool_results）——前后端所见一致。

- 机器字段（存储路径/缩略图/尺寸/SHA-256/产物路径）从模型上下文剥离，
  前端展示（stream 事件 / metadata.tool_runs）与上下文同源；
- arguments/reason（模型自产）不进模型上下文；display_run 仅保留它们作展示；
- 超长结果统一带截断标记（模型不会把截断点当成全部内容）。
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.core.tool_results import (  # noqa: E402
    display_tool_run,
    model_visible_result,
    model_visible_run,
    truncate,
)

VISION_LOAD_RESULT = json.dumps(
    {
        "note": "三张图",
        "images": [
            {"name": "a.png", "path": "/host/a.png", "thumb_path": "/host/t.jpg", "width": 1, "height": 1}
        ],
    },
    ensure_ascii=False,
)
PIXEL_DIFF_RESULT = json.dumps(
    {
        "ratio": 0.5,
        "differing_pixels": 10,
        "total_pixels": 100,
        "threshold": 16,
        "heatmap": r"D:\out\diff_1.png",
        "worst_regions": [{"grid": [0, 0], "pixels": 8}],
    },
    ensure_ascii=False,
)
CROP_RESULT = json.dumps(
    {"path": r"D:\out\crop_1.png", "size": [640, 480], "box": [0, 0, 640, 480]},
    ensure_ascii=False,
)


class ToolResultsVisibilityTests(unittest.TestCase):
    def test_vision_load_keeps_names_only(self) -> None:
        payload = json.loads(model_visible_result("vision_analyze", VISION_LOAD_RESULT))
        self.assertEqual(payload["images"], ["a.png"])
        self.assertNotIn("path", str(payload))
        self.assertNotIn("thumb_path", str(payload))
        self.assertNotIn("width", str(payload))

    def test_vision_ops_keeps_product_paths(self) -> None:
        """crop/热力图产物路径必须对模型可见：产物是模型后续引用对象
        （保存/复制/再处理），剥离路径会逼模型绕道重做（实测：多轮搜索+PowerShell 重算）。"""
        diff = json.loads(model_visible_result("vision_image_ops", PIXEL_DIFF_RESULT))
        self.assertEqual(diff["heatmap"], r"D:\out\diff_1.png")
        self.assertEqual(diff["ratio"], 0.5)
        crop = json.loads(model_visible_result("vision_image_ops", CROP_RESULT))
        self.assertEqual(crop["path"], r"D:\out\crop_1.png")
        self.assertEqual(crop["size"], [640, 480])

    def test_plain_text_result_passes_through_until_truncation(self) -> None:
        self.assertEqual(model_visible_result("read_file", "内容"), "内容")

    def test_truncate_adds_clear_marker(self) -> None:
        text = "x" * 40000
        cut = truncate(text, 30000)
        self.assertIn("已截断", cut)
        self.assertIn("40000", cut)
        self.assertEqual(truncate("短文", 30000), "短文")

    def test_model_run_contains_only_tool_success_result(self) -> None:
        run = {
            "tool": "vision_analyze",
            "arguments": {"paths": ["/host/a.png"]},
            "result": VISION_LOAD_RESULT,
            "success": True,
            "reason": "想看图片",
        }
        visible = model_visible_run(run)
        self.assertEqual(set(visible.keys()), {"tool", "success", "result"})
        self.assertNotIn("arguments", visible)
        self.assertNotIn("reason", visible)
        self.assertIn("a.png", visible["result"])
        self.assertNotIn("/host/", visible["result"])

    def test_display_run_keeps_arguments_reason_with_visible_result(self) -> None:
        run = {
            "tool": "vision_image_ops",
            "arguments": {"op": "crop", "image": "/host/a.png"},
            "result": CROP_RESULT,
            "success": True,
            "reason": "裁剪人脸",
        }
        shown = display_tool_run(run)
        self.assertEqual(shown["arguments"], {"op": "crop", "image": "/host/a.png"})
        self.assertEqual(shown["reason"], "裁剪人脸")
        self.assertEqual(shown["tool"], "vision_image_ops")
        # 产物路径保留（模型引用凭据），display 与 model 同源
        self.assertIn("crop_1.png", shown["result"])


if __name__ == "__main__":
    unittest.main()
