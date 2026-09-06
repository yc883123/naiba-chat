# -*- coding: utf-8 -*-
"""护栏：naiba/llm/stream 流解析与推理流行为规格（收官线 ① 第三件）。

保护对象：SSE/Ollama/LM Studio 流解析、<think> 推理流、Agent 工具协议守卫与
缓冲收尾自 ModelRuntime 迁入 StreamMixins 时的行为等价（MRO 委派，纯解析不触网）。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.llm.stream import (  # noqa: E402
    StreamMixins,
    _InlineReasoningParser,
    _ReasoningStreamer,
)


class StreamGuardTests(unittest.TestCase):
    def test_classify_text_vs_tool(self):
        self.assertEqual(StreamMixins._classify_agent_output("普通正文"), "text")
        self.assertEqual(StreamMixins._classify_agent_output('{"type": "tool", "tool": "pwsh"}'), "tool")
        self.assertEqual(StreamMixins._classify_agent_output('<tool name="pwsh">x</tool>'), "tool")

    def test_tool_protocol_offset(self):
        self.assertEqual(StreamMixins._tool_protocol_offset('<tool name="x">'), 0)
        self.assertIsNone(StreamMixins._tool_protocol_offset("纯文本内容"))

    def test_possible_protocol_suffix_length(self):
        self.assertEqual(StreamMixins._possible_protocol_suffix_length(""), 0)
        self.assertEqual(StreamMixins._possible_protocol_suffix_length("<to"), 3)
        self.assertEqual(StreamMixins._possible_protocol_suffix_length("<tool"), 5)
        # <think 由 _InlineReasoningParser 处理，不属于工具协议守卫 token。
        self.assertEqual(StreamMixins._possible_protocol_suffix_length("<thi"), 0)

    def test_clean_content_strips_think_blocks(self):
        # 语义：保留最后一个 </think> 之后的可见文本，其余 think 块剔除。
        self.assertEqual(StreamMixins._clean_content("前文<think>推理</think>答案"), "答案")
        self.assertEqual(StreamMixins._clean_content("  普通文本  \n"), "普通文本")


class InlineReasoningTests(unittest.TestCase):
    def test_think_split_across_feeds(self):
        parser = _InlineReasoningParser()
        self.assertEqual(parser.feed("<think>推理中"), ("", "推理中"))
        self.assertEqual(parser.feed("</think>正文"), ("正文", ""))

    def test_plain_text_passthrough(self):
        parser = _InlineReasoningParser()
        self.assertEqual(parser.feed("你好"), ("你好", ""))

    def test_final_flushes_reasoning(self):
        parser = _InlineReasoningParser()
        self.assertEqual(parser.feed("<think>未完", final=True), ("", "未完"))


class ReasoningStreamerTests(unittest.TestCase):
    def test_live_stream_events(self):
        events = []
        parts = []
        streamer = _ReasoningStreamer(events.append, parts)
        streamer.feed("第一段")
        streamer.feed("第二段")
        streamer.finish()
        self.assertEqual([e.get("type") for e in events],
                         ["reasoning_start", "reasoning_delta", "reasoning_delta", "reasoning_end"])

    def test_fallback_oneshot_for_precollected_parts(self):
        events = []
        streamer = _ReasoningStreamer(events.append, ["兜底思考"])
        streamer.finish()
        self.assertEqual([e.get("type") for e in events],
                         ["reasoning_start", "reasoning_delta", "reasoning_end"])
        self.assertEqual(events[1]["content"], "兜底思考")


class StreamReaderTests(unittest.TestCase):
    def test_read_ollama_stream_smoke(self):
        events = []
        response = [
            '{"message": {"content": "你好"}}'.encode("utf-8"),
            '{"message": {"thinking": "想一下"}, "prompt_eval_count": 3, "eval_count": 2}'.encode("utf-8"),
        ]
        result = StreamMixins._read_ollama_stream(response, events.append)
        self.assertEqual(result["content"], "你好")
        self.assertEqual(result["reasoning"], "想一下")
        self.assertEqual(result["usage"], {"input_tokens": 3, "output_tokens": 2,
                                           "total_tokens": 5, "cached_tokens": 0})

    def test_read_sse_response_smoke(self):
        events = []
        response = [
            'data: {"choices": [{"delta": {"content": "回答"}}]}'.encode("utf-8"),
            b"data: [DONE]",
        ]
        result = StreamMixins._read_sse_response(response, "openai_chat", events.append)
        self.assertEqual(result["content"], "回答")
        self.assertEqual(result["reasoning"], "")
        self.assertTrue(any(e.get("type") == "delta" for e in events))


if __name__ == "__main__":
    unittest.main()
