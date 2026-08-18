from __future__ import annotations

import json
import logging
import re
import threading
import time
from html.parser import HTMLParser
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

logger = logging.getLogger("naiba.model_runtime")


StatusCallback = Callable[[dict[str, Any]], None]
ONLINE_MODEL_TIMEOUT_SECONDS = 180
LOCAL_MODEL_TIMEOUT_SECONDS = 1800
PROVIDER_TEST_TIMEOUT_SECONDS = 30
FAST_RETRY_NETWORK_ERRORS = {10053, 10054, 10061}
# 本地推理后端对应的请求格式；与 server.LOCAL_REQUEST_FORMATS 保持一致。
LOCAL_REQUEST_FORMATS = {"ollama", "lm_studio", "llama_cpp"}

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
_STREAM_GUARD_CHARS = 128
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

    return summary[:800]


def _network_error_code(error: BaseException) -> int | None:
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    value = getattr(reason, "winerror", None) or getattr(reason, "errno", None)
    return int(value) if isinstance(value, int) else None


class ModelRuntime:
    """在线模型调用。"""

    def __init__(self) -> None:
        # 每个 HTTP 请求线程独立保存最近一次模型调用信息，避免并发对话互相覆盖。
        self._local = threading.local()

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
        content, reasoning, usage = self._complete_online(profile, messages, options, status)
        self.last_reasoning = reasoning
        self.last_usage = usage
        return content

    @staticmethod
    def _urlopen_cancelable(
        request: urllib.request.Request,
        timeout: float,
        cancel_event: threading.Event | None = None,
    ):
        """Run urllib in a daemon worker so a cancelled vision call returns promptly."""
        if cancel_event is None:
            return urllib.request.urlopen(request, timeout=timeout)
        if cancel_event.is_set():
            raise RuntimeError("任务已取消")
        done = threading.Event()
        result: dict[str, Any] = {}

        def worker() -> None:
            try:
                result["response"] = urllib.request.urlopen(request, timeout=timeout)
            except BaseException as exc:  # noqa: BLE001 - propagate worker errors
                result["error"] = exc
            finally:
                done.set()

        threading.Thread(target=worker, name="naiba-http-request", daemon=True).start()
        while not done.wait(0.1):
            if cancel_event.is_set():
                raise RuntimeError("任务已取消")
        if cancel_event.is_set():
            response = result.get("response")
            if response is not None:
                response.close()
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
    def _openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"role": item.get("role", "user"), "content": ModelRuntime._openai_content(item.get("content"))}
            for item in messages
        ]

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
        # llama.cpp server exposes the OpenAI Chat API, but it is a local
        # inference backend. Normalize only the wire protocol; retain the
        # profile kind below so timeout/retry routing stays local.
        request_format = "openai_chat" if configured_format == "llama_cpp" else configured_format
        if not base_url:
            raise ValueError("请先填写 API URL")

        headers = {"Accept": "application/json"}
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
            with urllib.request.urlopen(request, timeout=60) as response:
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
            raise RuntimeError(f"无法连接模型列表接口：{exc.reason}") from exc
        except OSError as exc:
            raise RuntimeError(f"模型列表连接被本机或远端中止：{exc}") from exc

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

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
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
    ) -> tuple[str, str, dict[str, int]]:
        base_url = str(profile.get("base_url") or "").rstrip("/")
        model = str(profile.get("model") or "").strip()
        api_key = str(profile.get("api_key") or "").strip()
        if not base_url or not model:
            raise ValueError("在线模型需要 Base URL 和模型名称")
        configured_format = str(profile.get("request_format") or "openai_chat").strip().lower()
        # llama.cpp server exposes the OpenAI Chat API, but it is a local
        # inference backend. Normalize only the wire protocol; retain the
        # profile kind below so timeout/retry routing stays local.
        request_format = "openai_chat" if configured_format == "llama_cpp" else configured_format
        temperature_raw = options.get("temperature", profile.get("temperature"))
        temperature = None if temperature_raw in (None, "") else float(temperature_raw)
        max_tokens_raw = options.get("max_tokens", profile.get("max_output_tokens"))
        max_tokens = None if max_tokens_raw in (None, "") else int(max_tokens_raw)
        stream_enabled = bool(options.get("stream", False))
        reasoning_effort = str(profile.get("reasoning_effort") or "auto").strip().lower()
        headers = {"Content-Type": "application/json"}

        if request_format == "openai_chat":
            endpoint = ModelRuntime._with_endpoint(base_url, "/v1/chat/completions")
            payload = {
                "model": model,
                "messages": ModelRuntime._openai_messages(messages),
                "stream": stream_enabled,
            }
            if temperature is not None:
                payload["temperature"] = temperature
            if max_tokens is not None:
                payload["max_tokens"] = max_tokens
            reasoning_params = ModelRuntime._reasoning_params(request_format, reasoning_effort)
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
                "input": [
                    {
                        "role": item.get("role", "user"),
                        "content": ModelRuntime._responses_content(
                            item.get("content"), str(item.get("role") or "user")
                        ),
                    }
                    for item in messages if item.get("role") != "system"
                ],
                "stream": stream_enabled,
            }
            if temperature is not None:
                payload["temperature"] = temperature
            if max_tokens is not None:
                payload["max_output_tokens"] = max_tokens
            if instructions:
                payload["instructions"] = instructions
            reasoning_params = ModelRuntime._reasoning_params(request_format, reasoning_effort)
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
                {
                    "role": "model" if item.get("role") == "assistant" else "user",
                    "parts": [
                        {"text": str(part.get("text") or "")}
                        if part.get("type") == "text"
                        else {
                            "inlineData": {
                                "mimeType": part.get("media_type") or "image/jpeg",
                                "data": part.get("data") or "",
                            }
                        }
                        for part in ModelRuntime._content_parts(item.get("content"))
                        if part.get("type") == "text" or (part.get("type") == "image" and part.get("data"))
                    ],
                }
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
                    {
                        "role": item.get("role", "user"),
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
                    for item in messages if item.get("role") != "system"
                ],
                "max_tokens": max_tokens if max_tokens is not None else 4096,
                "stream": stream_enabled,
            }
            if temperature is not None:
                payload["temperature"] = temperature
            if system:
                payload["system"] = system
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
        target_detail += f"（{endpoint_host}:{endpoint_port}）"
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
        for attempt in range(attempts):
            try:
                with ModelRuntime._urlopen_cancelable(request, request_timeout, cancel_event) as response:
                    if stream_enabled and request_format == "ollama":
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
                                    retry_request, request_timeout, cancel_event
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
                    if stream_enabled and request_format == "lm_studio":
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
                    if stream_enabled and request_format != "gemini":
                        streamed = ModelRuntime._read_sse_response(response, request_format, status)
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
                if (
                    not is_local
                    and not connection_test
                    and exc.code in {429, 502, 503, 504}
                    and attempt + 1 < attempts
                ):
                    if cancel_event and cancel_event.wait(1.5 * (attempt + 1)):
                        raise RuntimeError("任务已取消")
                    continue
                raise RuntimeError(f"{target_detail}返回 HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, OSError) as exc:
                reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                error_code = _network_error_code(exc)
                retryable_test_error = connection_test and error_code in FAST_RETRY_NETWORK_ERRORS
                if not is_local and attempt + 1 < attempts and (not connection_test or retryable_test_error):
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
                raise RuntimeError(f"无法连接{target_detail}：{reason}{hint}") from exc

        usage = ModelRuntime._online_usage(request_format, result)
        try:
            content, reasoning = ModelRuntime._online_response(request_format, result)
        except RuntimeError:
            reasoning = ModelRuntime._online_reasoning(request_format, result)
            if connection_test and reasoning:
                return "接口已返回有效响应", reasoning, usage
            raise
        return content, reasoning, usage

    @staticmethod
    def _ollama_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted = []
        for item in messages:
            content = item.get("content")
            message = {
                "role": item.get("role", "user"),
                "content": ModelRuntime._content_text(content) if not isinstance(content, str) else content,
            }
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
    def _reasoning_params(request_format: str, effort: str) -> dict[str, Any]:
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
        tool_protocol = False
        for raw_line in response:
            try:
                chunk = json.loads(raw_line.decode("utf-8", errors="replace"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(chunk, dict):
                continue
            chunks.append(chunk)
            text, reasoning = ModelRuntime._ollama_stream_delta(chunk)
            if reasoning:
                reasoning_parts.append(reasoning)
            if not text:
                continue
            full_content_parts.append(text)
            if not tool_protocol:
                pending += text
                pending, tool_protocol = ModelRuntime._forward_guarded_text(pending, status)
        if not tool_protocol:
            ModelRuntime._forward_guarded_text(pending, status, final=True)
        return {
            "content": ModelRuntime._clean_content("".join(full_content_parts)),
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
        native_tool_calls: dict[int, dict[str, str]] = {}
        tool_protocol = False
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
            if reasoning:
                reasoning_parts.append(reasoning)
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
        if not tool_protocol:
            ModelRuntime._forward_guarded_text(pending, status, final=True)
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
        tool_protocol = False
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
                reasoning_parts.append(ModelRuntime._text_value(chunk.get("content")))
                continue
            if event_type in ("message.delta", "message.full"):
                text = ModelRuntime._text_value(chunk.get("content"))
            else:
                # 未知事件但可能带有正文/文本字段（兼容字段命名）。
                text = ModelRuntime._text_value(chunk.get("content") or chunk.get("text"))
            if not text:
                continue
            full_content_parts.append(text)
            if not tool_protocol:
                pending += text
                pending, tool_protocol = ModelRuntime._forward_guarded_text(pending, status)
        if not tool_protocol:
            ModelRuntime._forward_guarded_text(pending, status, final=True)
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
        return text, reasoning, tool_calls

    @staticmethod
    def _build_action_from_native_tool_calls(native_tool_calls: dict[int, dict[str, str]]) -> str:
        """Reassemble streamed OpenAI ``tool_calls`` into the internal action JSON.

        Raises ``RuntimeError`` on malformed/unsupported calls so the original
        protocol is never forwarded to the chat answer.
        """
        calls = [native_tool_calls[index] for index in sorted(native_tool_calls)]
        if len(calls) != 1:
            logger.warning("工具调用解析失败：收到 %d 个并行 tool_calls，暂不支持", len(calls))
            raise RuntimeError(
                f"暂不支持并行工具调用（收到 {len(calls)} 个 tool_calls）；请一次只调用一个工具"
            )
        call = calls[0]
        name = call["name"].strip()
        if not name:
            logger.warning("工具调用解析失败：缺少工具名")
            raise RuntimeError("工具调用解析失败：缺少工具名")
        raw_args = call["arguments"] or "{}"
        try:
            arguments = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError as exc:
            logger.warning("工具调用解析失败：参数不是合法 JSON")
            raise RuntimeError(f"工具调用参数不是合法 JSON：{exc}") from exc
        if not isinstance(arguments, dict):
            logger.warning("工具调用解析失败：参数不是 JSON 对象")
            raise RuntimeError("工具调用参数必须是 JSON 对象")
        return json.dumps({"type": "tool", "tool": name, "arguments": arguments}, ensure_ascii=False)

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
    def _forward_guarded_text(
        pending: str,
        status: StatusCallback | None,
        final: bool = False,
    ) -> tuple[str, bool]:
        """Forward safe prose while retaining enough tail to catch tool XML/JSON.

        Returns the unflushed tail and whether a tool protocol was detected.
        Once detected, callers suppress the remainder of that model response.
        """
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
        if len(pending) <= _STREAM_GUARD_CHARS:
            return pending, False
        visible = pending[:-_STREAM_GUARD_CHARS]
        if visible and status:
            status({"type": "delta", "content": visible})
        return pending[-_STREAM_GUARD_CHARS:], False

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
            candidate = chunk.get("usage") or chunk.get("usageMetadata")
            if request_format == "ollama" and any(
                key in chunk for key in ("prompt_eval_count", "eval_count")
            ):
                candidate = chunk
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
            "prompt_tokens", "input_tokens", "promptTokenCount", "inputTokenCount", "prompt_eval_count"
        )
        output_tokens = number(
            "completion_tokens", "output_tokens", "candidatesTokenCount", "outputTokenCount", "eval_count"
        )
        total_tokens = number("total_tokens", "totalTokenCount") or input_tokens + output_tokens
        cached_tokens = number(
            "cached_tokens", "cache_read_input_tokens", "cachedContentTokenCount"
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
        if request_format not in {"openai_chat", "lm_studio"} or not isinstance(result, dict):
            return None
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
            calls.append(
                {
                    "name": str(function.get("name") or ""),
                    "arguments": function.get("arguments") if isinstance(function.get("arguments"), str) else "",
                }
            )
        if len(calls) != 1:
            logger.warning("工具调用解析失败：收到 %d 个并行 tool_calls，暂不支持", len(calls))
            raise RuntimeError(
                f"暂不支持并行工具调用（收到 {len(calls)} 个 tool_calls）；请一次只调用一个工具"
            )
        name = calls[0]["name"].strip()
        if not name:
            logger.warning("工具调用解析失败：缺少工具名")
            raise RuntimeError("工具调用解析失败：缺少工具名")
        raw_args = calls[0]["arguments"] or "{}"
        try:
            arguments = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError as exc:
            logger.warning("工具调用解析失败：参数不是合法 JSON")
            raise RuntimeError(f"工具调用参数不是合法 JSON：{exc}") from exc
        if not isinstance(arguments, dict):
            logger.warning("工具调用解析失败：参数不是 JSON 对象")
            raise RuntimeError("工具调用参数必须是 JSON 对象")
        return json.dumps({"type": "tool", "tool": name, "arguments": arguments}, ensure_ascii=False)

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
            if base_path.endswith(marker_root):
                path = base_path + target_path[len(marker_root):]
                break
            marker_index = base_path.rfind(marker)
            if marker_index >= 0:
                path = base_path[:marker_index] + target_path
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
