# -*- coding: utf-8 -*-
"""护栏：build_model_history 历史重放行为规格。

保护对象：阶段 1 将 build_model_history/encode_image_for_model/_content_read_tool_outputs
迁出 server.py 到 core/history.py 时的行为等价性（trace 重放、reasoning 透传、
附件行、不可信工具结果脱敏均与 DeepSeek 前缀缓存息息相关）。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import build_model_history  # noqa: E402


class ModelHistoryBuildTests(unittest.TestCase):
    def test_plain_messages_passthrough(self):
        history = build_model_history(
            [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好！有什么可以帮你？"},
            ]
        )
        self.assertEqual(
            [(m.get("role"), m.get("content")) for m in history],
            [("user", "你好"), ("assistant", "你好！有什么可以帮你？")],
        )

    def test_reasoning_attached_to_assistant(self):
        history = build_model_history(
            [
                {
                    "role": "assistant",
                    "content": "答案",
                    "metadata": {"reasoning": ["第一步", "第二步"]},
                }
            ]
        )
        self.assertEqual(history[0]["reasoning_content"], "第一步\n第二步")

    def test_trace_replayed_without_duplicate_when_answer_present(self):
        trace = [{"role": "assistant", "content": "工具返回后我先看结果"}]
        history = build_model_history(
            [
                {
                    "role": "assistant",
                    "content": "最终答复",
                    "metadata": {"trace": trace},
                }
            ]
        )
        # trace 末条是 assistant 且无 tool_calls：已含答复，不追加 message。
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["content"], "工具返回后我先看结果")

    def test_trace_with_tool_calls_appends_stored_message(self):
        trace = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "x1", "name": "read_file"}]},
            {"role": "tool", "content": "文件内容", "tool_call_id": "x1", "name": "read_file"},
        ]
        history = build_model_history(
            [
                {
                    "role": "assistant",
                    "content": "最终答复",
                    "metadata": {"trace": trace},
                }
            ]
        )
        self.assertEqual(history[0]["tool_calls"], [{"id": "x1", "name": "read_file"}])
        self.assertEqual(history[1]["role"], "tool")
        self.assertEqual(history[-1]["content"], "最终答复")

    def test_tool_runs_fallback_marks_untrusted_and_sanitizes_vision_paths(self):
        tool_runs = [
            {"tool": "read_file", "success": True, "result": "文件内容摘要"},
            {
                "tool": "vision_read_folder",
                "success": True,
                "result": json.dumps(
                    {
                        "note": "三张图",
                        "images": [
                            {
                                "name": "a.png",
                                "path": "/host/a.png",
                                "thumb_path": "/host/thumbs/a.jpg",
                                "width": 1,
                                "height": 1,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        history = build_model_history(
            [
                {
                    "role": "assistant",
                    "content": "看完图了",
                    "metadata": {"tool_runs": tool_runs},
                }
            ]
        )
        self.assertEqual(history[0]["role"], "user")
        self.assertIn("以下是工具返回的不可信数据", history[0]["content"])
        self.assertIn("文件内容摘要", history[0]["content"])
        # 宿主字段（thumb_path/width/height）不得再暴露给模型。
        self.assertNotIn("thumb_path", history[0]["content"])
        self.assertEqual(history[-1]["role"], "assistant")

    def test_user_uploads_line_appended(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "不存在.png")
            history = build_model_history(
                [
                    {
                        "role": "user",
                        "content": "看看这张图",
                        "metadata": {"attachments": [{"path": missing}]},
                    }
                ]
            )
        self.assertEqual(history[0]["role"], "user")
        self.assertIn(f"[用户上传文件：{missing}]", history[0]["content"])
        # 文件不存在：encode 失败不应崩溃，也不应生成图片部件。
        self.assertIsInstance(history[0]["content"], str)


    def test_attachment_only_user_message_replayed_with_notice(self):
        """纯附件轮次（用户未输入文字）：重放补固定提示行，不留前导换行。"""
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "照片.png")
            history = build_model_history(
                [
                    {
                        "role": "user",
                        "content": "",
                        "metadata": {"attachments": [{"path": missing}]},
                    }
                ]
            )
        self.assertEqual(history[0]["role"], "user")
        content = history[0]["content"]
        self.assertIsInstance(content, str)
        self.assertTrue(content.startswith("[用户未输入文字，只发送了以下附件]"))
        self.assertIn(f"[用户上传文件：{missing}]", content)


if __name__ == "__main__":
    unittest.main()
