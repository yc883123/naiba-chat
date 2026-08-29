from __future__ import annotations

import json
import logging
import re
import threading
import time
from html.parser import HTMLParser
import http.client
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

logger = logging.getLogger("naiba.model_runtime")


StatusCallback = Callable[[dict[str, Any]], None]

# Some OpenAI-compatible gateways sit behind Cloudflare rules that reject
# urllib's default ``Python-urllib/...`` signature before authentication is
# evaluated. A normal browser-compatible UA keeps the API request protocol
# unchanged while allowing model-list and inference requests through.
API_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
ONLINE_MODEL_TIMEOUT_SECONDS = 180
LOCAL_MODEL_TIMEOUT_SECONDS = 1800
PROVIDER_TEST_TIMEOUT_SECONDS = 30
FAST_RETRY_NETWORK_ERRORS = {10053, 10054, 10061}
# 本地推理后端对应的请求格式；与 server.LOCAL_REQUEST_FORMATS 保持一致。
LOCAL_REQUEST_FORMATS = {"ollama", "lm_studio", "llama_cpp", "unsloth"}

# Patterns used to keep agent tool-call protocols out of the user-facing
# streaming answer. The classifier below decides, before forwarding any
# fragment, whether the leading model output is ordinary prose or an agent
# action (JSON / XML tool protocol) that must only reach the Agent Loop.
_TOOL_OPEN_TAG = re.compile(r"^<(tool_calls|invoke|tool)\b", re.IGNORECASE)
_TOOL_NAMED_ATTR = re.compile(r"\b(?:name|type)\s*=")
# Some compatible endpoints prepend a sentence before emitting their tool
# protocol. Keep a short unflushed tail so a marker split across SSE chunks is
# detected before it can reach the visible answer.
_TOOL_PROTOCOL_ANYWHERE = re.compile(r"<(?:tool_calls|invoke|tool)\b", re.IGNORECASE)
_JSON_TOOL_ANYWHERE = re.compile(
    r'\{(?=[\s\S]{0,96}"(?:type|tool)"\s*:)',
    re.IGNORECASE,
)
# Upper bound (chars) for buffering an ambiguous leading fragment before we
# give up and treat it as plain text, so a malformed stream can never stall.
_AGENT_BUFFER_LIMIT = 1024


class _ErrorHTMLParser(HTMLParser):
    """Extract readable text from an upstream HTML error page."""

    _IGNORED_TAGS = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.parts: list[str] = []
        self._ignored_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._IGNORED_TAGS:
            self._ignored_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        value = " ".join(data.split())
        if not value:
            return
        if self._in_title:
            self.title = f"{self.title} {value}".strip()
        self.parts.append(value)


def _summarize_http_error(raw: str, content_type: str = "", host: str = "") -> str:
    """Keep upstream failures readable and actionable in the chat UI."""
    text = str(raw or "").strip()
    if not text:
        return "空响应"

    is_html = "html" in str(content_type).lower() or re.search(
        r"<!doctype\s+html|<html\b|<head\b|<body\b", text, flags=re.IGNORECASE
    )
    if is_html:
        parser = _ErrorHTMLParser()
        try:
            parser.feed(text)
        except Exception:
            parser = None
        if parser:
            title = " ".join(parser.title.split())
            visible = " ".join(parser.parts)
            if title and visible.lower().startswith(title.lower()):
                visible = visible[len(title):].lstrip(" :—-")
            summary = f"{title}: {visible}" if title and visible else title or visible
        else:
            summary = ""
        summary = summary or "上游返回了 HTML 错误页"
        lowered_host = str(host or "").lower()
        if "deepseek.com" in lowered_host and not lowered_host.startswith("api."):
            summary += "；请将 API URL 改为 https://api.deepseek.com，不要填写 deepseek.com 网页地址"
        else:
            summary += "；请检查 API URL 是否为模型接口地址，而不是网页地址或被拦截的代理地址"
    else:
        summary = re.sub(r"\s+", " ", text)
        try:
            parsed = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict):
                summary = str(error.get("message") or error.get("detail") or error.get("type") or summary)
            elif error:
                summary = str(error)
            elif parsed.get("message"):
                summary = str(parsed["message"])

    # Cloudflare error 1010 is a gateway policy decision, not a bad model
    # name or API protocol. Keep the upstream detail but add a concise action
    # so users know to try the browser-compatible client signature or ask the
    # provider to allow this endpoint.
    lowered = summary.lower()
    raw_lowered = str(raw or "").lower()
    if (
        "browser_signature_banned" in lowered
        or "browser_signature_banned" in raw_lowered
        or "cloudflare_error\":true" in lowered
        or "cloudflare_error\":true" in raw_lowered
    ):
        summary += "；上游 Cloudflare 拦截了当前客户端签名，请让服务方放行 API 请求，或暂时手动填写模型名称"

    return summary[:800]


def _network_error_code(error: BaseException) -> int | None:
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    value = getattr(reason, "winerror", None) or getattr(reason, "errno", None)
    return int(value) if isinstance(value, int) else None


# ``urllib.request`` builds its global opener lazily on the FIRST request and then
# caches it for the whole process. On Windows it derives proxies from the registry
# / environment at that moment. If proxy or VPN/TUN software is toggled mid-session
# (or the proxy process restarts), the cached opener still points at a now-dead
# proxy, so every later API call fails with a connection-level refusal/reset even
# though the target host is reachable directly. We keep a direct (proxy-bypassed)
# opener as a fallback for exactly this case.
_NO_PROXY_OPENER: urllib.request.OpenerDirector | None = None


def _no_proxy_opener() -> urllib.request.OpenerDirector:
    """Return a process-wide opener that ignores system/environment proxies."""
    global _NO_PROXY_OPENER
    if _NO_PROXY_OPENER is None:
        _NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _NO_PROXY_OPENER


def _urlopen_proxy_resilient(
    request: urllib.request.Request,
    timeout: float,
) -> Any:
    """Open ``request`` with the default opener (system proxy aware), and on a
    connection-level refusal/reset (10053/10054/10061) retry once with a direct,
    proxy-bypassed connection. If the direct attempt also fails, re-raise the
    original error. Used by short-lived one-shot calls (e.g. the model list)."""
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except (urllib.error.URLError, OSError) as exc:
        code = _network_error_code(exc)
        if code in FAST_RETRY_NETWORK_ERRORS:
            try:
                return _no_proxy_opener().open(request, timeout=timeout)
            except Exception:
                pass
        raise


class _NullLock:
    def acquire(self):
        return True

    def release(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _InlineReasoningParser:
    """Split local-model <think> streams without leaking them into the answer."""

    _OPEN = ("<think>", "<thinking>", "<reasoning>")
    _CLOSE = ("</think>", "</thinking>", "</reasoning>")

    def __init__(self) -> None:
        self.buffer = ""
        self.inside = False

    def feed(self, text: str, final: bool = False) -> tuple[str, str]:
        self.buffer += str(text or "")
        visible: list[str] = []
        reasoning: list[str] = []
        while self.buffer:
            markers = self._CLOSE if self.inside else self._OPEN
            positions = [(self.buffer.lower().find(marker), marker) for marker in markers]
            positions = [(index, marker) for index, marker in positions if index >= 0]
            if positions:
                index, marker = min(positions, key=lambda item: item[0])
                chunk = self.buffer[:index]
                (reasoning if self.inside else visible).append(chunk)
                self.buffer = self.buffer[index + len(marker):]
                self.inside = not self.inside
                continue
            if final:
                (reasoning if self.inside else visible).append(self.buffer)
                self.buffer = ""
                break
            # Retain only a suffix that can actually become a marker in the
            # next SSE chunk.  A fixed tail made every short answer arrive in
            # bursts even when it contained no reasoning tag at all.
            lower = self.buffer.lower()
            keep = 0
            for marker in markers:
                limit = min(len(marker) - 1, len(lower))
                for size in range(1, limit + 1):
                    if marker.startswith(lower[-size:]):
                        keep = max(keep, size)
            if keep:
                if keep == len(self.buffer):
                    # The entire buffer may be the beginning of a marker
                    # (for example ``<thi``). Keep it for the next SSE chunk
                    # and stop this pass instead of looping over unchanged
                    # data forever.
                    break
                chunk, self.buffer = self.buffer[:-keep], self.buffer[-keep:]
            else:
                chunk, self.buffer = self.buffer, ""
            (reasoning if self.inside else visible).append(chunk)
        return "".join(visible), "".join(reasoning)


class _ReasoningStreamer:
    """Stream reasoning deltas live when a provider exposes them incrementally.

    ``feed`` emits ``reasoning_start`` once, then a ``reasoning_delta`` per
    incoming piece. ``finish`` closes with ``reasoning_end`` when streaming was
    possible; otherwise (a model that only returns a lump of thinking at the end,
    or none at all) it falls back to a one-shot ``_emit_buffered_reasoning`` so
    the reasoning is still shown, just not incrementally.
    """

    def __init__(self, status: StatusCallback | None, parts: list[str]):
        self.status = status
        self.parts = parts
        self.started = False

    def feed(self, reasoning: str) -> None:
        if not reasoning:
            return
        self.parts.append(reasoning)
        if self.status is not None:
            if not self.started:
                self.status({"type": "reasoning_start"})
                self.started = True
            self.status({"type": "reasoning_delta", "content": reasoning})

    def finish(self) -> None:
        if self.started:
            if self.status is not None:
                self.status({"type": "reasoning_end"})
        else:
            # 兜底：模型未实时暴露思考（增量解析没有触发），改为结尾一次性输出。
            ModelRuntime._emit_buffered_reasoning(self.status, "".join(self.parts))


class ModelRuntime:
    """在线模型调用。"""

    _local_execution_lock = threading.RLock()

    def __init__(self) -> None:
        # 每个 HTTP 请求线程独立保存最近一次模型调用信息，避免并发对话互相覆盖。
        self._local = threading.local()

    @property
    def last_diagnostics(self) -> dict[str, Any]:
        value = getattr(self._local, "last_diagnostics", {})
        return dict(value) if isinstance(value, dict) else {}

    @last_diagnostics.setter
    def last_diagnostics(self, value: dict[str, Any]) -> None:
        self._local.last_diagnostics = dict(value or {})

    @property
    def last_reasoning(self) -> str:
        return str(getattr(self._local, "last_reasoning", ""))

    @last_reasoning.setter
    def last_reasoning(self, value: str) -> None:
        self._local.last_reasoning = value

    @property
    def last_usage(self) -> dict[str, int]:
        value = getattr(self._local, "last_usage", {})
        return dict(value) if isinstance(value, dict) else {}

    @last_usage.setter
    def last_usage(self, value: dict[str, int]) -> None:
        self._local.last_usage = dict(value)

    def complete(
        self,
        profile: dict[str, Any],
        messages: list[dict[str, Any]],
        options: dict[str, Any],
        status: StatusCallback | None = None,
    ) -> str:
        # 按 profile.kind 路由，禁止在线/本地跨模式 fallback。
        kind = str(profile.get("kind") or "").strip().lower()
        request_format = str(profile.get("request_format") or "openai_chat").strip().lower()
        if not kind:
            # 旧 profile 未携带 kind 时按请求格式推断，保持兼容。
            kind = "local" if request_format in LOCAL_REQUEST_FORMATS else "online"
            profile = {**profile, "kind": kind}
        if kind == "local":
            if request_format not in LOCAL_REQUEST_FORMATS:
                raise ValueError(f"本地模型配置使用了非本地请求格式：{request_format}")
        elif kind == "online":
            if request_format in LOCAL_REQUEST_FORMATS:
                raise ValueError(f"在线模型配置使用了本地请求格式：{request_format}")
        else:
            raise ValueError(f"不支持的模型类型：{kind}")
        reasoning_enabled = bool(options.get("reasoning_enabled", True))
        effective_status = status
        if status is not None and not reasoning_enabled:
            def effective_status(payload: dict[str, Any]) -> None:
                if not str(payload.get("type") or "").startswith("reasoning"):
                    status(payload)
        is_local = kind == "local"
        if is_local and status:
            status({"type": "status", "message": "等待本地模型资源"})
        # Local backends share GPU/RAM and commonly expose one active model.
        # Serialize requests so a vision call, sub-agent, and main turn cannot
        # make the local server compete with itself.
        lock = self._local_execution_lock if is_local else _NullLock()
        diagnostics: dict[str, Any] = {
            "provider": str(profile.get("name") or ""),
            "model": str(profile.get("model") or ""),
            "request_format": request_format,
            "local": is_local,
            "stream": bool(options.get("stream", False)),
            "image_count": sum(
                1 for item in messages
                for part in self._content_parts(item.get("content"))
                if isinstance(part, dict) and part.get("type") == "image"
            ),
            "tool_count": len(options.get("tools") or []),
            "context_window": int(profile.get("context_window") or profile.get("context_size") or 0),
            "max_output_tokens": int(options.get("max_tokens") or profile.get("max_output_tokens") or 0),
            "reasoning_effort": str(profile.get("reasoning_effort") or "auto"),
        }
        lock_started = time.perf_counter()
        lock.acquire()
        diagnostics["lock_wait_ms"] = round((time.perf_counter() - lock_started) * 1000, 1)
        total_started = time.perf_counter()
        try:
            content, reasoning, usage = self._complete_online(
                profile, messages, options, effective_status, diagnostics
            )
        finally:
            lock.release()
            diagnostics["total_ms"] = round((time.perf_counter() - total_started) * 1000, 1)
            self.last_diagnostics = diagnostics
        # DeepSeek thinking mode REQUIRES assistant reasoning_content to be passed
        # back on every tool-call message ("The reasoning_content in the thinking
        # mode must be passed back to the API"). Even when the UI has reasoning
        # display off, we must keep the captured reasoning so `last_reasoning`
        # feeds `reasoning_content` into the agent loop's assistant messages.
        # The streaming display is already suppressed by `effective_status`.
        if not reasoning_enabled and not ModelRuntime._is_deepseek_profile(profile):
            reasoning = ""
        self.last_reasoning = reasoning
        self.last_usage = usage
        return content

    @staticmethod
    def _urlopen_cancelable(
        request: urllib.request.Request,
        timeout: float,
        cancel_event: threading.Event | None = None,
        opener: urllib.request.OpenerDirector | None = None,
    ):
        """Run urllib in a daemon worker so a cancelled vision call returns promptly.

        ``opener`` (optional) selects an explicit opener — used to retry with a
        direct, proxy-bypassed connection when the system proxy has gone stale.
        """
        open_function = opener.open if opener is not None else urllib.request.urlopen
        if cancel_event is None:
            return open_function(request, timeout=timeout)
        if cancel_event.is_set():
            raise RuntimeError("任务已取消")
        done = threading.Event()
        abandoned = threading.Event()
        result: dict[str, Any] = {}

        def worker() -> None:
            try:
                response = open_function(request, timeout=timeout)
                if abandoned.is_set():
                    # Main thread already gave up (user cancelled). Close the
                    # fresh connection right away so sockets are not leaked over
                    # a long session (which eventually makes the whole process
                    # unable to connect — to remote or local hosts).
                    try:
                        response.close()
                    except Exception:
                        pass
                else:
                    result["response"] = response
            except BaseException as exc:  # noqa: BLE001 - propagate worker errors
                result["error"] = exc
            finally:
                done.set()

        threading.Thread(target=worker, name="naiba-http-request", daemon=True).start()
        while not done.wait(0.1):
            if cancel_event.is_set():
                abandoned.set()
                raise RuntimeError("任务已取消")
        if cancel_event.is_set():
            response = result.get("response")
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            raise RuntimeError("任务已取消")
        error = result.get("error")
        if error is not None:
            raise error
        return result["response"]

    @staticmethod
    def _read_response_cancelable(response: Any, cancel_event: threading.Event | None = None) -> bytes:
        if cancel_event is None:
            return response.read()
        if cancel_event.is_set():
            raise RuntimeError("任务已取消")
        done = threading.Event()
        result: dict[str, Any] = {}

        def worker() -> None:
            try:
                result["data"] = response.read()
            except BaseException as exc:  # noqa: BLE001
                result["error"] = exc
            finally:
                done.set()

        threading.Thread(target=worker, name="naiba-http-read", daemon=True).start()
        while not done.wait(0.1):
            if cancel_event.is_set():
                try:
                    response.close()
                except Exception:
                    pass
                raise RuntimeError("任务已取消")
        if cancel_event.is_set():
            raise RuntimeError("任务已取消")
        error = result.get("error")
        if error is not None:
            raise error
        return result.get("data", b"")

    @staticmethod
    def _content_parts(content: Any) -> list[dict[str, Any]]:
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        if not isinstance(content, list):
            return [{"type": "text", "text": str(content or "")}]
        return [part for part in content if isinstance(part, dict)]

    @staticmethod
    def _content_text(content: Any) -> str:
        return "\n".join(
            str(part.get("text") or "")
            for part in ModelRuntime._content_parts(content)
            if part.get("type") == "text" and part.get("text")
        )

    @staticmethod
    def _openai_content(content: Any) -> Any:
        if isinstance(content, str):
            return content
        converted = []
        for part in ModelRuntime._content_parts(content):
            if part.get("type") == "text":
                converted.append({"type": "text", "text": str(part.get("text") or "")})
            elif part.get("type") == "image" and part.get("data"):
                converted.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{part.get('media_type') or 'image/jpeg'};base64,{part['data']}"
                        },
                    }
                )
        return converted

    @staticmethod
    def _openai_messages(
        messages: list[dict[str, Any]],
        include_reasoning_content: bool = False,
    ) -> list[dict[str, Any]]:
        converted = []
        for item in messages:
            role = str(item.get("role") or "user")
            if role == "tool":
                converted.append({
                    "role": "tool",
                    "tool_call_id": str(item.get("tool_call_id") or ""),
                    "content": ModelRuntime._content_text(item.get("content")),
                })
                continue
            message = {"role": role, "content": ModelRuntime._openai_content(item.get("content"))}
            if role == "assistant":
                reasoning_content = item.get("reasoning_content")
                if reasoning_content is None:
                    reasoning_content = item.get("reasoning")
                if reasoning_content is not None or include_reasoning_content:
                    message["reasoning_content"] = (
                        "" if reasoning_content is None else str(reasoning_content)
                    )
            if role == "assistant" and isinstance(item.get("tool_calls"), list):
                message["tool_calls"] = [
                    {
                        "id": str(call.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(call.get("name") or ""),
                            "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                        },
                    }
                    for call in item["tool_calls"] if isinstance(call, dict)
                ]
            converted.append(message)
        return converted

    @staticmethod
    def _is_deepseek_profile(profile: dict[str, Any]) -> bool:
        """Return whether an OpenAI-compatible profile speaks DeepSeek's
        thinking-mode dialect, which requires assistant reasoning_content on
        every replayed assistant message (including an empty value)."""
        base_url = str(profile.get("base_url") or "").lower()
        model = str(profile.get("model") or "").lower()
        return "deepseek" in model or "deepseek.com" in base_url

    @staticmethod
    def _responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted = []
        for item in messages:
            role = str(item.get("role") or "user")
            if role == "tool":
                converted.append({
                    "type": "function_call_output",
                    "call_id": str(item.get("tool_call_id") or ""),
                    "output": ModelRuntime._content_text(item.get("content")),
                })
                continue
            if role == "assistant" and isinstance(item.get("tool_calls"), list):
                converted.extend({
                    "type": "function_call",
                    "call_id": str(call.get("id") or ""),
                    "name": str(call.get("name") or ""),
                    "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                } for call in item["tool_calls"] if isinstance(call, dict))
                continue
            converted.append({
                "role": role,
                "content": ModelRuntime._responses_content(item.get("content"), role),
            })
        return converted

    @staticmethod
    def _tool_schemas(tools: Any, request_format: str) -> list[dict[str, Any]]:
        """Convert ToolRegistry rows to the provider's native function schema."""
        rows = tools if isinstance(tools, list) else []
        converted: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict) or not str(row.get("name") or "").strip():
                continue
            name = str(row["name"])
            description = str(row.get("description") or "")
            parameters = row.get("parameters") or {"type": "object", "properties": {}}
            if request_format == "codex_responses":
                converted.append({
                    "type": "function",
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                    "strict": False,
                })
            elif request_format == "gemini":
                converted.append({
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                })
            elif request_format == "claude":
                converted.append({
                    "name": name,
                    "description": description,
                    "input_schema": parameters,
                })
            else:
                converted.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": parameters,
                    },
                })
        return converted

    @staticmethod
    def _responses_content(content: Any, role: str) -> Any:
        if isinstance(content, str):
            return content
        converted = []
        for part in ModelRuntime._content_parts(content):
            if part.get("type") == "text":
                converted.append(
                    {
                        "type": "output_text" if role == "assistant" else "input_text",
                        "text": str(part.get("text") or ""),
                    }
                )
            elif role != "assistant" and part.get("type") == "image" and part.get("data"):
                converted.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{part.get('media_type') or 'image/jpeg'};base64,{part['data']}",
                    }
                )
        return converted

    @staticmethod
    def list_online_models(profile: dict[str, Any]) -> list[dict[str, Any]]:
        base_url = str(profile.get("base_url") or "").rstrip("/")
        api_key = str(profile.get("api_key") or "").strip()
        configured_format = str(profile.get("request_format") or "openai_chat").strip().lower()
        # llama.cpp and Unsloth servers expose the OpenAI Chat API, but they
        # are local inference backends. Normalize only the wire protocol;
        # retain the profile kind below so timeout/retry routing stays local.
        request_format = "openai_chat" if configured_format in {"llama_cpp", "unsloth"} else configured_format
        if not base_url:
            raise ValueError("请先填写 API URL")

        headers = {"Accept": "application/json", "User-Agent": API_USER_AGENT}
        if request_format == "gemini":
            endpoint = ModelRuntime._with_endpoint(base_url, "/v1beta/models")
            if api_key:
                headers["x-goog-api-key"] = api_key
        elif request_format == "ollama":
            endpoint = ModelRuntime._local_endpoint(base_url, "/api/tags")
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        elif request_format == "lm_studio":
            endpoint = ModelRuntime._local_endpoint(base_url, "/api/v1/models")
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        else:
            endpoint = ModelRuntime._with_endpoint(base_url, "/v1/models")
            if request_format == "claude":
                if api_key:
                    headers["x-api-key"] = api_key
                headers["anthropic-version"] = "2023-06-01"
            elif api_key:
                headers["Authorization"] = f"Bearer {api_key}"

        request = urllib.request.Request(endpoint, headers=headers, method="GET")
        try:
            with _urlopen_proxy_resilient(request, 60) as response:
                raw = response.read().decode("utf-8", errors="replace")
                content_type = response.headers.get("Content-Type", "") if hasattr(response, "headers") else ""
                try:
                    result = json.loads(raw)
                except json.JSONDecodeError as exc:
                    detail = _summarize_http_error(raw, content_type, urllib.parse.urlsplit(endpoint).hostname or "")
                    raise RuntimeError(f"模型接口返回的不是 JSON：{detail}") from exc
        except urllib.error.HTTPError as exc:
            detail = _summarize_http_error(
                exc.read().decode("utf-8", errors="replace"),
                exc.headers.get("Content-Type", "") if exc.headers else "",
                urllib.parse.urlsplit(endpoint).hostname or "",
            )
            raise RuntimeError(f"模型列表返回 HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接模型列表接口 {endpoint}：{exc.reason}") from exc
        except OSError as exc:
            raise RuntimeError(f"模型列表接口 {endpoint} 的连接被本机或远端中止：{exc}") from exc

        items = []
        if isinstance(result, dict):
            items = result.get("data") or result.get("models") or []
        models = []
        seen = set()
        for item in items:
            if isinstance(item, str):
                model_id = item
                display_name = item
            elif isinstance(item, dict):
                if request_format == "gemini":
                    methods = item.get("supportedGenerationMethods") or []
                    if methods and not any("generateContent" in str(method) for method in methods):
                        continue
                model_id = str(
                    item.get("id") or item.get("key") or item.get("name")
                    or (item.get("model") if request_format == "ollama" else "") or ""
                )
                if request_format == "gemini" and model_id.startswith("models/"):
                    model_id = model_id[7:]
                display_name = str(
                    item.get("display_name") or item.get("displayName") or item.get("name") or model_id
                )
            else:
                continue
            model_id = model_id.strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            capability: dict[str, Any] = {}
            if isinstance(item, dict):
                capabilities = item.get("capabilities")
                vision = capabilities.get("vision") if isinstance(capabilities, dict) else None
                if not isinstance(vision, bool):
                    loaded = item.get("loaded_instances")
                    if isinstance(loaded, list):
                        for instance in loaded:
                            instance_caps = instance.get("capabilities") if isinstance(instance, dict) else None
                            candidate = instance_caps.get("vision") if isinstance(instance_caps, dict) else None
                            if isinstance(candidate, bool):
                                vision = candidate
                                break
                if isinstance(vision, bool):
                    capability["supports_images"] = vision
                for target, keys in {
                    "context_window": (
                        "context_window", "contextWindow", "context_length", "contextLength",
                        "max_context_length", "maxContextLength", "inputTokenLimit",
                    ),
                    "max_output_tokens": (
                        "max_output_tokens", "maxOutputTokens", "outputTokenLimit",
                        "max_completion_tokens", "maxCompletionTokens",
                    ),
                }.items():
                    for key in keys:
                        raw = item.get(key)
                        if raw not in (None, ""):
                            try:
                                parsed = int(raw)
                            except (TypeError, ValueError):
                                continue
                            if parsed > 0:
                                capability[target] = parsed
                                break
            models.append({"id": model_id, "name": display_name.strip() or model_id, **capability})
        return models[:500]

    @staticmethod
    def unload_local_model(profile: dict[str, Any]) -> dict[str, str]:
        """Ask a supported local model server to unload its active model."""
        base_url = str(profile.get("base_url") or "").rstrip("/")
        model = str(profile.get("model") or "").strip()
        api_key = str(profile.get("api_key") or "").strip()
        request_format = str(profile.get("request_format") or "").strip().lower()
        local_kind = str(profile.get("local_kind") or "").strip().lower()
        if not base_url or not model:
            raise ValueError("本地模型需要 Base URL 和模型名称")

        if request_format == "ollama" or local_kind == "ollama":
            endpoint = ModelRuntime._local_endpoint(base_url, "/api/generate")
            payload = {"model": model, "keep_alive": 0}
            provider_name = "Ollama"
        elif request_format == "lm_studio" or local_kind == "lm_studio":
            endpoint = ModelRuntime._local_endpoint(base_url, "/api/v1/models/unload")
            payload = {"instance_id": model}
            provider_name = "LM Studio"
        else:
            raise ValueError("当前供应商不是支持手动卸载的本地模型服务")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": API_USER_AGENT,
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            detail = _summarize_http_error(
                exc.read().decode("utf-8", errors="replace"),
                exc.headers.get("Content-Type", "") if exc.headers else "",
                urllib.parse.urlsplit(endpoint).hostname or "",
            )
            raise RuntimeError(f"{provider_name} 卸载模型失败 HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"无法连接 {provider_name} 卸载接口：{exc.reason}") from exc
        except OSError as exc:
            raise RuntimeError(f"{provider_name} 卸载连接被本机或服务中止：{exc}") from exc
        return {"provider": provider_name, "model": model}

    @staticmethod
    def _complete_online(
        profile: dict[str, Any],
        messages: list[dict[str, Any]],
        options: dict[str, Any],
        status: StatusCallback | None = None,
        diagnostics: dict[str, Any] | None = None,
    ) -> tuple[str, str, dict[str, int]]:
        base_url = str(profile.get("base_url") or "").rstrip("/")
        model = str(profile.get("model") or "").strip()
        api_key = str(profile.get("api_key") or "").strip()
        if not base_url or not model:
            raise ValueError("在线模型需要 Base URL 和模型名称")
        configured_format = str(profile.get("request_format") or "openai_chat").strip().lower()
        # llama.cpp and Unsloth servers expose the OpenAI Chat API, but they
        # are local inference backends. Normalize only the wire protocol;
        # retain the profile kind below so timeout/retry routing stays local.
        request_format = "openai_chat" if configured_format in {"llama_cpp", "unsloth"} else configured_format
        temperature_raw = options.get("temperature", profile.get("temperature"))
        temperature = None if temperature_raw in (None, "") else float(temperature_raw)
        max_tokens_raw = options.get("max_tokens", profile.get("max_output_tokens"))
        max_tokens = None if max_tokens_raw in (None, "") else int(max_tokens_raw)
        # DeepSeek rejects zero/negative values and any value above its API
        # ceiling.  UI/router defaults can be larger than a provider's limit,
        # so clamp only the wire value while keeping the profile unchanged.
        if max_tokens is not None:
            # A legacy profile can persist zero as "unset".  Never send that
            # sentinel to an API: DeepSeek answers it with HTTP 400.
            if max_tokens <= 0:
                max_tokens = None
            elif ModelRuntime._is_deepseek_profile(profile):
                max_tokens = min(max_tokens, 393216)
        stream_enabled = bool(options.get("stream", False))
        reasoning_effort = str(profile.get("reasoning_effort") or "auto").strip().lower()
        reasoning_enabled = bool(
            options.get("reasoning_enabled", reasoning_effort in {"low", "medium", "high"})
        )
        headers = {"Content-Type": "application/json", "User-Agent": API_USER_AGENT}
        native_tools = ModelRuntime._tool_schemas(options.get("tools"), request_format)
        response_format = request_format

        if request_format == "openai_chat":
            endpoint = ModelRuntime._with_endpoint(base_url, "/v1/chat/completions")
            payload = {
                "model": model,
                "messages": ModelRuntime._openai_messages(
                    messages,
                    # DeepSeek-compatible gateways commonly reject an empty
                    # reasoning_content field on ordinary assistant history.
                    # Real persisted reasoning is still preserved by
                    # _openai_messages; only synthetic empty backfills are
                    # disabled for DeepSeek.
                    include_reasoning_content=(
                        reasoning_enabled and not ModelRuntime._is_deepseek_profile(profile)
                    ),
                ),
                "stream": stream_enabled,
            }
            if temperature is not None:
                payload["temperature"] = temperature
            if max_tokens is not None:
                payload["max_tokens"] = max_tokens
            if native_tools:
                payload["tools"] = native_tools
                payload["tool_choice"] = "auto"
                payload["parallel_tool_calls"] = True
            reasoning_params = ModelRuntime._reasoning_params(request_format, reasoning_effort)
            # DeepSeek selects thinking behavior from the model itself; its
            # OpenAI-compatible endpoint does not accept OpenAI's
            # `reasoning_effort` request field.
            if ModelRuntime._is_deepseek_profile(profile):
                reasoning_params = {}
            if reasoning_params:
                payload.update(reasoning_params)
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        elif request_format == "codex_responses":
            endpoint = ModelRuntime._with_endpoint(base_url, "/v1/responses")
            instructions = "\n\n".join(
                ModelRuntime._content_text(item.get("content"))
                for item in messages if item.get("role") == "system"
            )
            payload = {
                "model": model,
                "input": ModelRuntime._responses_input([
                    item for item in messages if item.get("role") != "system"
                ]),
                "stream": stream_enabled,
            }
            if temperature is not None:
                payload["temperature"] = temperature
            if max_tokens is not None:
                payload["max_output_tokens"] = max_tokens
            if instructions:
                payload["instructions"] = instructions
            if native_tools:
                payload["tools"] = native_tools
                payload["tool_choice"] = "auto"
                payload["parallel_tool_calls"] = True
            reasoning_params = ModelRuntime._reasoning_params(
                request_format, reasoning_effort,
                deepseek=ModelRuntime._is_deepseek_profile(profile),
            )
            if reasoning_params:
                payload.update(reasoning_params)
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        elif request_format == "gemini":
            encoded_model = urllib.parse.quote(model, safe="")
            endpoint = ModelRuntime._with_endpoint(base_url, f"/v1beta/models/{encoded_model}:streamGenerateContent")
            system_parts = [
                {"text": ModelRuntime._content_text(item.get("content"))}
                for item in messages if item.get("role") == "system"
            ]
            contents = [
                ModelRuntime._gemini_message(item)
                for item in messages if item.get("role") != "system"
            ]
            generation_config = {}
            if temperature is not None:
                generation_config["temperature"] = temperature
            if max_tokens is not None:
                generation_config["maxOutputTokens"] = max_tokens
            payload = {"contents": contents}
            if generation_config:
                payload["generationConfig"] = generation_config
            if system_parts:
                payload["systemInstruction"] = {"parts": system_parts}
            if native_tools:
                payload["tools"] = [{"functionDeclarations": native_tools}]
                payload["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
            if api_key:
                headers["x-goog-api-key"] = api_key
        elif request_format == "claude":
            endpoint = ModelRuntime._with_endpoint(base_url, "/v1/messages")
            system = "\n\n".join(
                ModelRuntime._content_text(item.get("content"))
                for item in messages if item.get("role") == "system"
            )
            # Anthropic requires max_tokens; use its compatibility floor only
            # when the provider did not expose a limit and the user left it blank.
            payload = {
                "model": model,
                "messages": [
                    ModelRuntime._claude_message(item)
                    for item in messages if item.get("role") != "system"
                ],
                "max_tokens": max_tokens if max_tokens is not None else 4096,
                "stream": stream_enabled,
            }
            if temperature is not None:
                payload["temperature"] = temperature
            if system:
                # Anthropic 的前缀缓存必须显式标记 cache_control 才生效；
                # system 改为 content block 数组并打上缓存断点。
                payload["system"] = [{
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }]
            if native_tools:
                payload["tools"] = native_tools
                payload["tool_choice"] = {"type": "auto"}
            # 在最后一条非 tool_result 的 user 消息末尾追加缓存断点，
            # 让编辑重开时编辑点之前的前缀命中 Anthropic prompt cache。
            ModelRuntime._claude_apply_cache_control(payload["messages"])
            if api_key:
                headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        elif request_format == "ollama":
            endpoint = ModelRuntime._local_endpoint(base_url, "/api/chat")
            ollama_options = {}
            if temperature is not None:
                ollama_options["temperature"] = temperature
            if max_tokens is not None:
                ollama_options["num_predict"] = max_tokens
            payload = {
                "model": model,
                "messages": ModelRuntime._ollama_messages(messages),
                "stream": stream_enabled,
            }
            if native_tools:
                payload["tools"] = native_tools
            context_window = profile.get("context_window") or profile.get("context_size")
            if context_window:
                ollama_options["num_ctx"] = int(context_window)
            if ollama_options:
                payload["options"] = ollama_options
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            reasoning_params = ModelRuntime._reasoning_params(request_format, reasoning_effort)
            if reasoning_params:
                payload.update(reasoning_params)
        elif request_format == "lm_studio":
            if native_tools:
                # LM Studio exposes an OpenAI-compatible endpoint for native
                # function calling. Keep its custom endpoint for tool-free chat.
                response_format = "openai_chat"
                endpoint = ModelRuntime._with_endpoint(base_url, "/v1/chat/completions")
                payload = {
                    "model": model,
                    "messages": ModelRuntime._openai_messages(
                        messages,
                        include_reasoning_content=(
                            reasoning_enabled and not ModelRuntime._is_deepseek_profile(profile)
                        ),
                    ),
                    "stream": stream_enabled,
                    "tools": native_tools,
                    "tool_choice": "auto",
                    "parallel_tool_calls": True,
                }
                if temperature is not None:
                    payload["temperature"] = temperature
                if max_tokens is not None:
                    payload["max_tokens"] = max_tokens
            else:
                endpoint = ModelRuntime._local_endpoint(base_url, "/api/v1/chat")
                system_prompt, lm_input = ModelRuntime._lm_studio_messages(messages)
                payload = {
                    "model": model,
                    "system_prompt": system_prompt,
                    "input": lm_input,
                    "stream": stream_enabled,
                }
                if temperature is not None:
                    payload["temperature"] = temperature
                if max_tokens is not None:
                    payload["max_output_tokens"] = max_tokens
                context_window = profile.get("context_window") or profile.get("context_size")
                if context_window:
                    payload["context_length"] = int(context_window)
                reasoning_params = ModelRuntime._reasoning_params(request_format, reasoning_effort)
                if reasoning_params:
                    payload.update(reasoning_params)
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        else:
            raise ValueError(f"不支持的在线请求格式：{request_format}")

        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        stream_options_requested = bool(stream_enabled and response_format == "openai_chat")
        if stream_options_requested:
            payload["stream_options"] = {"include_usage": True}
            request = urllib.request.Request(
                endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )

        is_local = (
            str(profile.get("kind") or "").strip().lower() == "local"
            or configured_format in LOCAL_REQUEST_FORMATS
        )
        connection_test = bool(options.get("connection_test", False))
        cancel_event = options.get("cancel_event")
        if not isinstance(cancel_event, threading.Event):
            cancel_event = None
        provider_name = str(profile.get("name") or "").strip()
        parsed_endpoint = urllib.parse.urlsplit(endpoint)
        endpoint_host = parsed_endpoint.hostname or parsed_endpoint.netloc or endpoint
        endpoint_port = parsed_endpoint.port or (443 if parsed_endpoint.scheme == "https" else 80)
        target = "本地模型" if is_local else "在线模型"
        target_detail = f"{target}“{provider_name}”" if provider_name else target
        # Include the endpoint PATH (not only host:port) so a mis-routed request
        # (e.g. an unexpected /v1/models) is immediately visible in the error.
        endpoint_path = parsed_endpoint.path or "/"
        target_detail += f"（{endpoint_host}:{endpoint_port}{endpoint_path}）"
        request_timeout = (
            LOCAL_MODEL_TIMEOUT_SECONDS
            if is_local
            else PROVIDER_TEST_TIMEOUT_SECONDS if connection_test else ONLINE_MODEL_TIMEOUT_SECONDS
        )
        timeout_override = options.get("request_timeout_seconds")
        if isinstance(timeout_override, (int, float)) and timeout_override > 0:
            request_timeout = max(1, min(int(timeout_override), LOCAL_MODEL_TIMEOUT_SECONDS))
        attempts = 1 if is_local else 3
        attempts_override = options.get("request_attempts")
        if isinstance(attempts_override, int) and attempts_override > 0:
            attempts = min(attempts_override, 5)
        if native_tools:
            attempts = max(attempts, 2)
        if stream_options_requested:
            attempts = max(attempts, 2)
        if stream_options_requested and native_tools:
            attempts = max(attempts, 3)
        tool_fallback_used = False
        stream_options_fallback_used = False
        reasoning_fallback_used = False
        request_opener: urllib.request.OpenerDirector | None = None
        no_proxy_tried = False
        if diagnostics is not None:
            parsed = urllib.parse.urlsplit(endpoint)
            diagnostics.update({
                "endpoint": f"{parsed.hostname or parsed.netloc}{parsed.path}",
                "attempts": 0,
                "http_ms": 0.0,
            })
        for attempt in range(attempts):
            request_started = time.perf_counter()
            if diagnostics is not None:
                diagnostics["attempts"] = attempt + 1
            try:
                with ModelRuntime._urlopen_cancelable(request, request_timeout, cancel_event, request_opener) as response:
                    if stream_enabled and response_format == "ollama":
                        streamed = ModelRuntime._read_ollama_stream(response, status)
                        content = ModelRuntime._clean_content(streamed["content"])
                        reasoning = streamed["reasoning"]
                        if not content:
                            content = ModelRuntime._reasoning_action(reasoning)
                            if content:
                                reasoning = ""
                            elif payload.get("think") is not False:
                                if status:
                                    status({"type": "status", "message": "Ollama 未返回正文，正在关闭思考后重试"})
                                retry_payload = dict(payload)
                                retry_payload["think"] = False
                                retry_request = urllib.request.Request(
                                    endpoint,
                                    data=json.dumps(retry_payload, ensure_ascii=False).encode("utf-8"),
                                    headers=headers,
                                    method="POST",
                                )
                                with ModelRuntime._urlopen_cancelable(
                                    retry_request, request_timeout, cancel_event, request_opener
                                ) as retry_response:
                                    streamed = ModelRuntime._read_ollama_stream(retry_response, status)
                                content = ModelRuntime._clean_content(streamed["content"])
                                reasoning = streamed["reasoning"]
                                if not content:
                                    content = ModelRuntime._reasoning_action(reasoning)
                                    if content:
                                        reasoning = ""
                            if not content:
                                raise RuntimeError("Ollama 流式响应中没有文本内容")
                        return content, reasoning, streamed["usage"]
                    if stream_enabled and response_format == "lm_studio":
                        streamed = ModelRuntime._read_lm_studio_stream(response, status)
                        content = ModelRuntime._clean_content(streamed["content"])
                        reasoning = streamed["reasoning"]
                        if not content:
                            content = ModelRuntime._reasoning_action(reasoning)
                            if content:
                                reasoning = ""
                            else:
                                raise RuntimeError("LM Studio 流式响应中没有文本内容")
                        return content, reasoning, streamed["usage"]
                    if stream_enabled and response_format != "gemini":
                        streamed = ModelRuntime._read_sse_response(response, response_format, status)
                        content = ModelRuntime._clean_content(streamed["content"])
                        reasoning = streamed["reasoning"]
                        if not content:
                            content = ModelRuntime._reasoning_action(reasoning)
                            if content:
                                reasoning = ""
                            else:
                                raise RuntimeError("在线模型流式响应中没有文本内容")
                        return content, reasoning, streamed["usage"]
                    raw_response = ModelRuntime._read_response_cancelable(
                        response, cancel_event
                    ).decode("utf-8", errors="replace")
                    content_type = response.headers.get("Content-Type", "") if hasattr(response, "headers") else ""
                    try:
                        result = json.loads(raw_response)
                    except json.JSONDecodeError as exc:
                        chunks = []
                        for line in raw_response.splitlines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if not data or data == "[DONE]":
                                continue
                            try:
                                chunks.append(json.loads(data))
                            except json.JSONDecodeError:
                                chunks = []
                                break
                        if chunks:
                            result = chunks
                        else:
                            preview = raw_response.strip()[:500] or "空响应"
                            raise RuntimeError(
                                f"在线模型返回的不是 JSON（{content_type or '未知类型'}）：{preview}"
                            ) from exc
                break
            except urllib.error.HTTPError as exc:
                detail = _summarize_http_error(
                    exc.read().decode("utf-8", errors="replace"),
                    exc.headers.get("Content-Type", "") if exc.headers else "",
                    endpoint_host,
                )
                stream_option_rejection = any(
                    marker in detail.lower()
                    for marker in ("stream_options", "include_usage")
                )
                if (
                    stream_options_requested
                    and not stream_options_fallback_used
                    and exc.code in {400, 422}
                    and stream_option_rejection
                ):
                    payload.pop("stream_options", None)
                    request = urllib.request.Request(
                        endpoint,
                        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        headers=headers,
                        method="POST",
                    )
                    stream_options_fallback_used = True
                    if status:
                        status({"type": "status", "message": "当前接口不支持流式 usage 参数，已切换兼容请求"})
                    continue
                tool_rejection = any(
                    marker in detail.lower()
                    for marker in ("tools", "tool_choice", "functioncalling", "function calling", "unknown field", "unsupported")
                )
                if native_tools and not tool_fallback_used and exc.code in {400, 404, 422} and tool_rejection:
                    fallback_payload = dict(payload)
                    for key in ("tools", "tool_choice", "parallel_tool_calls", "toolConfig"):
                        fallback_payload.pop(key, None)
                    request = urllib.request.Request(
                        endpoint,
                        data=json.dumps(fallback_payload, ensure_ascii=False).encode("utf-8"),
                        headers=headers,
                        method="POST",
                    )
                    tool_fallback_used = True
                    if status:
                        status({"type": "status", "message": "当前接口不支持原生 Tool Calling，已切换兼容工具协议"})
                    continue
                # DeepSeek thinking mode rejects an assistant history message that
                # is missing reasoning_content ("must be passed back to the API").
                # That is the OPPOSITE of "this field is not accepted", so we must
                # NOT trigger the strip-fields fallback — otherwise the retry drops
                # reasoning_content and is guaranteed to fail with the same 400.
                lower_detail = str(detail).lower()
                reasoning_required = any(
                    marker in lower_detail
                    for marker in (
                        "must be passed", "must be provided", "must be included",
                        "must be returned", "is required", "required to be",
                    )
                )
                reasoning_rejection = (
                    not reasoning_required
                    and any(
                        marker in lower_detail
                        for marker in (
                            "reasoning_content", "reasoning_effort", "thinking",
                            "unknown field", "unrecognized", "does not support",
                            "unsupported", "invalid field",
                        )
                    )
                )
                if (
                    not is_local
                    and not reasoning_fallback_used
                    and exc.code in {400, 422}
                    and reasoning_rejection
                ):
                    # OpenAI-compatible gateways disagree on whether they
                    # accept optional thinking fields. Retry once with those
                    # fields removed; never duplicate a tool submission beyond
                    # this protocol-only retry.
                    fallback_payload = dict(payload)
                    fallback_messages = []
                    for message in fallback_payload.get("messages", []):
                        if isinstance(message, dict):
                            message = dict(message)
                            message.pop("reasoning_content", None)
                        fallback_messages.append(message)
                    fallback_payload["messages"] = fallback_messages
                    fallback_payload.pop("reasoning_effort", None)
                    request = urllib.request.Request(
                        endpoint,
                        data=json.dumps(fallback_payload, ensure_ascii=False).encode("utf-8"),
                        headers=headers,
                        method="POST",
                    )
                    reasoning_fallback_used = True
                    if status:
                        status({"type": "status", "message": "当前网关不接受思考字段，已自动切换兼容请求"})
                    continue
                if (
                    not is_local
                    and not connection_test
                    and exc.code in {429, 502, 503, 504}
                    and attempt + 1 < attempts
                ):
                    if exc.code == 429:
                        retry_after = ""
                        if exc.headers:
                            retry_after = str(exc.headers.get("Retry-After") or "").strip()
                        try:
                            delay = max(1.0, min(float(retry_after), 60.0))
                        except (TypeError, ValueError):
                            delay = min(10.0 * (2 ** attempt), 60.0)
                        retry_message = (
                            f"供应商限流，{delay:g} 秒后重试"
                            f"（{attempt + 1}/{attempts - 1}）"
                        )
                    else:
                        delay = min(1.5 * (attempt + 1), 5.0)
                        retry_message = (
                            f"供应商暂时不可用（HTTP {exc.code}），{delay:g} 秒后重试"
                            f"（{attempt + 1}/{attempts - 1}）"
                        )
                    if status:
                        status({"type": "status", "message": retry_message})
                    if cancel_event:
                        if cancel_event.wait(delay):
                            raise RuntimeError("任务已取消")
                    else:
                        time.sleep(delay)
                    continue
                raise RuntimeError(f"{target_detail}返回 HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                error_code = _network_error_code(exc)
                # 响应读取中途被切断（IncompleteRead / RemoteDisconnected 等 HTTPException）
                # 属于瞬时传输故障：与连接失败一样可重试，而不是让整个 run 直接报错。
                is_http_exception = isinstance(exc, http.client.HTTPException)
                # A connection-level refusal/reset for an ONLINE provider often
                # follows a stale system proxy (proxy/TUN toggled mid-session, or
                # the proxy process restarted). urllib caches the proxy in its
                # opener at first use, so it will keep failing on the same dead
                # proxy. Before giving up, retry once with a direct,
                # proxy-bypassed connection.
                if (
                    not is_local
                    and not connection_test
                    and not no_proxy_tried
                    and error_code in FAST_RETRY_NETWORK_ERRORS
                ):
                    no_proxy_tried = True
                    request_opener = _no_proxy_opener()
                    if status:
                        status({"type": "status", "message": "检测到代理/TUN 连接异常，正在尝试直连重试"})
                    continue
                retryable_test_error = connection_test and error_code in FAST_RETRY_NETWORK_ERRORS
                if (
                    not is_local
                    and attempt + 1 < attempts
                    and (not connection_test or retryable_test_error or is_http_exception)
                ):
                    delay = (0.5 if connection_test else 1.5) * (attempt + 1)
                    if cancel_event and cancel_event.wait(delay):
                        raise RuntimeError("任务已取消")
                    continue
                if isinstance(reason, TimeoutError):
                    duration = f"{request_timeout // 60} 分钟" if request_timeout >= 60 else f"{request_timeout} 秒"
                    raise RuntimeError(f"{target_detail}响应超过 {duration}，已停止等待") from exc
                hint = ""
                if error_code == 10061:
                    hint = "；目标端口拒绝连接，请检查 API URL、代理/TUN 或服务是否已启动"
                elif error_code in {10053, 10054}:
                    hint = "；连接被中止，请检查代理/TUN、防火墙或服务状态"
                elif is_http_exception:
                    hint = "；响应读取不完整（连接中断），请检查网络/代理后重试"
                raise RuntimeError(f"无法连接{target_detail}：{reason}{hint}") from exc

            finally:
                if diagnostics is not None:
                    diagnostics["http_ms"] = round(
                        float(diagnostics.get("http_ms") or 0.0)
                        + (time.perf_counter() - request_started) * 1000,
                        1,
                    )

        usage = ModelRuntime._online_usage(response_format, result)
        try:
            content, reasoning = ModelRuntime._online_response(response_format, result)
        except RuntimeError:
            reasoning = ModelRuntime._online_reasoning(response_format, result)
            if connection_test and reasoning:
                return "接口已返回有效响应", reasoning, usage
            raise
        return content, reasoning, usage

    @staticmethod
    def _ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted = []
        for item in messages:
            content = item.get("content")
            role = str(item.get("role") or "user")
            message = {
                "role": role,
                "content": ModelRuntime._content_text(content) if not isinstance(content, str) else content,
            }
            if role == "assistant" and isinstance(item.get("tool_calls"), list):
                message["tool_calls"] = [{
                    "function": {
                        "name": str(call.get("name") or ""),
                        "arguments": call.get("arguments") or {},
                    }
                } for call in item["tool_calls"] if isinstance(call, dict)]
            if role == "tool":
                message["tool_name"] = str(item.get("name") or "")
            images = [
                str(part.get("data") or "")
                for part in ModelRuntime._content_parts(content)
                if part.get("type") == "image" and part.get("data")
            ]
            if images:
                message["images"] = images
            converted.append(message)
        return converted

    @staticmethod
    def _gemini_message(item: dict[str, Any]) -> dict[str, Any]:
        role = str(item.get("role") or "user")
        if role == "tool":
            try:
                response = json.loads(ModelRuntime._content_text(item.get("content")))
            except (json.JSONDecodeError, TypeError):
                response = {"result": ModelRuntime._content_text(item.get("content"))}
            return {
                "role": "user",
                "parts": [{"functionResponse": {"name": str(item.get("name") or ""), "response": response}}],
            }
        if role == "assistant" and isinstance(item.get("tool_calls"), list):
            return {
                "role": "model",
                "parts": [{"functionCall": {
                    "name": str(call.get("name") or ""),
                    "args": call.get("arguments") or {},
                }} for call in item["tool_calls"] if isinstance(call, dict)],
            }
        return {
            "role": "model" if role == "assistant" else "user",
            "parts": [
                {"text": str(part.get("text") or "")}
                if part.get("type") == "text"
                else {"inlineData": {"mimeType": part.get("media_type") or "image/jpeg", "data": part.get("data") or ""}}
                for part in ModelRuntime._content_parts(item.get("content"))
                if part.get("type") == "text" or (part.get("type") == "image" and part.get("data"))
            ],
        }

    @staticmethod
    def _claude_message(item: dict[str, Any]) -> dict[str, Any]:
        role = str(item.get("role") or "user")
        if role == "tool":
            return {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": str(item.get("tool_call_id") or ""),
                    "content": ModelRuntime._content_text(item.get("content")),
                }],
            }
        if role == "assistant" and isinstance(item.get("tool_calls"), list):
            return {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": str(call.get("id") or ""),
                    "name": str(call.get("name") or ""),
                    "input": call.get("arguments") or {},
                } for call in item["tool_calls"] if isinstance(call, dict)],
            }
        return {
            "role": "assistant" if role == "assistant" else "user",
            "content": [
                {"type": "text", "text": str(part.get("text") or "")}
                if part.get("type") == "text"
                else {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": part.get("media_type") or "image/jpeg",
                        "data": part.get("data") or "",
                    },
                }
                for part in ModelRuntime._content_parts(item.get("content"))
                if part.get("type") == "text" or (part.get("type") == "image" and part.get("data"))
            ],
        }

    @staticmethod
    def _lm_studio_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        """LM Studio 原生 /api/v1/chat 序列化。

        - system 消息提取到顶层 ``system_prompt``；
        - 其余消息的内容统一展开为结构化部件：文本 ``{"type":"text","content":...}``、
          图片 ``{"type":"image","data_url":"..."}``。不再使用 OpenAI ``messages``/``image_url``。
        """
        system_parts: list[str] = []
        input_parts: list[dict[str, Any]] = []
        for item in messages:
            role = str(item.get("role") or "user")
            content = item.get("content")
            if role == "system":
                system_parts.append(ModelRuntime._content_text(content))
                continue
            for part in ModelRuntime._content_parts(content):
                if part.get("type") == "text":
                    text = str(part.get("text") or "")
                    if text:
                        input_parts.append({"type": "text", "content": text})
                elif part.get("type") == "image" and part.get("data"):
                    input_parts.append(
                        {
                            "type": "image",
                            "data_url": f"data:{part.get('media_type') or 'image/jpeg'};base64,{part['data']}",
                        }
                    )
        system_prompt = "\n\n".join(p.strip() for p in system_parts if p.strip())
        return system_prompt, input_parts

    @staticmethod
    def _reasoning_params(request_format: str, effort: str, deepseek: bool = False) -> dict[str, Any]:
        """把思维强度映射为各供应商协议字段；``auto`` 不发送任何参数。"""
        effort = (effort or "auto").strip().lower()
        if effort not in {"off", "low", "medium", "high"}:
            return {}
        if request_format == "lm_studio":
            # LM Studio 原生 API 支持 off/low/medium/high/on。
            return {"reasoning": effort if effort != "off" else "off"}
        if request_format == "ollama":
            # Ollama 支持布尔值以及 low/medium/high；保留用户选择的强度。
            return {"think": False if effort == "off" else effort}
        if request_format == "openai_chat":
            # OpenAI 仅支持 low/medium/high；off 视为不启用（不发送字段）。
            if effort == "off":
                return {}
            return {"reasoning_effort": effort}
        if request_format == "codex_responses":
            if deepseek:
                # DeepSeek Responses API 的 effort 取值：none/low/high/max。
                # 应用四档 off/low/medium/high 据此映射（最高档 high→max，off→none 真正关思考）。
                mapping = {"off": "none", "low": "low", "medium": "high", "high": "max"}
                return {"reasoning": {"effort": mapping[effort]}}
            # OpenAI Codex Responses：低/中/高三档。
            if effort == "off":
                return {}
            return {"reasoning": {"effort": effort}}
        # gemini / claude 首期保持自动，不发送未验证字段。
        return {}

    @staticmethod
    def _read_ollama_stream(response: Any, status: StatusCallback | None) -> dict[str, Any]:
        chunks: list[dict[str, Any]] = []
        full_content_parts: list[str] = []
        pending = ""
        reasoning_parts: list[str] = []
        reasoning_streamer = _ReasoningStreamer(status, reasoning_parts)
        tool_protocol = False
        inline_parser = _InlineReasoningParser()
        native_tool_calls: dict[int, dict[str, str]] = {}
        for raw_line in response:
            try:
                chunk = json.loads(raw_line.decode("utf-8", errors="replace"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(chunk, dict):
                continue
            chunks.append(chunk)
            message = chunk.get("message") or {}
            for index, raw_call in enumerate(message.get("tool_calls") or [] if isinstance(message, dict) else []):
                if not isinstance(raw_call, dict):
                    continue
                function = raw_call.get("function") or {}
                raw_arguments = function.get("arguments", {})
                arguments = raw_arguments if isinstance(raw_arguments, str) else json.dumps(raw_arguments, ensure_ascii=False)
                native_tool_calls[index] = {
                    "id": str(raw_call.get("id") or ""),
                    "name": str(function.get("name") or ""),
                    "arguments": arguments,
                }
            text, reasoning = ModelRuntime._ollama_stream_delta(chunk)
            text, inline_reasoning = inline_parser.feed(text)
            reasoning = reasoning + inline_reasoning
            if reasoning:
                reasoning_streamer.feed(reasoning)
            if not text:
                continue
            full_content_parts.append(text)
            if not tool_protocol:
                pending += text
                pending, tool_protocol = ModelRuntime._forward_guarded_text(pending, status)
        final_text, final_reasoning = inline_parser.feed("", final=True)
        if final_reasoning:
            reasoning_streamer.feed(final_reasoning)
        if final_text:
            full_content_parts.append(final_text)
            pending += final_text
        if not tool_protocol:
            pending, tool_protocol = ModelRuntime._forward_guarded_text(pending, status, final=True)
        reasoning_streamer.finish()
        return {
            "content": (
                ModelRuntime._build_action_from_native_tool_calls(native_tool_calls)
                if native_tool_calls
                else ModelRuntime._clean_content("".join(full_content_parts))
            ),
            "reasoning": "".join(reasoning_parts),
            "usage": ModelRuntime._online_usage("ollama", chunks),
        }

    @staticmethod
    def _ollama_stream_delta(chunk: dict[str, Any]) -> tuple[str, str]:
        message = chunk.get("message") or {}
        if not isinstance(message, dict):
            return "", ""
        return str(message.get("content") or ""), str(message.get("thinking") or "")

    @staticmethod
    def _read_sse_response(
        response: Any,
        request_format: str,
        status: StatusCallback | None,
    ) -> dict[str, Any]:
        """Collect SSE chunks while forwarding only ordinary prose to the chat client.

        Agent tool protocols (JSON actions, ``<tool_calls>`` / ``<invoke>``,
        DeepSeek ``<tool name="...">`` and native OpenAI ``tool_calls``) are
        buffered but never sent as ``delta`` events—they only reach the Agent
        Loop as the parsed action returned by ``complete``.
        """
        chunks: list[dict[str, Any]] = []
        full_content_parts: list[str] = []
        pending = ""
        reasoning_parts: list[str] = []
        reasoning_streamer = _ReasoningStreamer(status, reasoning_parts)
        native_tool_calls: dict[int, dict[str, str]] = {}
        tool_protocol = False
        inline_parser = _InlineReasoningParser()
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            chunks.append(chunk)
            text, reasoning, tool_calls = ModelRuntime._stream_delta_full(request_format, chunk)
            text, inline_reasoning = inline_parser.feed(text)
            reasoning = reasoning + inline_reasoning
            if reasoning:
                reasoning_streamer.feed(reasoning)
            if tool_calls:
                # Native OpenAI tool calls must not appear as answer text.
                if not tool_protocol:
                    ModelRuntime._forward_guarded_text(pending, status, final=True)
                    pending = ""
                tool_protocol = True
                for call in tool_calls:
                    slot = native_tool_calls.setdefault(
                        call.get("index", 0), {"id": "", "name": "", "arguments": ""}
                    )
                    if call.get("id"):
                        slot["id"] = call["id"]
                    if call.get("name"):
                        slot["name"] += call["name"]
                    if call.get("arguments") is not None:
                        slot["arguments"] += call["arguments"]
                continue
            if not text:
                continue
            full_content_parts.append(text)
            if not tool_protocol:
                pending += text
                pending, tool_protocol = ModelRuntime._forward_guarded_text(pending, status)
        final_text, final_reasoning = inline_parser.feed("", final=True)
        if final_reasoning:
            reasoning_streamer.feed(final_reasoning)
        if final_text:
            full_content_parts.append(final_text)
            pending += final_text
        if not tool_protocol:
            pending, tool_protocol = ModelRuntime._forward_guarded_text(pending, status, final=True)
        reasoning_streamer.finish()
        usage = ModelRuntime._online_usage(request_format, chunks)
        if native_tool_calls:
            # Convert to the internal action structure the Agent Loop consumes.
            content = ModelRuntime._build_action_from_native_tool_calls(native_tool_calls)
        else:
            content = ModelRuntime._clean_content("".join(full_content_parts))
        return {"content": content, "reasoning": "".join(reasoning_parts), "usage": usage}

    @staticmethod
    def _read_lm_studio_stream(response: Any, status: StatusCallback | None) -> dict[str, Any]:
        """按 LM Studio 原生 chat SSE 事件解析（``message.delta`` / ``reasoning.delta`` / ``error`` / ``chat.end``）。

        与 OpenAI 兼容流不同，LM Studio 的 ``type`` 事件直接携带 ``content`` 增量；结构化错误事件
        为 ``{"type":"error","error":...}``，结束事件为 ``{"type":"chat.end"}``。
        """
        chunks: list[dict[str, Any]] = []
        full_content_parts: list[str] = []
        pending = ""
        reasoning_parts: list[str] = []
        reasoning_streamer = _ReasoningStreamer(status, reasoning_parts)
        tool_protocol = False
        inline_parser = _InlineReasoningParser()
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            chunks.append(chunk)
            event_type = str(chunk.get("type") or "")
            if event_type == "error":
                error = chunk.get("error") or "LM Studio 返回未知错误"
                if isinstance(error, dict):
                    error = str(error.get("message") or error.get("details") or error)
                raise RuntimeError(f"LM Studio 流式错误：{error}")
            if event_type in ("chat.end", "message.end", "reasoning.end", "message.start", "reasoning.start"):
                continue
            if event_type in ("reasoning.delta", "reasoning.full"):
                reasoning = ModelRuntime._text_value(chunk.get("content"))
                if reasoning:
                    reasoning_streamer.feed(reasoning)
                continue
            if event_type in ("message.delta", "message.full"):
                text = ModelRuntime._text_value(chunk.get("content"))
            else:
                # 未知事件但可能带有正文/文本字段（兼容字段命名）。
                text = ModelRuntime._text_value(chunk.get("content") or chunk.get("text"))
            text, inline_reasoning = inline_parser.feed(text)
            if inline_reasoning:
                reasoning_streamer.feed(inline_reasoning)
            if not text:
                continue
            full_content_parts.append(text)
            if not tool_protocol:
                pending += text
                pending, tool_protocol = ModelRuntime._forward_guarded_text(pending, status)
        final_text, final_reasoning = inline_parser.feed("", final=True)
        if final_reasoning:
            reasoning_streamer.feed(final_reasoning)
        if final_text:
            full_content_parts.append(final_text)
            pending += final_text
        if not tool_protocol:
            pending, tool_protocol = ModelRuntime._forward_guarded_text(pending, status, final=True)
        reasoning_streamer.finish()
        return {
            "content": ModelRuntime._clean_content("".join(full_content_parts)),
            "reasoning": "".join(reasoning_parts),
            "usage": ModelRuntime._online_usage("lm_studio", chunks) if chunks else {},
        }

    @staticmethod
    def _stream_delta_full(request_format: str, chunk: dict[str, Any]) -> tuple[str, str, list[dict[str, str]]]:
        """Like ``_stream_delta`` but also extracts native OpenAI ``tool_calls``.

        Returns ``(text, reasoning, tool_calls)`` where ``tool_calls`` is a list
        of ``{"index", "id", "name", "arguments"}`` dicts accumulated by the caller.
        """
        text, reasoning = ModelRuntime._stream_delta(request_format, chunk)
        tool_calls: list[dict[str, str]] = []
        if request_format in {"openai_chat", "lm_studio"}:
            choices = chunk.get("choices") or []
            delta = (choices[0].get("delta") or {}) if choices and isinstance(choices[0], dict) else {}
            for raw_call in delta.get("tool_calls") or []:
                if not isinstance(raw_call, dict):
                    continue
                function = raw_call.get("function") or {}
                tool_calls.append(
                    {
                        "index": int(raw_call.get("index", 0) or 0),
                        "id": str(raw_call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "arguments": function.get("arguments") if isinstance(function.get("arguments"), str) else "",
                    }
                )
        elif request_format == "codex_responses":
            event_type = str(chunk.get("type") or "")
            if event_type == "response.output_item.added":
                item = chunk.get("item") or {}
                if isinstance(item, dict) and item.get("type") == "function_call":
                    tool_calls.append({
                        "index": int(chunk.get("output_index", 0) or 0),
                        "id": str(item.get("call_id") or item.get("id") or ""),
                        "name": str(item.get("name") or ""),
                        "arguments": str(item.get("arguments") or ""),
                    })
            elif event_type == "response.function_call_arguments.delta":
                tool_calls.append({
                    "index": int(chunk.get("output_index", 0) or 0),
                    "id": str(chunk.get("item_id") or ""),
                    "name": "",
                    "arguments": str(chunk.get("delta") or ""),
                })
        elif request_format == "claude":
            event_type = str(chunk.get("type") or "")
            index = int(chunk.get("index", 0) or 0)
            if event_type == "content_block_start":
                block = chunk.get("content_block") or {}
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    raw_input = block.get("input") or {}
                    tool_calls.append({
                        "index": index,
                        "id": str(block.get("id") or ""),
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(raw_input, ensure_ascii=False) if raw_input else "",
                    })
            elif event_type == "content_block_delta":
                delta = chunk.get("delta") or {}
                if isinstance(delta, dict) and delta.get("type") == "input_json_delta":
                    tool_calls.append({
                        "index": index,
                        "id": "",
                        "name": "",
                        "arguments": str(delta.get("partial_json") or ""),
                    })
        return text, reasoning, tool_calls

    @staticmethod
    def _build_action_from_native_tool_calls(native_tool_calls: dict[int, dict[str, str]]) -> str:
        """Reassemble streamed OpenAI ``tool_calls`` into the internal action JSON.

        Raises ``RuntimeError`` on malformed/unsupported calls so the original
        protocol is never forwarded to the chat answer.
        """
        actions = []
        for call in (native_tool_calls[index] for index in sorted(native_tool_calls)):
            name = call["name"].strip()
            if not name:
                logger.warning("工具调用解析失败：缺少工具名")
                raise RuntimeError("工具调用解析失败：缺少工具名")
            raw_args = call["arguments"] or "{}"
            try:
                arguments = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError as exc:
                # Large native tool-call arguments (e.g. a full workflow JSON) can
                # be truncated/malformed mid-stream. Do NOT abort the whole run with
                # a fatal error — surface a retryable "parse_error" action so the
                # agent loop asks the model to re-emit a clean tool call.
                logger.warning(
                    "工具调用解析失败：参数不是合法 JSON（len=%d, head=%r…）：%s",
                    len(raw_args), raw_args[:120], exc,
                )
                return json.dumps({"type": "parse_error"}, ensure_ascii=False)
            if not isinstance(arguments, dict):
                logger.warning("工具调用解析失败：参数不是 JSON 对象")
                return json.dumps({"type": "parse_error"}, ensure_ascii=False)
            actions.append({"type": "tool", "tool": name, "arguments": arguments})
        payload = actions[0] if len(actions) == 1 else {"type": "tools", "calls": actions}
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _classify_agent_output(buffer: str) -> str:
        """Classify the leading model output before forwarding it to the chat UI.

        Returns ``"tool"`` for an agent tool-call protocol (kept out of the
        visible answer), ``"text"`` for ordinary prose (safe to stream), or
        ``"pending"`` when the buffer is too short to decide confidently.
        """
        probe = buffer.lstrip()
        if not probe:
            return "pending"
        first = probe[0]
        if first in "{[":
            # JSON object / array agent action is never part of the answer.
            return "tool"
        if first == "<":
            match = re.match(r"^<([A-Za-z][\w-]*)", probe)
            if not match:
                return "text" if (">" in probe[:64] or len(probe) > 64) else "pending"
            tag = match.group(1).lower()
            if tag in {"tool_calls", "invoke"}:
                return "tool"
            if tag == "tool":
                # DeepSeek named-tool dialect: <tool name="..."> (optionally
                # wrapped in <tool type="tool">). Require a name/type attribute.
                if _TOOL_NAMED_ATTR.search(probe[:200]):
                    return "tool"
                if ">" in probe[:200]:
                    return "text"
                return "pending" if len(probe) <= _AGENT_BUFFER_LIMIT else "text"
            # Another tag (markdown/HTML in prose, <think>, ...): decide once
            # the opening tag closes; otherwise keep buffering briefly.
            if ">" in probe[:200]:
                return "text"
            return "pending" if len(probe) <= _AGENT_BUFFER_LIMIT else "text"
        return "text"

    @staticmethod
    def _tool_protocol_offset(buffer: str) -> int | None:
        """Return the earliest XML/JSON agent protocol marker in ``buffer``."""
        offsets = []
        xml_match = _TOOL_PROTOCOL_ANYWHERE.search(buffer)
        if xml_match:
            offsets.append(xml_match.start())
        json_match = _JSON_TOOL_ANYWHERE.search(buffer)
        if json_match:
            offsets.append(json_match.start())
        return min(offsets) if offsets else None

    @staticmethod
    def _possible_protocol_suffix_length(buffer: str) -> int:
        """Return only the ambiguous suffix that must wait for the next chunk.

        Ordinary answer text should be forwarded immediately.  We retain a
        short partial XML marker (for example ``<tool_ca``) or a partial JSON
        first field (for example ``{\"ty``), rather than delaying every stream
        by a fixed number of characters.
        """
        lower = buffer.lower()
        keep = 0
        for token in ("<tool_calls", "<invoke", "<tool"):
            limit = min(len(token) - 1, len(lower))
            for size in range(1, limit + 1):
                if token.startswith(lower[-size:]):
                    keep = max(keep, size)

        brace = buffer.rfind("{")
        if brace >= 0:
            fragment = buffer[brace:]
            rest = fragment[1:].lstrip()
            possible = not rest
            if rest.startswith('"'):
                field = rest[1:]
                if '"' in field:
                    name, tail = field.split('"', 1)
                    possible = name.lower() in {"type", "tool"} and not tail.strip()
                else:
                    possible = any(name.startswith(field.lower()) for name in ("type", "tool"))
            if possible:
                keep = max(keep, len(fragment))
        return keep

    @staticmethod
    def _emit_buffered_reasoning(status: StatusCallback | None, reasoning: str) -> None:
        """Publish reasoning only after the response is known to be user-facing."""
        if not status or not reasoning.strip():
            return
        status({"type": "reasoning_start"})
        status({"type": "reasoning_delta", "content": reasoning})
        status({"type": "reasoning_end"})

    @staticmethod
    def _forward_guarded_text(
        pending: str,
        status: StatusCallback | None,
        final: bool = False,
    ) -> tuple[str, bool]:
        """Forward safe prose while retaining enough tail to catch tool XML/JSON.

        Returns the unflushed tail and whether a tool protocol was detected.
        Once detected, callers suppress the remainder of that model response.
        """
        classification = ModelRuntime._classify_agent_output(pending)
        if classification == "tool":
            return "", True
        offset = ModelRuntime._tool_protocol_offset(pending)
        if offset is not None:
            visible = pending[:offset]
            if visible and status:
                status({"type": "delta", "content": visible})
            return "", True
        if final:
            if pending and status:
                status({"type": "delta", "content": pending})
            return "", False
        keep = ModelRuntime._possible_protocol_suffix_length(pending)
        if keep >= len(pending):
            return pending, False
        visible = pending[:-keep] if keep else pending
        if visible and status:
            status({"type": "delta", "content": visible})
        return pending[-keep:] if keep else "", False

    @staticmethod
    def _stream_delta(request_format: str, chunk: dict[str, Any]) -> tuple[str, str]:
        if request_format in {"openai_chat", "lm_studio"}:
            choices = chunk.get("choices") or []
            delta = (choices[0].get("delta") or {}) if choices and isinstance(choices[0], dict) else {}
            return (
                ModelRuntime._text_value(delta.get("content")),
                ModelRuntime._text_value(
                    delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking")
                ),
            )
        if request_format == "codex_responses":
            event_type = str(chunk.get("type") or "")
            if event_type.endswith("output_text.delta"):
                return str(chunk.get("delta") or ""), ""
            if "reasoning" in event_type and event_type.endswith("delta"):
                return "", str(chunk.get("delta") or "")
            return "", ""
        if request_format == "claude":
            delta = chunk.get("delta") or {}
            if str(chunk.get("type") or "") == "content_block_delta":
                return str(delta.get("text") or ""), str(delta.get("thinking") or "")
        return "", ""

    @staticmethod
    def _online_usage(request_format: str, result: Any) -> dict[str, int]:
        """把各供应商的 token usage 统一为输入、输出、总量和缓存命中数。"""
        chunks = result if isinstance(result, list) else [result]
        usage: dict[str, Any] = {}
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            candidate = chunk.get("usage") or chunk.get("usageMetadata") or chunk.get("stats")
            if request_format == "ollama" and any(
                key in chunk for key in ("prompt_eval_count", "eval_count")
            ):
                candidate = chunk
            # Responses 流式：usage 随最后一条 response.completed / response.incomplete
            # 事件嵌套在 response 对象里，不在顶层 chunk 上。
            if candidate is None and request_format == "codex_responses":
                response_obj = chunk.get("response")
                if isinstance(response_obj, dict):
                    candidate = response_obj.get("usage")
            if isinstance(candidate, dict):
                usage = candidate
        if not usage:
            return {}

        def number(*keys: str) -> int:
            for key in keys:
                value = usage.get(key)
                if value is not None:
                    try:
                        return max(0, int(value))
                    except (TypeError, ValueError):
                        continue
            return 0

        input_tokens = number(
            "prompt_tokens", "input_tokens", "inputTokens", "promptTokenCount", "inputTokenCount", "prompt_eval_count"
        )
        output_tokens = number(
            "completion_tokens", "output_tokens", "outputTokens", "candidatesTokenCount", "outputTokenCount", "eval_count"
        )
        total_tokens = number("total_tokens", "totalTokens", "totalTokenCount") or input_tokens + output_tokens
        cached_tokens = number(
            "cached_tokens", "cache_read_input_tokens", "cachedContentTokenCount",
            # DeepSeek reports the context-cache prefix hit via its own field.
            "prompt_cache_hit_tokens",
        )
        if request_format == "claude":
            # Anthropic separately reports uncached, cache-created and cache-read input tokens.
            input_tokens += cached_tokens + number("cache_creation_input_tokens")
            total_tokens = input_tokens + output_tokens
        for detail_key in ("prompt_tokens_details", "input_tokens_details"):
            details = usage.get(detail_key)
            if isinstance(details, dict):
                try:
                    cached_tokens = max(cached_tokens, int(details.get("cached_tokens") or 0))
                except (TypeError, ValueError):
                    pass
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cached_tokens": min(cached_tokens, input_tokens) if input_tokens else cached_tokens,
        }

    @staticmethod
    def _online_response(request_format: str, result: Any) -> tuple[str, str]:
        reasoning = ModelRuntime._online_reasoning(request_format, result)
        # Native OpenAI tool_calls (non-streaming) are converted to the internal
        # action structure so the Agent Loop can consume them directly.
        action = ModelRuntime._openai_tool_calls_action(result, request_format)
        if action is None:
            action = ModelRuntime._responses_tool_calls_action(result, request_format)
        if action is None:
            action = ModelRuntime._gemini_tool_calls_action(result, request_format)
        if action is None:
            action = ModelRuntime._claude_tool_calls_action(result, request_format)
        if action is not None:
            return action, reasoning
        content = ModelRuntime._online_content(request_format, result)
        if not content:
            content = ModelRuntime._reasoning_action(reasoning)
            if content:
                # 这是模型的 Agent 协议动作，不作为思考过程展示给用户。
                reasoning = ""
            else:
                raise RuntimeError(f"在线模型响应中没有文本内容：{str(result)[:1000]}")
        return ModelRuntime._clean_content(content), reasoning

    @staticmethod
    def _openai_tool_calls_action(result: Any, request_format: str) -> str | None:
        """Extract native OpenAI ``tool_calls`` from a non-streaming response.

        Returns the internal action JSON string, or ``None`` when the response
        has no tool calls. Raises ``RuntimeError`` on malformed calls.
        """
        if request_format not in {"openai_chat", "lm_studio", "ollama"} or not isinstance(result, dict):
            return None
        if request_format == "ollama":
            message = result.get("message") or {}
        else:
            choices = result.get("choices") or []
            message = (choices[0].get("message") or {}) if choices and isinstance(choices[0], dict) else {}
        tool_calls = message.get("tool_calls") if isinstance(message, dict) else None
        if not isinstance(tool_calls, list) or not tool_calls:
            return None
        calls: list[dict[str, str]] = []
        for raw_call in tool_calls:
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function") or {}
            raw_arguments = function.get("arguments", "")
            calls.append(
                {
                    "name": str(function.get("name") or ""),
                    "arguments": (
                        raw_arguments if isinstance(raw_arguments, str)
                        else json.dumps(raw_arguments, ensure_ascii=False)
                    ),
                }
            )
        native = {
            index: {"id": "", "name": call["name"], "arguments": call["arguments"]}
            for index, call in enumerate(calls)
        }
        return ModelRuntime._build_action_from_native_tool_calls(native)

    @staticmethod
    def _responses_tool_calls_action(result: Any, request_format: str) -> str | None:
        """Extract a native function call from a non-streaming Responses API result."""
        if request_format != "codex_responses" or not isinstance(result, dict):
            return None
        calls = [
            item for item in result.get("output") or []
            if isinstance(item, dict) and item.get("type") == "function_call"
        ]
        if not calls:
            return None
        native = {
            index: {
                "id": str(call.get("call_id") or call.get("id") or ""),
                "name": str(call.get("name") or ""),
                "arguments": str(call.get("arguments") or ""),
            }
            for index, call in enumerate(calls)
        }
        return ModelRuntime._build_action_from_native_tool_calls(native)

    @staticmethod
    def _gemini_tool_calls_action(result: Any, request_format: str) -> str | None:
        if request_format != "gemini":
            return None
        chunks = result if isinstance(result, list) else [result]
        calls: list[dict[str, Any]] = []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            candidates = chunk.get("candidates") or []
            content = (candidates[0].get("content") or {}) if candidates and isinstance(candidates[0], dict) else {}
            for part in content.get("parts") or []:
                call = part.get("functionCall") if isinstance(part, dict) else None
                if isinstance(call, dict):
                    calls.append(call)
        if not calls:
            return None
        native = {
            index: {
                "id": "",
                "name": str(call.get("name") or ""),
                "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
            }
            for index, call in enumerate(calls)
        }
        return ModelRuntime._build_action_from_native_tool_calls(native)

    @staticmethod
    def _claude_tool_calls_action(result: Any, request_format: str) -> str | None:
        if request_format != "claude" or not isinstance(result, dict):
            return None
        calls = [
            block for block in result.get("content") or []
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        if not calls:
            return None
        native = {
            index: {
                "id": str(call.get("id") or ""),
                "name": str(call.get("name") or ""),
                "arguments": json.dumps(call.get("input") or {}, ensure_ascii=False),
            }
            for index, call in enumerate(calls)
        }
        return ModelRuntime._build_action_from_native_tool_calls(native)

    @staticmethod
    def _claude_apply_cache_control(messages: list[dict[str, Any]]) -> None:
        """在最后一条"非 tool_result 的 user 消息"末尾追加 cache_control，启用前缀缓存。

        Anthropic 的 prompt caching 必须显式标记 ``cache_control: {"type": "ephemeral"}``
        才生效。约束：
        - 只能标记在 user 角色消息上（assistant 之后内容会打断缓存）；
        - 绝不能标记在含 tool_result 的 user 消息上（否则 API 400）。
        """
        for message in reversed(messages):
            if str(message.get("role") or "") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                if not content:
                    continue
                message["content"] = [{"type": "text", "text": content}]
                content = message["content"]
            if not isinstance(content, list) or not content:
                continue
            if any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content
            ):
                continue
            last_block = content[-1]
            if isinstance(last_block, dict):
                last_block["cache_control"] = {"type": "ephemeral"}
            return

    @staticmethod
    def _reasoning_action(reasoning: str) -> str:
        """只接受 reasoning_content 中完整、合法的 Agent 动作 JSON。"""
        cleaned = str(reasoning or "").strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            action = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            return ""
        if not isinstance(action, dict):
            return ""
        action_type = action.get("type")
        if action_type == "tool":
            if not str(action.get("tool") or "").strip():
                return ""
            if "arguments" in action and not isinstance(action.get("arguments"), dict):
                return ""
        elif action_type == "final":
            if not str(action.get("content") or "").strip():
                return ""
        else:
            return ""
        return json.dumps(action, ensure_ascii=False)

    @staticmethod
    def _with_endpoint(base_url: str, suffix: str) -> str:
        parsed = urllib.parse.urlsplit(base_url)
        target_path = suffix if suffix.startswith("/") else f"/{suffix}"
        base_path = parsed.path.rstrip("/")
        if base_path.endswith(target_path):
            return base_url
        path = ""
        for marker in ("/api/v1/", "/v1beta/", "/v1/"):
            if not target_path.startswith(marker):
                continue
            marker_root = marker.rstrip("/")
            rest = target_path[len(marker_root):]  # e.g. "/chat/completions"
            if base_path.endswith(marker_root):
                path = base_path + rest
                break
            marker_index = base_path.rfind(marker)
            if marker_index >= 0:
                path = base_path[:marker_index] + target_path
                break
            # base_url already carries an endpoint path (e.g. a full chat URL
            # pasted in) but omitted the API-version marker. Re-root it onto the
            # versioned target instead of doubling the path, which would otherwise
            # produce broken URLs such as .../chat/completions/v1/chat/completions.
            if base_path.endswith(rest):
                path = target_path
                break
        if not path:
            path = base_path + target_path
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))

    @staticmethod
    def _local_endpoint(base_url: str, path: str) -> str:
        parsed = urllib.parse.urlsplit(base_url)
        base_path = parsed.path.rstrip("/")
        if base_path.endswith("/v1"):
            base_path = base_path[:-3]
        target_path = f"{base_path}{path}" if base_path else path
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, target_path, parsed.query, parsed.fragment)
        )

    @staticmethod
    def _online_reasoning(request_format: str, result: Any) -> str:
        """尽力从响应中提取推理/思考内容（reasoning_content / thinking 等），无则返回空。"""
        if not isinstance(result, dict):
            return ""
        if request_format in {"openai_chat", "ollama"}:
            choices = result.get("choices") or []
            message = (choices[0].get("message") or {}) if choices else {}
            for key in ("reasoning_content", "reasoning", "thinking", "thought"):
                value = message.get(key) or result.get(key)
                if value:
                    return ModelRuntime._text_value(value)
            if request_format == "ollama":
                message = result.get("message") or {}
                if isinstance(message, dict) and message.get("thinking"):
                    return str(message["thinking"])
        if request_format in ("codex_responses", "lm_studio"):
            for item in result.get("output") or []:
                if isinstance(item, dict) and item.get("type") == "reasoning":
                    return ModelRuntime._text_value(item.get("content") or item.get("summary"))
        return ""

    @staticmethod
    def _online_content(request_format: str, result: Any) -> str:
        if request_format == "openai_chat":
            choices = result.get("choices") or [] if isinstance(result, dict) else []
            choice = choices[0] if choices else {}
            message = choice.get("message") or {}
            return ModelRuntime._text_value(message.get("content") or choice.get("text"))

        if request_format == "ollama":
            if isinstance(result, list):
                return "".join(ModelRuntime._online_content(request_format, item) for item in result)
            message = result.get("message") if isinstance(result, dict) else None
            return ModelRuntime._text_value(message.get("content") if isinstance(message, dict) else "")

        if request_format == "codex_responses":
            if not isinstance(result, dict):
                return ""
            if result.get("output_text"):
                return str(result["output_text"])
            texts = []
            for output in result.get("output") or []:
                for item in output.get("content") or []:
                    text = item.get("text") if isinstance(item, dict) else ""
                    if text:
                        texts.append(str(text))
            return "\n".join(texts)

        if request_format == "gemini":
            chunks = result if isinstance(result, list) else [result]
            texts = []
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    continue
                for candidate in chunk.get("candidates") or []:
                    for part in (candidate.get("content") or {}).get("parts") or []:
                        if isinstance(part, dict) and part.get("text"):
                            texts.append(str(part["text"]))
            return "".join(texts)

        if request_format == "claude":
            return ModelRuntime._text_value(result.get("content") if isinstance(result, dict) else None)

        if request_format == "lm_studio":
            if not isinstance(result, dict):
                return ""
            if result.get("output_text"):
                return str(result["output_text"])
            output = result.get("output") or result.get("choices") or []
            texts = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                # 推理类部件（type == "reasoning"）不属于正文，单独由 _online_reasoning 提取。
                if str(item.get("type") or "") == "reasoning":
                    continue
                value = item.get("content") or (item.get("message") or {}).get("content") or item.get("text")
                text = ModelRuntime._text_value(value)
                if text:
                    texts.append(text)
            return "\n".join(texts)
        return ""

    @staticmethod
    def _text_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n".join(
                str(item.get("text") or item.get("content") or "")
                for item in value if isinstance(item, dict) and (item.get("text") or item.get("content"))
            )
        return ""

    @staticmethod
    def _clean_content(content: str) -> str:
        text = content.strip()
        if "</think>" in text:
            text = text.rsplit("</think>", 1)[-1]
        text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
        return text.strip()
