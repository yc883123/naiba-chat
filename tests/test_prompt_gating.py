# -*- coding: utf-8 -*-
"""守门：系统提示里的视觉/ComfyUI 编排指引必须与会话固化工具集同口径（缺工具就不提）。

背景：这两段原本无条件注入——工具集里没有对应工具时，系统提示等于让模型去调用不存在的
工具；而会话工具集首轮固化、中途不可改，用户只能重开会话换 Agent（PDF 段已按同一口径
处理）。判定只依赖会话固化的工具集（同一会话内恒定），不按"本轮是否含图"这类逐轮状态，
因此不会破坏前缀缓存。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.run.chat import (  # noqa: E402
    VISION_ANALYZE_GUIDE,
    VISION_ANALYZE_LOAD_GUIDE,
    VISION_OPS_GUIDE,
    vision_prompt_sections,
)
from naiba.skills.agent import comfyui_script_guide_enabled  # noqa: E402


class VisionPromptGatingTests(unittest.TestCase):
    def test_no_vision_tools_no_section(self):
        for has_vision in (False, True):
            with self.subTest(model_has_vision=has_vision):
                self.assertEqual(
                    vision_prompt_sections({"read_file", "pwsh"}, model_has_vision=has_vision), [])

    def test_text_model_gets_analyze_wording(self):
        parts = vision_prompt_sections({"vision_analyze"}, model_has_vision=False)
        self.assertEqual(parts, [VISION_ANALYZE_GUIDE])
        self.assertNotIn("vision_image_ops", "".join(parts))
        self.assertIn("需要了解附件/上下文中图片的内容时", "".join(parts))
        self.assertNotIn("装入本次对话", "".join(parts), "分析形态不得写成装载语义")

    def test_multimodal_model_gets_load_wording(self):
        """vision_analyze 的 schema 按模型能力分流（分析/装载），文案必须同口径。"""
        parts = vision_prompt_sections({"vision_analyze"}, model_has_vision=True)
        self.assertEqual(parts, [VISION_ANALYZE_LOAD_GUIDE])
        self.assertIn("装入本次对话", "".join(parts), "多模态形态应说明是把图片装入对话")
        self.assertNotIn("视觉后端", "".join(parts))

    def test_analyze_plus_ops_appends_ops_sentence(self):
        parts = vision_prompt_sections({"vision_analyze", "vision_image_ops"}, model_has_vision=False)
        self.assertEqual(parts, [VISION_ANALYZE_GUIDE, VISION_OPS_GUIDE])
        self.assertEqual("".join(parts), (
            "图片处理策略：需要了解附件/上下文中图片的内容时，调用 vision_analyze 工具并传入图片路径。"
            "仅当用户明确要求裁剪、OCR、坐标、像素比较等新操作时才调用 vision_image_ops。"
        ))

    def test_text_guide_has_no_multimodal_clause(self):
        """文本模型看不到原图，不该出现"（多模态模型）无需调用"这类描述别的模型的从句。"""
        self.assertNotIn("多模态模型", VISION_ANALYZE_GUIDE)
        self.assertNotIn("无需调用", VISION_ANALYZE_GUIDE)

    def test_load_plus_ops_appends_ops_sentence(self):
        parts = vision_prompt_sections({"vision_analyze", "vision_image_ops"}, model_has_vision=True)
        self.assertEqual(parts, [VISION_ANALYZE_LOAD_GUIDE, VISION_OPS_GUIDE])

    def test_ops_only_gets_standalone_sentence(self):
        for has_vision in (False, True):
            with self.subTest(model_has_vision=has_vision):
                parts = vision_prompt_sections({"vision_image_ops"}, model_has_vision=has_vision)
                self.assertEqual(len(parts), 1, "只开 ops 也要给一句，否则模型不知道它何时用")
                self.assertTrue(parts[0].startswith("图片处理策略："))
                self.assertIn("vision_image_ops", parts[0])
                self.assertNotIn("vision_analyze", parts[0])


class ComfyuiGuideGatingTests(unittest.TestCase):
    def test_requires_comfyui_tool(self):
        self.assertFalse(comfyui_script_guide_enabled({"write_file", "pwsh", "run_in_background"}))
        self.assertFalse(comfyui_script_guide_enabled({"read_file", "vision_analyze"}))

    def test_builtin_comfyui_tools_enable_guide(self):
        self.assertTrue(comfyui_script_guide_enabled({"comfyui_batch", "pwsh"}))
        self.assertTrue(comfyui_script_guide_enabled({"comfyui_prepare_workflow", "write_file"}))

    def test_mcp_comfy_tool_enables_guide(self):
        self.assertTrue(comfyui_script_guide_enabled(
            {"mcp__comfy-mcp__run_workflow", "run_in_background"}))

    def test_requires_script_path_tools_too(self):
        self.assertFalse(comfyui_script_guide_enabled({"comfyui_batch", "read_file"}),
                         "指引引用了 write_file/pwsh/run_in_background，缺了就不该提")

    def test_unrelated_mcp_tool_does_not_enable(self):
        self.assertFalse(comfyui_script_guide_enabled({"mcp__other__ping", "pwsh"}))


class VisionVariantConsistencyTests(unittest.TestCase):
    """文案与 schema 必须同口径：装载形态的 schema 说「装入本次对话」，文案也得这么说。"""

    def test_guide_matches_schema_variant_wording(self):
        from naiba.tools.registry import build_vision_tool_specs, vision_analyze_load_variant

        base = next(spec for spec in build_vision_tool_specs() if spec.name == "vision_analyze")
        load = vision_analyze_load_variant(base)
        self.assertIn("装入本次对话", load.description, "多模态形态的 schema 是装载语义")
        self.assertIn("分析本地图片", base.description, "文本形态的 schema 是分析语义")
        self.assertIn("装入本次对话", VISION_ANALYZE_LOAD_GUIDE, "装载文案必须与装载 schema 同口径")
        self.assertNotIn("装入本次对话", VISION_ANALYZE_GUIDE, "分析文案不得写成装载语义")


class PromptSourceWiringTests(unittest.TestCase):
    """源码级：两处提示必须走上面的判定函数，不得再无条件拼接。"""

    def test_chat_uses_vision_sections(self):
        source = (ROOT / "naiba/run/chat.py").read_text(encoding="utf-8")
        self.assertIn("vision_prompt_sections(", source)
        self.assertIn("if vision_sections:", source, "视觉段必须条件拼接")
        self.assertIn("model_has_vision=bool(brain_supports_images)", source,
                      "视觉文案必须按会话模型能力分流（与 session_tool_defs 同口径）")

    def test_agent_uses_comfyui_gate(self):
        source = (ROOT / "naiba/skills/agent.py").read_text(encoding="utf-8")
        self.assertIn("if comfyui_script_guide_enabled(allowed):", source)
        self.assertNotIn('if {"write_file", "pwsh", "run_in_background"} & allowed:', source,
                         "旧的“有写文件/命令就提 ComfyUI”条件不得复活")


if __name__ == "__main__":
    unittest.main()
