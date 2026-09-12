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
version = update["version"]        # 如 2.3.5-beta
series = version.split("-")[0]     # 2.3.5
print(f"[§2/§3] release_notes {len(notes)} 条，与更新清单一致 = {notes == update['release_notes']}，"
      f"version = {version}")

# ---- 2) README 版本串（版本号从更新清单派生；曾写死 2.1.0 而长期失效）----
readme = (ROOT / "README.md").read_text(encoding="utf-8")
for token in (f"# Naiba Chat {series} Beta", f"## {series} Beta 主要能力",
              f"naiba-chat-{version}-windows-x64.zip",
              f'NAIBA_BUILD_VERSION = "{version}"'):
    print(f"[§4] README 含 {token!r}: {token in readme}")

# ---- 3) 工作流 YAML：三处版本串必须与清单一致（无 pyyaml 也照查）----
workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
try:
    import yaml  # type: ignore
    data = yaml.safe_load(workflow)
    print(f"[§1] YAML 解析 OK，env.RELEASE_VERSION = {data['env']['RELEASE_VERSION']}")
except ImportError:
    print("[§1] 未安装 pyyaml，跳过 YAML 解析（改用下面的正则口径）")
except Exception as exc:  # noqa: BLE001
    print(f"[§1] YAML 解析失败：{exc}")

for field, expect in (("RELEASE_VERSION", version),
                      ("RELEASE_TAG", f"v{version}"),
                      ("PACKAGE_NAME", f"naiba-chat-{version}-windows-x64")):
    found = re.search(rf"^\s*{field}:\s*(\S+)\s*$", workflow, re.M)
    actual = found.group(1) if found else "(未找到)"
    suffix = "" if actual == expect else f"  ← 与清单不符（应为 {expect}）"
    print(f"[§1] {field} = {actual}{suffix}")
for token in (f"name: Naiba Chat {series} Beta", f"Publish {series} Beta release"):
    print(f"[§1] 工作流含 {token!r}: {token in workflow}")

# ---- 4) 本地绝对路径审计 ----
PATTERNS = {
    "Windows 用户目录": re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"'`)]+"),
    "盘符绝对路径": re.compile(r"\b[A-Za-z]:\\(?!\.\.)[^\s\"'`)]*"),
}
TARGETS = [ROOT / "项目维护说明（修改代码前必读）.md"]
TARGETS += sorted(p for p in (ROOT / "verify").rglob("*") if p.is_file())

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
