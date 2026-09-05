"""工具协议解析（原 SkillAgent XML/JSON 动作解析，纯函数化）。"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Any


_TOOL_OPEN_TAG = re.compile(r"^<(tool_calls|invoke|tool)\b", re.IGNORECASE)
_TOOL_NAMED_ATTR = re.compile(r"\b(?:name|type)\s*=")






def _parse_action(text: str) -> dict[str, Any]:
    xml_action = _extract_xml_tool_action(text)
    if xml_action:
        return xml_action
    parsed = _extract_json(text)
    if isinstance(parsed, dict) and parsed.get("type") in {"tool", "tools", "final"}:
        return parsed
    # The output clearly intends an agent tool action but could not be
    # parsed (truncated tag, malformed JSON, ...). Signal a parse failure
    # instead of leaking the raw protocol as the answer.
    if _looks_like_tool_protocol(text):
        return {"type": "parse_error"}
    return {"type": "final", "content": text.strip()}


def _looks_like_tool_protocol(text: str) -> bool:
    """Heuristic: does ``text`` look like an agent tool-call protocol that
    merely failed to parse, rather than a plain-language answer?"""
    probe = (text or "").lstrip()
    if not probe:
        return False
    # Models sometimes emit a short natural-language preface before the
    # action. Still classify the embedded protocol as an action so it is
    # never persisted as the assistant's visible answer.
    if re.search(r"<(?:tool_calls|invoke|tool)\b", probe, flags=re.IGNORECASE):
        return True
    if re.search(r'\{[\s\S]{0,96}"(?:type|tool)"\s*:', probe, flags=re.IGNORECASE):
        return True
    first = probe[0]
    if first in "{[":
        # JSON/array action schema: only treat as a protocol when it
        # carries the action-style ``"type"``/``"tool"`` key, so an ordinary
        # JSON answer is still shown to the user.
        return bool(re.search(r'"(?:type|tool)"\s*:', probe[:200]))
    if first == "<":
        if _TOOL_OPEN_TAG.match(probe):
            if probe[:4].lower() == "<tool":
                return bool(_TOOL_NAMED_ATTR.search(probe[:200]))
            return True
    return False


def _extract_xml_tool_action(text: str) -> dict[str, Any] | None:
    """Accept XML tool-call dialects emitted by some OpenAI-compatible models."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return None
    # Models occasionally wrap the protocol in a markdown XML fence.
    cleaned = re.sub(r"^```(?:xml)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    # DeepSeek-compatible endpoints may emit ``<tool name="...">``
    # wrapped in an outer ``<tool type="tool">`` block. Some versions
    # append a mismatched ``</invoke>`` marker, so parse the named block
    # directly instead of requiring the entire response to be valid XML.
    named_tool = re.search(
        r"<tool\b[^>]*\bname\s*=\s*['\"]([^'\"]+)['\"][^>]*>(.*?)</tool>",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if named_tool:
        tool = named_tool.group(1).strip()
        body = named_tool.group(2)
        arguments = _parse_xml_parameters(body)
        return {"type": "tool", "tool": tool, "arguments": arguments}

    if "<invoke" not in cleaned:
        return None
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError:
        return None
    invokes = [root] if root.tag.rsplit("}", 1)[-1] == "invoke" else [
        node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "invoke"
    ]
    if len(invokes) != 1:
        return None
    invoke = invokes[0]
    tool = str(invoke.attrib.get("name") or "").strip()
    if not tool:
        return None
    arguments: dict[str, Any] = {}
    for parameter in invoke:
        if parameter.tag.rsplit("}", 1)[-1] != "parameter":
            continue
        name = str(parameter.attrib.get("name") or "").strip()
        if not name:
            continue
        value = "".join(parameter.itertext()).strip()
        if value:
            try:
                arguments[name] = json.loads(value)
            except json.JSONDecodeError:
                arguments[name] = value
        else:
            arguments[name] = ""
    return {"type": "tool", "tool": tool, "arguments": arguments}


def _parse_xml_parameters(body: str) -> dict[str, Any]:
    """Parse parameter children from a named tool block."""
    arguments: dict[str, Any] = {}
    try:
        wrapper = ET.fromstring(f"<invoke>{body}</invoke>")
        parameters = list(wrapper)
    except ET.ParseError:
        parameters = []
        for match in re.finditer(
            r"<parameter\b[^>]*\bname\s*=\s*['\"]([^'\"]+)['\"][^>]*>(.*?)</parameter>",
            body,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            parameters.append((match.group(1), match.group(2)))

    for parameter in parameters:
        if isinstance(parameter, tuple):
            name, value = parameter
        else:
            if parameter.tag.rsplit("}", 1)[-1] != "parameter":
                continue
            name = str(parameter.attrib.get("name") or "").strip()
            value = "".join(parameter.itertext()).strip()
        name = str(name or "").strip()
        if not name:
            continue
        value = str(value or "").strip()
        if not value:
            arguments[name] = ""
            continue
        try:
            arguments[name] = json.loads(value)
        except json.JSONDecodeError:
            arguments[name] = value
    return arguments


def _extract_json(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[index:])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    return None


