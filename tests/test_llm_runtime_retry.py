"""守门：在线模型 HTTP 5xx 必须退避重试 + 失败请求体落盘（2026-09-09 实测教训）。

背景：一次 HTTP 500（响应体只有 ``Internal Server Error``，没有结构化错误信息）直接终止
了整轮对话；18 秒后重发同样的请求即成功（供应商侧瞬时故障）。原实现只对 429/502/503/504
重试、且只在「思考回传类 400」时落盘请求体，于是 5xx 既不自愈、也无任何取证。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from unittest import mock

from naiba.llm.runtime import ERROR_DUMP_FILENAME, ModelRuntime

PROFILE = {
    "kind": "online",
    "name": "deepseek",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-v4-flash",
    "request_format": "codex_responses",
    "api_key": "sk-should-never-be-dumped",
}
MESSAGES = [{"role": "user", "content": "ping"}]


def http_error(code: int, body: str = "", reason: str = "Internal Server Error") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.deepseek.com/responses",
        code,
        reason,
        {"Content-Type": "application/json"},
        io.BytesIO(body.encode("utf-8")),
    )


class FakeResponse:
    """够用的 urllib 响应替身：非流式路径只用到 read()/headers。"""

    def __init__(self, payload: dict) -> None:
        self._data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.headers = {"Content-Type": "application/json"}

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        return None

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class OnlineRetryTests(unittest.TestCase):
    def _run(
        self,
        outcomes: list,
        status=None,
        messages: list | None = None,
        dump_dir: str | None = None,
    ) -> tuple[list, str, BaseException | None]:
        """outcomes：按调用顺序生效，异常实例→抛出，dict→返回该响应（末项重复）。

        dump_dir 缺省用独立临时目录，避免把取证文件写进仓库工作区。
        """
        calls: list = []
        if dump_dir is None:
            dump_dir = tempfile.mkdtemp(prefix="naiba-retry-test-")
            self.addCleanup(shutil.rmtree, dump_dir, ignore_errors=True)

        def fake_open(request, timeout, cancel_event=None, opener=None):
            calls.append(request)
            outcome = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            if isinstance(outcome, BaseException):
                raise outcome
            return FakeResponse(outcome)

        with mock.patch.object(ModelRuntime, "_urlopen_cancelable", fake_open), \
                mock.patch("naiba.llm.runtime.time.sleep"), \
                mock.patch.dict(os.environ, {"NAIBA_ERROR_DUMP_DIR": dump_dir}, clear=False):
            try:
                content = ModelRuntime().complete(
                    PROFILE, messages or MESSAGES, {"stream": False}, status)
                error = None
            except RuntimeError as exc:
                content, error = "", exc
        return calls, content, error

    def test_http_500_is_retried_then_raises(self) -> None:
        events: list[dict] = []
        calls, _content, error = self._run([http_error(500)] * 3, status=events.append)
        self.assertEqual(len(calls), 3, "HTTP 500 必须重试到次数上限")
        self.assertIsNotNone(error)
        self.assertIn("HTTP 500", str(error))
        retry_notes = [
            str(event.get("message") or "")
            for event in events
            if event.get("type") == "status" and "重试" in str(event.get("message") or "")
        ]
        self.assertTrue(
            any("HTTP 500" in note for note in retry_notes),
            f"重试状态提示必须带状态码，实际：{retry_notes}",
        )

    def test_http_500_then_success_returns_content(self) -> None:
        calls, content, error = self._run([
            http_error(500),
            {"output_text": "pong", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}},
        ])
        self.assertIsNone(error)
        self.assertEqual(content, "pong", "重试成功后应当正常返回内容")
        self.assertEqual(len(calls), 2)

    def test_http_400_is_not_retried(self) -> None:
        calls, _content, error = self._run([http_error(400, "bad request", "Bad Request")])
        self.assertEqual(len(calls), 1, "普通 400 不得重试")
        self.assertIsNotNone(error)

    def test_5xx_dumps_sanitized_payload(self) -> None:
        long_image = "A" * 200_000
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image", "data": long_image, "media_type": "image/jpeg"},
            ],
        }]
        with tempfile.TemporaryDirectory() as tmp:
            _calls, _content, error = self._run([http_error(500)], messages=messages, dump_dir=tmp)
            self.assertIsNotNone(error)
            path = os.path.join(tmp, ERROR_DUMP_FILENAME)
            self.assertTrue(os.path.exists(path), "5xx 必须落盘请求体")
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            parsed = json.loads(text)
            self.assertEqual(parsed["status"], 500)
            self.assertEqual(parsed["reason"], "server-error")
            self.assertIn("payload", parsed)
            self.assertNotIn(long_image[:200], text, "base64 图片必须被截断，不能原样落盘")
            self.assertIn("<str:2000", text, "超长字段应带自述长度的占位符")
            self.assertNotIn(PROFILE["api_key"], text, "API Key 绝不能落盘")

    def test_plain_400_does_not_dump(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._run([http_error(400, "bad request", "Bad Request")], dump_dir=tmp)
            self.assertFalse(os.path.exists(os.path.join(tmp, ERROR_DUMP_FILENAME)))

    def test_reasoning_400_still_dumps(self) -> None:
        body = json.dumps({
            "error": {"message": "The `reasoning_text` in the thinking mode must be passed back to the API."},
        })
        with tempfile.TemporaryDirectory() as tmp:
            _calls, _content, error = self._run([http_error(400, body, "Bad Request")], dump_dir=tmp)
            self.assertIsNotNone(error)
            path = os.path.join(tmp, ERROR_DUMP_FILENAME)
            self.assertTrue(os.path.exists(path), "思考回传类 400 仍要落盘（既有取证能力不得回退）")
            with open(path, encoding="utf-8") as handle:
                parsed = json.loads(handle.read())
            self.assertEqual(parsed["reason"], "reasoning-passback")


if __name__ == "__main__":
    unittest.main()
