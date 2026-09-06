# -*- coding: utf-8 -*-
"""护栏：ComfyUI 防幻觉守卫已整体移除，防幻觉由「事实回环 + 系统法规」承担。

决策依据（用户实测 + 全库取证，2026-09-07）：
- 守卫在全库 253 会话中正确拦截 0 次、误判 2 次（把"引用历史真实 Job ID/连接证据"
  判为编造 → 撤回回复 + role=user 校正消息冒充用户 + 自动 retry，
  模型误以为用户质疑，正常轮次被拖去执行真实提交）；
- 现有框架下模型无从编造成功：一切 ID/状态只能来自工具返回；编造 ID 会立即被
  job_status/job_output/job_wait 的"不存在/无从访问"事实戳穿（天然闭环）。

保护对象：守卫函数与校正注入不存在；系统提示常驻法规存在；job 状态查询的
"不存在"提示（事实回环端点）仍在。
"""

import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import naiba.skills.agent as agent_mod  # noqa: E402
from naiba.skills.agent import SkillAgent  # noqa: E402


class GuardRemovedByTruthLoopTests(unittest.TestCase):
    def test_guard_machinery_is_removed(self) -> None:
        for name in (
            "_unsupported_comfyui_submission_claim",
            "_unsupported_comfyui_connection_claim",
            "_comfyui_evidence_covered",
        ):
            with self.subTest(method=name):
                self.assertFalse(hasattr(SkillAgent, name), f"{name} 应已整体移除")

    def test_source_has_no_guard_correction_copy(self) -> None:
        source = inspect.getsource(agent_mod)
        self.assertNotIn("你刚才声称已提交", source)
        self.assertNotIn("视为从未提交", source)
        self.assertNotIn("[系统校验（非用户消息）]", source)

    def test_truth_loop_rule_present_in_system_prompt(self) -> None:
        source = inspect.getsource(agent_mod)
        self.assertIn("未经验证的状态", source, "系统提示须含任务事实纪律法规")
        self.assertIn("先用 job_status", source)

    def test_job_status_missing_job_truth_loop_endpoint(self) -> None:
        """事实回环端点仍在：查询不存在的 Job 必须明确返回"不存在/无从访问"。"""
        from naiba.subagent import job_tool_handler_factory

        class _MissingJobs:
            def get(self, job_id):
                return None

            def read(self, job_id, cursor=0):
                raise AssertionError("不应尝试读取不存在的 Job")

        from types import SimpleNamespace

        app = SimpleNamespace(jobs=_MissingJobs())
        handler = job_tool_handler_factory(app)["job_status"]
        ok, out = handler({"job_id": "no_such_job"}, [], None)
        self.assertFalse(ok)
        self.assertIn("不存在", out)


if __name__ == "__main__":
    unittest.main()
