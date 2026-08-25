from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from model_runtime import ModelRuntime


class DiagnosticsParsingTests(unittest.TestCase):
    def test_lm_studio_stats_are_normalized(self):
        self.assertEqual(
            {
                "input_tokens": 12,
                "output_tokens": 7,
                "total_tokens": 19,
                "cached_tokens": 0,
            },
            ModelRuntime._online_usage(
                "lm_studio",
                {"stats": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19}},
            ),
        )

    def test_lm_studio_chat_end_stats_are_read_from_sse(self):
        lines = [
            b'data: {"type":"message.delta","content":"ok"}\n',
            b'data: {"type":"chat.end","stats":{"input_tokens":4,"output_tokens":2}}\n',
            b'data: [DONE]\n',
        ]

        class Response:
            def __iter__(self):
                return iter(lines)

        result = ModelRuntime._read_lm_studio_stream(Response(), None)
        self.assertEqual("ok", result["content"])
        self.assertEqual(4, result["usage"]["input_tokens"])
        self.assertEqual(2, result["usage"]["output_tokens"])

    def test_openai_streams_reasoning_events_incrementally(self):
        lines = [
            'data: {"choices":[{"delta":{"reasoning_content":"想一下A"}}]}\n'.encode("utf-8"),
            'data: {"choices":[{"delta":{"reasoning_content":"想一下B"}}]}\n'.encode("utf-8"),
            'data: {"choices":[{"delta":{"content":"答案"}}]}\n'.encode("utf-8"),
            b'data: [DONE]\n',
        ]

        class Response:
            def __iter__(self):
                return iter(lines)

        events = []
        result = ModelRuntime._read_sse_response(Response(), "openai_chat", events.append)
        self.assertEqual("答案", result["content"])
        self.assertEqual("想一下A想一下B", result["reasoning"])
        reasoning = [e for e in events if e.get("type") in ("reasoning_start", "reasoning_delta", "reasoning_end")]
        self.assertEqual(
            ["reasoning_start", "reasoning_delta", "reasoning_delta", "reasoning_end"],
            [e["type"] for e in reasoning],
        )
        self.assertEqual(
            ["想一下A", "想一下B"],
            [e["content"] for e in reasoning if e["type"] == "reasoning_delta"],
        )

    def test_codex_responses_streams_reasoning_events_incrementally(self):
        lines = [
            'data: {"type":"response.reasoning_text.delta","delta":"先查"}\n'.encode("utf-8"),
            'data: {"type":"response.output_text.delta","delta":"结果"}\n'.encode("utf-8"),
            b'data: {"type":"response.completed"}\n',
        ]

        class Response:
            def __iter__(self):
                return iter(lines)

        events = []
        result = ModelRuntime._read_sse_response(Response(), "codex_responses", events.append)
        self.assertEqual("结果", result["content"])
        self.assertEqual("先查", result["reasoning"])
        reasoning = [e for e in events if e.get("type") in ("reasoning_start", "reasoning_delta", "reasoning_end")]
        self.assertEqual(["reasoning_start", "reasoning_delta", "reasoning_end"], [e["type"] for e in reasoning])
        self.assertEqual(["先查"], [e["content"] for e in reasoning if e["type"] == "reasoning_delta"])

    def test_stream_without_reasoning_emits_no_reasoning_events(self):
        # 兜底：模型不暴露思考时，不产生思考事件、也不崩。
        lines = [
            'data: {"choices":[{"delta":{"content":"你好"}}]}\n'.encode("utf-8"),
            b'data: [DONE]\n',
        ]

        class Response:
            def __iter__(self):
                return iter(lines)

        events = []
        result = ModelRuntime._read_sse_response(Response(), "openai_chat", events.append)
        self.assertEqual("你好", result["content"])
        self.assertEqual("", result["reasoning"])
        self.assertEqual([], [e for e in events if e.get("type") in ("reasoning_start", "reasoning_delta", "reasoning_end")])

    def test_stream_options_are_added_to_openai_payload(self):
        class Response:
            headers = {"Content-Type": "text/event-stream"}

            def __iter__(self):
                return iter([b'data: {"choices":[{"delta":{"content":"ok"}}]}\n', b'data: [DONE]\n'])

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        request = {}

        def open_url(req, *_args, **_kwargs):
            request["payload"] = json.loads(req.data.decode())
            return Response()

        profile = {
            "kind": "local",
            "request_format": "llama_cpp",
            "base_url": "http://127.0.0.1:8080",
            "model": "vision",
        }
        with patch("model_runtime.ModelRuntime._urlopen_cancelable", side_effect=open_url):
            ModelRuntime().complete(profile, [{"role": "user", "content": "hi"}], {"stream": True})
        self.assertEqual({"include_usage": True}, request["payload"]["stream_options"])


if __name__ == "__main__":
    unittest.main()
