# -*- coding: utf-8 -*-
"""为「侧栏收藏 + ⋯ 菜单 + 滚轮步进」冒烟播种会话（服务停止时执行）。

结构（幂等：先按标题前缀删除旧会话再重建）：
- 工作区「收藏冒烟A」：30 个会话（足够撑出滚动条，用于滚轮步进测量）；
- 工作区「收藏冒烟B」：3 个会话；
- 未分组：2 个会话；
- 已收藏：A 组第 1 个 + 未分组第 1 个（跨工作区，验证「已收藏」分组聚合）。

用法：先确保没有源码 server 占用 data/chat.db，然后
    python verify/seed_favorites.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.storage.store import ChatStorage  # noqa: E402

PREFIX = "收藏冒烟"
STORAGE = ChatStorage(ROOT / "data" / "chat.db")


def _delete_seeded() -> int:
    removed = 0
    for conversation in STORAGE.list_conversations():
        title = str(conversation.get("title") or "")
        provider = str(conversation.get("provider_id") or "")
        if title.startswith(PREFIX) or provider.startswith(PREFIX):
            STORAGE.delete_conversation(str(conversation["id"]))
            removed += 1
    return removed


def _create(title: str, group: str, favorite: bool = False) -> str:
    conversation = STORAGE.create_conversation(
        title=title, workspace_group=group, permission_mode="auto"
    )
    conversation_id = str(conversation["id"])
    if favorite:
        # 收藏只改标记、不推进 updated_at（与前端/接口同口径）。
        STORAGE.set_conversation_favorite(conversation_id, True)
    return conversation_id


def main() -> int:
    removed = _delete_seeded()
    group_a = f"{PREFIX}A"
    group_b = f"{PREFIX}B"
    created: list[str] = []
    for index in range(30):
        created.append(_create(f"{PREFIX} A-{index + 1:02d}", group_a, favorite=index == 0))
    for index in range(3):
        created.append(_create(f"{PREFIX} B-{index + 1:02d}", group_b))
    for index in range(2):
        created.append(_create(f"{PREFIX} 未分组-{index + 1:02d}", "", favorite=index == 0))

    favorites = [
        item["title"]
        for item in STORAGE.list_conversations()
        if str(item.get("title") or "").startswith(PREFIX) and int(item.get("favorite") or 0) == 1
    ]
    print(f"已清理旧播种 {removed} 条；新建 {len(created)} 条；已收藏：{favorites}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
