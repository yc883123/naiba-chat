# -*- coding: utf-8 -*-
"""角色卡解析（SillyTavern ccv3/chara PNG 元数据）——层级 1 纯函数。"""

from __future__ import annotations

import base64
import gzip
import json
import re
import struct
from pathlib import Path
from typing import Any


def _decode_card_payload(raw: str, try_gzip: bool) -> dict[str, Any]:
    """base64 解码角色卡字段（ccv3/chara），gzip 容错后 json 解析。"""
    try:
        payload = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("角色卡元数据 base64 解码失败") from exc
    if try_gzip and payload.startswith(b"\x1f\x8b"):
        try:
            payload = gzip.decompress(payload)
        except (OSError, EOFError):
            pass  # gzip 失败回退，按未压缩 JSON 处理
    try:
        value = json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("角色卡元数据不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("角色卡元数据格式异常")
    return value


def parse_sillytavern_card(data: bytes) -> dict[str, Any]:
    """解析 SillyTavern 角色卡 PNG 的 tEXt 元数据，归一化为人设 system_prompt 文本。

    返回 ``{"system_prompt": str, "meta": {"name", "creator", "tags"}}``。
    """
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("不是有效的 PNG 文件")

    text_chunks: dict[str, str] = {}
    offset = 8
    data_len = len(data)
    while offset + 8 <= data_len:
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        chunk_type = data[offset + 4:offset + 8].decode("latin-1")
        chunk_end = offset + 8 + length
        if chunk_end > data_len:
            break
        if chunk_type == "tEXt":
            chunk_data = data[offset + 8:chunk_end]
            if b"\x00" in chunk_data:
                keyword, _, text = chunk_data.partition(b"\x00")
                text_chunks[keyword.decode("latin-1")] = text.decode("latin-1")
        offset = chunk_end + 4  # 跳过 4 字节 CRC

    if not text_chunks:
        raise ValueError("该 PNG 不含角色卡元数据（tEXt 块）")

    card: dict[str, Any] = {}
    ccv3_raw = text_chunks.get("ccv3")
    if ccv3_raw:
        decoded = _decode_card_payload(ccv3_raw, try_gzip=True)
        data_field = decoded.get("data")
        card = data_field if isinstance(data_field, dict) else decoded
    if not card:
        chara_raw = text_chunks.get("chara")
        if chara_raw:
            decoded = _decode_card_payload(chara_raw, try_gzip=True)
            data_field = decoded.get("data")
            card = data_field if isinstance(data_field, dict) else decoded

    if not isinstance(card, dict) or not card:
        raise ValueError("未能从 PNG 中解析出角色卡内容")

    def _field(*keys: str) -> str:
        for key in keys:
            value = card.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    name = _field("name")
    description = _field("description")
    personality = _field("personality")
    scenario = _field("scenario")
    first_mes = _field("first_mes")
    mes_example = _field("mes_example")
    system_prompt = _field("system_prompt")
    post_history_instructions = _field("post_history_instructions")
    creator = _field("creator")

    tags_value = card.get("tags") or []
    if isinstance(tags_value, str):
        tags = [t.strip() for t in tags_value.split(",") if t.strip()]
    elif isinstance(tags_value, list):
        tags = [str(t).strip() for t in tags_value if str(t).strip()]
    else:
        tags = []

    parts: list[str] = [
        "以下为你的角色设定，你必须以该角色身份、第一人称口吻回复所有消息。",
    ]
    if name:
        parts.append(f"姓名：{name}")
    if description:
        parts.append(description)
    if personality:
        parts.append(f"## 性格\n{personality}")
    if scenario:
        parts.append(f"## 背景 / 场景\n{scenario}")
    if mes_example:
        parts.append(f"## 说话示例\n{mes_example}")
    if system_prompt:
        parts.append(f"## 角色说明\n{system_prompt}")
    if post_history_instructions:
        parts.append(f"## 后置指令\n{post_history_instructions}")
    if first_mes:
        parts.append(f"## 开场白\n{first_mes}")

    return {
        "system_prompt": "\n\n".join(parts),
        "meta": {"name": name, "creator": creator, "tags": tags},
    }


