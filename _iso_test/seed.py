# -*- coding: utf-8 -*-
"""Isolated regression seed: builds config.json, workspace files, and a DB with
one rich conversation (messages carrying metadata.files) plus 46 filler
conversations so the virtualized sidebar list overflows and can be scrolled."""
import json
import os
import struct
import sys
import time
import zlib
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
WS = BASE / "ws"

# ---------------- config.json ----------------
CONFIG = {
    "host": "127.0.0.1",
    "port": 8787,
    "access_token": "",
    "skills_dirs": [],
    "workspace_dir": "ws",
    "data_dir": "data",
    "providers": [],
    "default_agent_id": "general",
    "agents": [
        {"id": "general", "name": "通用 Agent", "system_prompt": "", "skill_ids": []},
    ],
}
(BASE / "config.json").write_text(json.dumps(CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
print("config.json written")

# ---------------- workspace files ----------------
WS.mkdir(parents=True, exist_ok=True)
(WS / "rel.txt").write_text(
    "说明：这是一个被「编辑」过的普通文本文件。\n\n"
    "第二段内容，用来测试右侧面板的纯文本预览与编辑回写。\n"
    "行末标记 L3 便于观察保存后的变化。\n",
    encoding="utf-8",
)
(WS / "note.md").write_text(
    "# 项目说明（markdown 富文本预览）\n\n"
    "这里是**加粗**与 `行内代码` 的中文段落，用于验证面板把 .md 渲染成富文本而不是纯文本。\n\n"
    "## 二级标题\n\n"
    "- 列表项 A：面板应显示为项目符号\n"
    "- 列表项 B：换行后保持缩进\n\n"
    "> 引用块：右侧边栏「查看文件 + 手动编辑」能力验证。\n\n"
    "```python\n"
    "def hello(name: str) -> str:\n"
    "    return f\"你好，{name}\"\n"
    "```\n",
    encoding="utf-8",
)
(WS / "code.py").write_text(
    "# demo.py —— 编辑前后端预览的代码文件\n"
    "import os\n\n"
    "def main():\n"
    "    for name in os.listdir('.'):\n"
    "        print(name)  # 列出当前目录\n\n"
    "if __name__ == '__main__':\n"
    "    main()\n",
    encoding="utf-8",
)


def make_png(path: Path, width: int, height: int):
    def chunk(tag: bytes, data: bytes) -> bytes:
        block = tag + data
        return struct.pack(">I", len(data)) + block + struct.pack(">I", zlib.crc32(block))

    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter none
        for x in range(width):
            r = int(60 + 190 * x / width)
            g = int(40 + 150 * (1 - x / width))
            b = int(120 + 110 * (y / height))
            rows += bytes((r, g, b))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


make_png(WS / "img.png", 320, 160)
print("workspace files written:", sorted(p.name for p in WS.iterdir()))

# ---------------- DB seed ----------------
sys.path.insert(0, str(BASE))
from storage import ChatStorage  # noqa: E402

DB = ChatStorage(DATA / "chat.db")

# 46 filler conversations, slightly staggered so ordering is deterministic.
for i in range(46):
    time.sleep(0.003)
    DB.create_conversation(title=f"历史会话 {i + 1:02d}（用于侧栏滚动回归）", permission_mode="auto")

# Target conversation created last -> newest updated_at -> first in sidebar.
target = DB.create_conversation(title="UI 回归：修改文件与右侧面板", permission_mode="auto")
cid = target["id"]
DB.add_message(
    cid,
    "user",
    "请帮我新建一个项目说明文件，并编辑一份文本、写一段脚本，最后生成一张渐变图。",
)
DB.add_message(
    cid,
    "assistant",
    "已完成全部文件改动，汇总如下：\n\n"
    "1. 新建了 **note.md**，用 Markdown 撰写了项目说明，右侧面板会以富文本展示；\n"
    "2. **编辑了 rel.txt**，在原文本基础上补充了第二段；\n"
    "3. 新建了 **code.py**，脚本遍历当前目录并打印文件名；\n"
    "4. 生成了一张 **img.png** 渐变图，可直接预览。\n\n"
    "每条文件都出现在本消息末尾的「本轮修改文件」卡片里，桌面端点击卡片即可在右侧面板打开预览或编辑。",
    metadata={
        "files": [
            {"path": "rel.txt", "name": "rel.txt", "op": "edit"},
            {"path": "note.md", "name": "note.md", "op": "write"},
            {"path": "code.py", "name": "code.py", "op": "write"},
            {"path": "img.png", "name": "img.png", "op": "write"},
        ],
        "usage": {"total_tokens": 321},
    },
)
# Second assistant message with only an edit, so chips group by operation too.
DB.add_message(
    cid,
    "user",
    "再微调一下 rel.txt 的结尾。",
)
DB.add_message(
    cid,
    "assistant",
    "已把 rel.txt 的行末标记改成 L3。",
    metadata={
        "files": [{"path": "rel.txt", "name": "rel.txt", "op": "edit"}],
    },
)

meta_path = BASE / "target_conv.json"
meta_path.write_text(json.dumps({"conversation_id": cid}, ensure_ascii=False), encoding="utf-8")
print("target conversation:", cid, "->", meta_path)
print("total conversations:", len(DB.list_conversations()))
