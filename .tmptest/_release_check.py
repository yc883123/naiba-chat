# -*- coding: utf-8 -*-
"""发布清单同步自检 + 本地绝对路径审计（只读）。"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---- 1) 两份 release_notes 一致性 ----
notes = json.loads((ROOT / "release_notes.json").read_text(encoding="utf-8"))
update = json.loads((ROOT / "naiba-chat-update.json").read_text(encoding="utf-8"))
print(f"[§2/§3] release_notes {len(notes)} 条，与更新清单一致 = {notes == update['release_notes']}，"
      f"version = {update['version']}")

# ---- 2) README 版本串 ----
readme = (ROOT / "README.md").read_text(encoding="utf-8")
for token in ("# Naiba Chat 2.0.0 Beta", "## 2.0.0 Beta 主要能力",
              "naiba-chat-2.0.0-beta-windows-x64.zip", "NAIBA_BUILD_VERSION = \"2.0.0-beta\""):
    print(f"[§4] README 含 {token!r}: {token in readme}")

# ---- 3) 工作流 YAML ----
try:
    import yaml  # type: ignore
    data = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    print(f"[§1] YAML 解析 OK，env.RELEASE_VERSION = {data['env']['RELEASE_VERSION']}")
except ImportError:
    print("[§1] 未安装 pyyaml，跳过 YAML 解析")
except Exception as exc:  # noqa: BLE001
    print(f"[§1] YAML 解析失败：{exc}")

# ---- 4) 本地绝对路径审计 ----
PATTERNS = {
    "Windows 用户目录": re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"'`)]+"),
    "盘符绝对路径": re.compile(r"\b[A-Za-z]:\\(?!\.\.)[^\s\"'`)]*"),
}
TARGETS = [ROOT / "项目维护说明（修改代码前必读）.md"]
TARGETS += sorted(p for p in (ROOT / ".tmptest").rglob("*") if p.is_file())

print("\n=== 本地绝对路径审计 ===")
hits = 0
for path in TARGETS:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        continue
    for line_no, line in enumerate(text.split("\n"), 1):
        for label, pattern in PATTERNS.items():
            for match in pattern.findall(line):
                # 允许语义化写法：项目根\... / %LOCALAPPDATA%\... / C:\...（省略号示例）
                if "..." in match or "项目根" in match:
                    continue
                hits += 1
                rel = path.relative_to(ROOT)
                print(f"  {rel}:{line_no}  [{label}]  {match}")
print(f"命中 {hits} 处")
