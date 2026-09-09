# -*- coding: utf-8 -*-
"""为「对话刻度轨」冒烟播种一个多轮会话（服务停止时执行）。

播种 40 轮（user + assistant 各一条，共 80 条消息）——既覆盖常规滚动高亮，
也覆盖"超过 30 条时只显示视口附近 30 条"的滑动窗口。

用法：
    python verify/seed_turn_rail_chat.py            # 播种（备份 chat.db）
    python verify/seed_turn_rail_chat.py --restore  # 还原备份
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "chat.db"
BACKUP = ROOT / "verify" / "chatdb.before_turn_rail"
TITLE = "刻度轨冒烟会话"
TURNS = 40


def restore() -> None:
    if not BACKUP.exists():
        raise SystemExit("没有备份可还原")
    shutil.copyfile(BACKUP, DB)
    print("已还原 data/chat.db")


def seed() -> None:
    from naiba.storage.store import ChatStorage

    if DB.exists() and not BACKUP.exists():
        shutil.copyfile(DB, BACKUP)
        print(f"已备份 data/chat.db -> {BACKUP}")

    storage = ChatStorage(DB)
    for conversation in storage.list_conversations():
        if str(conversation.get("title") or "") == TITLE:
            storage.delete_conversation(str(conversation["id"]))

    conversation = storage.create_conversation(title=TITLE, permission_mode="auto")
    conversation_id = str(conversation["id"])
    for index in range(1, TURNS + 1):
        storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content=f"用户第 {index} 轮：请说明第 {index} 个问题。",
            metadata={},
        )
        storage.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=(
                f"AI 第 {index} 轮回复：这是第 {index} 轮的回答内容，用于验证右侧刻度轨的概要弹窗。"
                "这里再补一段较长的说明文字，确保超过两行时会被省略号截断，"
                "并且不会把弹窗撑得太高。"
            ),
            metadata={},
        )
    # add_message 会用首条用户消息改写标题，最后再改回固定标题（方便冒烟按标题定位）。
    storage.update_conversation_settings(conversation_id, title=TITLE)
    print(f"已播种会话 {conversation_id}（{TURNS} 轮 / {TURNS * 2} 条消息）")


if __name__ == "__main__":
    if "--restore" in sys.argv:
        restore()
    else:
        seed()
