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
