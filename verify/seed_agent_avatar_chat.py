# -*- coding: utf-8 -*-
"""为「Agent 头像」冒烟播种数据（服务停止时执行）。

播种内容：
- 一个带自定义头像的 Agent（id 固定 `agent_avatar_smoke`，头像用 PIL 生成后走
  `store_agent_avatar` 真实落盘）；
- 一个绑定该 Agent 的会话 + 一条助手消息（用于验证聊天气泡头像替换「AI」圆标）。

用法：
    python verify/seed_agent_avatar_chat.py           # 播种（备份 config.json / chat.db）
    python verify/seed_agent_avatar_chat.py --restore  # 还原备份
"""
from __future__ import annotations

import io
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CONFIG = ROOT / "config.json"
DB = ROOT / "data" / "chat.db"
BACKUP_CONFIG = ROOT / "verify" / "config.before_avatar_smoke.json"
BACKUP_DB = ROOT / "verify" / "chatdb.before_avatar_smoke"

AGENT_ID = "agent_avatar_smoke"
AGENT_NAME = "冒烟头像Agent"
CONV_TITLE = "头像冒烟会话"


def _avatar_png() -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (600, 300), (250, 250, 252))
    draw = ImageDraw.Draw(img)
    draw.ellipse((60, 30, 260, 230), fill=(120, 80, 200))
    draw.rectangle((320, 90, 540, 210), fill=(60, 180, 140))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def restore() -> None:
    if BACKUP_CONFIG.exists():
        shutil.copyfile(BACKUP_CONFIG, CONFIG)
        print("已还原 config.json")
    if BACKUP_DB.exists():
        DB.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(BACKUP_DB, DB)
        print("已还原 data/chat.db")


def seed() -> None:
    from naiba.storage.avatars import store_agent_avatar
    from naiba.storage.store import ChatStorage

    if not BACKUP_CONFIG.exists():
        shutil.copyfile(CONFIG, BACKUP_CONFIG)
        print(f"已备份 config.json -> {BACKUP_CONFIG}")
    if DB.exists() and not BACKUP_DB.exists():
        shutil.copyfile(DB, BACKUP_DB)
        print(f"已备份 data/chat.db -> {BACKUP_DB}")

    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    agents = [a for a in data.get("agents", []) if a.get("id") != AGENT_ID]
    data_dir = ROOT / "data"
    avatar = store_agent_avatar(data_dir, AGENT_ID, _avatar_png())
    agents.append({
        "id": AGENT_ID,
        "name": AGENT_NAME,
        "system_prompt": "你是头像冒烟测试用的 Agent。",
        "skill_ids": [],
        "tool_scope": [],
        "avatar": avatar,
    })
    data["agents"] = agents
    CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已播种 Agent {AGENT_ID}（avatar={avatar}）")

    storage = ChatStorage(DB)
    for conversation in storage.list_conversations():
        if str(conversation.get("title") or "") == CONV_TITLE:
            storage.delete_conversation(str(conversation["id"]))
    conversation = storage.create_conversation(
        title=CONV_TITLE, agent_id=AGENT_ID, permission_mode="auto"
    )
    conversation_id = str(conversation["id"])
    storage.add_message(
        conversation_id=conversation_id,
        role="assistant",
        content="这是头像冒烟会话的助手消息，用来验证气泡头像已替换为自定义图片。",
        metadata={},
    )
    print(f"已播种会话 {conversation_id}（绑定 {AGENT_ID} + 1 条助手消息）")


if __name__ == "__main__":
    if "--restore" in sys.argv:
        restore()
    else:
        seed()
