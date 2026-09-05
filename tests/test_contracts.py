# -*- coding: utf-8 -*-
"""契约守卫：RunContext / EventType / MetadataKeys 的一致性校验。

1. EventType 必须覆盖前端可处置的全部事件 type（从 public/app.js handleChatEvent 反查），
   防止前后端事件协议漂移（wire 兼容由本测试钉死，后端新增事件必须先入枚举）。
2. RunContext 契约键存在性 + 构造样本可运行（TypedDict 运行时即 dict）。
3. MetadataKeys 与 core/history.py 读取的 key 保持一致（读者已常量化的部分）。
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core.contracts import EventType, MetadataKeys, RunContext  # noqa: E402


class EventContractTests(unittest.TestCase):
    def test_frontend_handled_events_subset_of_enum(self):
        js = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        handle = js[js.index("function handleChatEvent"):]
        frontend_types = set(re.findall(r"""event\.type === ['"]([a-z_]+)['"]""", handle))
        self.assertGreaterEqual(len(frontend_types), 20, "前端事件分支解析异常，请检查 app.js")
        missing = sorted(t for t in frontend_types if t not in EventType.__members__.values())
        self.assertEqual(missing, [], f"前端可处置的事件 type 不在 EventType 枚举中：{missing}")

    def test_run_context_contract_keys(self):
        canonical = {
            "run_id", "job_id", "conversation_id", "owner_session_id", "parent_job_id",
            "depth", "allowed_tools", "skill_policy", "job_registry", "executor",
            "cancel_event", "vision_budget", "interaction_mode", "routing_message",
            "pull_interjections", "mark_interjections_consumed",
        }
        annotations = set(RunContext.__annotations__)
        self.assertTrue(canonical <= annotations, f"RunContext 缺失契约键：{canonical - annotations}")

    def test_run_context_sample_is_runtime_dict(self):
        ctx: RunContext = {
            "run_id": "r1",
            "conversation_id": "c1",
            "owner_session_id": "c1",
            "depth": 0,
            "allowed_tools": ["read_file"],
            "interaction_mode": "craft",
            "routing_message": "hello",
        }
        # TypedDict 运行时就是 dict：消费方现有 .get 语义不变。
        self.assertIsInstance(ctx, dict)
        self.assertEqual(ctx["run_id"], "r1")

    def test_metadata_keys_readers_use_constants(self):
        history_src = (ROOT / "naiba" / "core" / "history.py").read_text(encoding="utf-8")
        self.assertIn("MetadataKeys.ATTACHMENTS", history_src)
        self.assertIn("MetadataKeys.TRACE", history_src)
        self.assertIn("MetadataKeys.TOOL_RUNS", history_src)
        self.assertIn("MetadataKeys.REASONING", history_src)
        # 键值必须与契约一致（若改契约值，此处立即失败）。
        self.assertEqual(MetadataKeys.ATTACHMENTS, "attachments")
        self.assertEqual(MetadataKeys.TRACE, "trace")


if __name__ == "__main__":
    unittest.main()
