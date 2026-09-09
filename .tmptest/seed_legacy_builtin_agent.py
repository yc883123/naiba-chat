# -*- coding: utf-8 -*-
"""为 Agent 卡片冒烟播种「已下线内置 Agent 的遗留副本」（服务停止时执行）。

复刻用户机器上的真实形态：config.json 里存着 id=dsh-standard、built_in=true 的条目。
启动后 ConfigStore._migrate_agent_builtin_flags() 应摘掉 built_in，使其变成普通可删 Agent。

用法：
    python .tmptest/seed_legacy_builtin_agent.py          # 播种（先备份 config.json）
    python .tmptest/seed_legacy_builtin_agent.py --restore # 还原备份
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.json"
BACKUP = ROOT / ".tmptest" / "config.before_agent_smoke.json"

LEGACY = {
    "id": "dsh-standard",
    "name": "dsh-standard（全能）",
    "system_prompt": "你是 naiba-chat 的全能内置 Agent（历史预设副本）。",
    "skill_ids": [],
    "tool_scope": [],
    "built_in": True,
}


def restore() -> None:
    if not BACKUP.exists():
        raise SystemExit("没有备份可还原")
    shutil.copyfile(BACKUP, CONFIG)
    print("已还原 config.json")


def seed() -> None:
    if not BACKUP.exists():
        shutil.copyfile(CONFIG, BACKUP)
        print(f"已备份 config.json -> {BACKUP}")
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    agents = data.setdefault("agents", [])
    agents = [agent for agent in agents if str(agent.get("id") or "") != LEGACY["id"]]
    agents.append(dict(LEGACY))
    data["agents"] = agents
    CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已播种遗留内置副本：{LEGACY['id']}（built_in=true）")


if __name__ == "__main__":
    if "--restore" in sys.argv:
        restore()
    else:
        seed()
