# -*- coding: utf-8 -*-
"""护栏：ComfyUI 反幻觉守卫（引用历史证据豁免 + 系统校验文案不冒充用户）。

修复的误判场景（用户实测复现）：
- 模型回复"复述了前几轮已成功提交的 Job ID"（或 ComfyUI 地址/版本）——这是历史真实证据，
  旧守卫只看"本轮 runs"，把引用历史判成编造 → 撤回回复 + 注入"你刚才声称…"的
  role=user 校正消息（模型误以为用户质疑）+ 自动重试（画面一闪、自动新一轮）。
- 校正文本曾断言"视为从未提交，禁止使用任务 ID"——对真实事实是破坏性误判。

保护对象：
- content 中引用的 ID/prompt_id/UUID/URL/版本号在历史证据中出现 → 豁免（不算幻觉）；
- 真正编造（ID 无任何出处）→ 仍然判为幻觉；
- 校正文案以 [系统校验（非用户消息）] 开头、不包含"视为从未提交"断言。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.skills.agent import SkillAgent  # noqa: E402

JOB_ID = "995ae103b0514986a1de445bdd68c53e"
FAKE_ID = "ffffffff00000000aaaaaaaa11111111"
HISTORY_TEXT = (
    "✅ 已成功提交 `sd文生图简单版 (2).json`： Job ID：995ae103b0514986a1de445bdd68c53e\n"
    "ComfyUI 探测完成，接口正常可用：地址 http://127.0.0.1:8188，版本 0.34.0，HTTP 200"
)


class ComfyUiGuardTests(unittest.TestCase):
    # ---- 提交守卫 ----
    def test_quoting_historical_job_id_is_exempt(self) -> None:
        content = "上一轮已提交成功，Job ID：995ae103b0514986a1de445bdd68c53e。"
        claim = SkillAgent._unsupported_comfyui_submission_claim(
            "改个种子再交一次", content, [], evidence_text=HISTORY_TEXT
        )
        self.assertFalse(claim, "引用历史真实 Job ID 不应判为幻觉")

    def test_fabricated_job_id_is_still_flagged(self) -> None:
        content = f"已提交任务，Job ID：{FAKE_ID}。"
        claim = SkillAgent._unsupported_comfyui_submission_claim(
            "提交sd文生图工作流", content, [], evidence_text=HISTORY_TEXT
        )
        self.assertTrue(claim, "无来源的编造 ID 必须仍被判定为幻觉")

    def test_this_round_proof_is_exempt(self) -> None:
        runs = [{"tool": "comfyui_batch", "success": True, "result": '{"job_id": "x"}'}]
        claim = SkillAgent._unsupported_comfyui_submission_claim(
            "提交sd文生图工作流", "已提交，Job ID：x", runs, evidence_text=""
        )
        self.assertFalse(claim)

    # ---- 连接守卫 ----
    def test_quoting_historical_connection_is_exempt(self) -> None:
        content = "ComfyUI 已连接，地址 http://127.0.0.1:8188，版本 0.34.0。"
        claim = SkillAgent._unsupported_comfyui_connection_claim(
            "检查ComfyUI", content, [], evidence_text=HISTORY_TEXT
        )
        self.assertFalse(claim, "引用历史探测结果（URL/版本）不应判为幻觉")

    def test_unverified_connection_is_still_flagged(self) -> None:
        claim = SkillAgent._unsupported_comfyui_connection_claim(
            "检查ComfyUI", "ComfyUI 已连接并正常运行。", [], evidence_text=""
        )
        self.assertTrue(claim, "无证据的连接结论必须仍被判定为幻觉")

    # ---- 证据覆盖辅助 ----
    def test_evidence_covered_identifier_and_connection(self) -> None:
        self.assertTrue(SkillAgent._comfyui_evidence_covered(
            f"Job ID：{JOB_ID}", HISTORY_TEXT
        ))
        self.assertTrue(SkillAgent._comfyui_evidence_covered(
            "http://127.0.0.1:8188 0.34.0", HISTORY_TEXT
        ))
        self.assertFalse(SkillAgent._comfyui_evidence_covered(
            f"Job ID：{FAKE_ID}", HISTORY_TEXT
        ))

    # ---- 校正文案纪律 ----
    def test_correction_copy_is_system_validation_not_user_voice(self) -> None:
        # 文案散落在 _run_active：断言当前源码不含旧的"冒充用户/破坏性断言"字眼。
        import inspect
        import naiba.skills.agent as agent_mod

        source = inspect.getsource(agent_mod)
        self.assertNotIn("你刚才声称已提交", source)
        self.assertNotIn("视为从未提交，禁止使用", source)
        self.assertIn("[系统校验（非用户消息）]", source)


if __name__ == "__main__":
    unittest.main()
