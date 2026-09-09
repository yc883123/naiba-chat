# -*- coding: utf-8 -*-
"""为「用量速率」冒烟播种一条带 requests_detail 的助手消息（服务停止时执行）。

用法：先确保没有源码 server 占用 data/chat.db，然后
    python verify/seed_usage_message.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.storage.store import ChatStorage  # noqa: E402

TITLE = "用量速率冒烟"
STORAGE = ChatStorage(ROOT / "data" / "chat.db")

# 先清理旧会话，保证幂等（首条用户消息会把标题自动改成消息内容，故按关键字匹配）
for conversation in STORAGE.list_conversations():
    if "用量速率" in str(conversation.get("title") or ""):
        STORAGE.delete_conversation(str(conversation["id"]))

conversation = STORAGE.create_conversation(TITLE)
STORAGE.add_message(str(conversation["id"]), "user", "用量速率测试")
STORAGE.add_message(
    str(conversation["id"]),
    "assistant",
    "用量速率测试回复",
    {
        "usage": {
            "input_tokens": 2599,
            "output_tokens": 368,
            "total_tokens": 2967,
            "cached_tokens": 2367,
            "uncached_tokens": 232,
            "requests": 2,
            "cache_hit_rate": 91.1,
            "requests_detail": [
                {
                    "index": 1,
                    "input_tokens": 2367,
                    "output_tokens": 232,
                    "cached_tokens": 0,
                    "total_tokens": 2599,
                    "request_ms": 3200,
                },
                {
                    "index": 2,
                    "input_tokens": 2599,
                    "output_tokens": 368,
                    "cached_tokens": 2367,
                    "total_tokens": 2967,
                    "request_ms": 2000,
                },
            ],
        },
        "performance": {"total_ms": 5200},
    },
)
print(json.dumps({"conversation_id": conversation["id"], "title": TITLE}, ensure_ascii=False))
