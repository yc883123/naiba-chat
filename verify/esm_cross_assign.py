# -*- coding: utf-8 -*-
"""ESM 化后的跨文件赋值扫描：导入名在导入方被直接赋值/自增（只读绑定会抛错）。"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
JS_DIR = Path(__file__).resolve().parents[1] / "public" / "js"
DECL_RE = re.compile(r"^(?:export\s+)?(?:async\s+function\s+|function\s+|const\s+|let\s+|var\s+|class\s+)([A-Za-z_$][\w$]*)")
IMP_RE = re.compile(r'^import\s*\{([^}]*)\}\s*from\s*"\./([^"]+)"')

files = sorted(JS_DIR.glob("*.js"))
decl: dict[str, list[str]] = {}
for path in files:
    for text in path.read_text(encoding="utf-8").splitlines():
        m = DECL_RE.match(text)
        if m:
            decl.setdefault(m.group(1), []).append(path.name)

for path in files:
    lines = path.read_text(encoding="utf-8").splitlines()
    imported: dict[str, str] = {}
    for text in lines:
        m = IMP_RE.match(text)
        if m:
            for name in [n.strip() for n in m.group(1).split(",")]:
                if name:
                    imported[name] = m.group(2)
    for name, source in imported.items():
        if source not in decl.get(name, []):
            continue  # import 源并非该名字的声明处（不应发生）
        for idx, text in enumerate(lines, start=1):
            if IMP_RE.match(text):
                continue
            if re.match(rf"(?<![\w$.]){re.escape(name)}\s*(?:=(?!=)|\+=|-=|\*=|/=|%=|\+\+|--)", text):
                print(f"{path.name}:{idx}: 跨文件赋值 {name}（源 {source}）→ {text.strip()[:80]}")
print("done")
