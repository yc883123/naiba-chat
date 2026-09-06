# -*- coding: utf-8 -*-
"""护栏：naiba/llm/protocols 协议适配行为规格（收官线 ① 第二件）。

保护对象：各 request_format 的 wire 序列化/解析自 ModelRuntime 迁入 ProtocolMixins 时的
行为等价（MRO 委派）。全部用例直接以 ProtocolMixins 静态方法调用（不经网络）。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.llm.protocols import ProtocolMixins as P  # noqa: E402


class LlmProtocolTests(unittest.TestCase):
    def test_openai_messages_basic(self):
        out = P._openai_messages([{"role": "user", "content": "你好"}, {"role": "assistant", "content": "好"}])
        self.assertEqual(out, [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "好"}])

    def test_openai_messages_tool_and_reasoning(self):
        out = P._openai_messages([
            {"role": "tool", "tool_call_id": "t1", "content": "结果"},
            {"role": "assistant", "content": "", "reasoning_content": "在想",
             "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"path": "a"}}]},
        ], include_reasoning_content=True)
        self.assertEqual(out[0]["role"], "tool")
        self.assertEqual(out[0]["tool_call_id"], "t1")
        self.assertEqual(out[1]["reasoning_content"], "在想")
        self.assertEqual(out[1]["tool_calls"][0]["function"]["name"], "read_file")

    def test_openai_content_image_part(self):
        out = P._openai_content([{"type": "text", "text": "看图"},
                                 {"type": "image", "media_type": "image/png", "data": "QQ=="}])
        self.assertEqual(out[1]["type"], "image_url")
        self.assertTrue(out[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_responses_input(self):
        out = P._responses_input([
            {"role": "tool", "tool_call_id": "c1", "content": "out"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "name": "pwsh", "arguments": {"command": "dir"}}]},
        ])
        self.assertEqual(out[0]["type"], "function_call_output")
        self.assertEqual(out[0]["call_id"], "c1")
        self.assertEqual(out[1]["type"], "function_call")

    def test_responses_input_reasoning_text_passback(self):
        """回归：DeepSeek 思考模式（携带 tools）要求历史 assistant 轮以 reasoning_text
        字段回传（官方规范），缺失会 400 "The reasoning_text must be passed back"。"""
        # 纯对话 assistant 轮：message item 带 reasoning_text
        out = P._responses_input([
            {"role": "assistant", "content": "回答", "reasoning_content": "思考中"},
        ])
        self.assertEqual(out[0]["reasoning_text"], "思考中")
        self.assertEqual(out[0]["role"], "assistant")
        # tool_calls 轮：assistant 消息（reasoning_text 随行）在前，function_call 紧随
        # （function_call 与 function_call_output 必须相邻配对，否则 "No tool output found"）
        out2 = P._responses_input([
            {"role": "assistant", "content": "",
             "reasoning": "先调用工具",
             "tool_calls": [{"id": "c1", "name": "pwsh", "arguments": {"command": "dir"}}]},
        ])
        self.assertEqual(out2[0]["role"], "assistant")
        self.assertEqual(out2[0]["reasoning_text"], "先调用工具")
        self.assertEqual(out2[1]["type"], "function_call")
        self.assertEqual(out2[1]["call_id"], "c1")
        # 无 reasoning 时不携带空字段
        out3 = P._responses_input([{"role": "assistant", "content": "回答"}])
        self.assertNotIn("reasoning_text", out3[0])
        self.assertEqual(out3[0].get("role"), "assistant")

    def test_tool_schemas_all_formats(self):
        rows = [{"name": "read_file", "description": "读", "parameters": {"type": "object", "properties": {}}}]
        self.assertEqual(P._tool_schemas(rows, "openai_chat")[0]["type"], "function")
        self.assertEqual(P._tool_schemas(rows, "claude")[0]["input_schema"]["type"], "object")
        self.assertEqual(P._tool_schemas(rows, "codex_responses")[0]["name"], "read_file")
        self.assertEqual(P._tool_schemas(rows, "gemini")[0]["name"], "read_file")

    def test_ollama_messages(self):
        out = P._ollama_messages([{"role": "user", "content": "hi"}])
        self.assertEqual(out[0]["role"], "user")

    def test_gemini_message(self):
        out = P._gemini_message({"role": "user", "content": "hi"})
        self.assertIn("role", out)
        self.assertIn("parts", out)

    def test_claude_message_and_cache_control(self):
        messages = [{"role": "user", "content": "最后一条"}]
        P._claude_apply_cache_control(messages)
        self.assertEqual(messages[0]["content"][-1].get("cache_control"), {"type": "ephemeral"})

    def test_reasoning_params(self):
        params = P._reasoning_params("openai_chat", "high", deepseek=True)
        self.assertIsInstance(params, dict)
        self.assertIn("reasoning_effort", params)

    def test_with_endpoint_and_local_endpoint(self):
        self.assertEqual(P._with_endpoint("https://api.openai.com/v1", "/chat/completions"),
                         "https://api.openai.com/v1/chat/completions")
        # 本地端点：剥离余留 /v1 后再拼路径（ollama/lm_studio 基础 URL 习惯带 /v1）。
        self.assertEqual(P._local_endpoint("http://127.0.0.1:11434/v1", "/api/chat"),
                         "http://127.0.0.1:11434/api/chat")
        self.assertEqual(P._local_endpoint("http://127.0.0.1:1234", "/v1/chat/completions"),
                         "http://127.0.0.1:1234/v1/chat/completions")

    def test_online_usage_variants(self):
        self.assertEqual(P._online_usage("openai_chat", {
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}),
            {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "cached_tokens": 0})
        claude = P._online_usage("claude", {"usage": {
            "input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 2}})
        self.assertEqual(claude["input_tokens"], 15)
        self.assertEqual(claude["total_tokens"], 20)
        self.assertEqual(P._online_usage("ollama", {"prompt_eval_count": 8, "eval_count": 4}),
                         {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12, "cached_tokens": 0})

    def test_stream_delta_formats(self):
        self.assertEqual(P._stream_delta("openai_chat", {"choices": [{"delta": {"content": "a"}}]}),
                         ("a", ""))
        self.assertEqual(P._stream_delta("claude", {"type": "content_block_delta",
                                                    "delta": {"text": "b", "thinking": "t"}}),
                         ("b", "t"))
        self.assertEqual(P._stream_delta("codex_responses", {"type": "response.output_text.delta",
                                                             "delta": "c"}), ("c", ""))

    def test_reasoning_action_validation(self):
        self.assertEqual(P._reasoning_action('```json\n{"type": "tool", "tool": "pwsh", "arguments": {}}\n```'),
                         '{"type": "tool", "tool": "pwsh", "arguments": {}}')
        self.assertEqual(P._reasoning_action("普通推理文本"), "")
        self.assertEqual(P._reasoning_action('{"type": "tool", "tool": "", "arguments": {}}'), "")

    def test_content_helpers(self):
        self.assertEqual(P._content_text([{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]), "A\nB")
        self.assertEqual(P._text_value("x"), "x")


if __name__ == "__main__":
    unittest.main()
