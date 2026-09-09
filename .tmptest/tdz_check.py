# -*- coding: utf-8 -*-
"""TDZ 复查（在已转换文件上）：顶层 const/let 初始化器跨模块引用检测。"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
JS_DIR = Path(__file__).resolve().parents[1] / "public" / "js"
DECL_RE = re.compile(r"^(?:export\s+)?(?:async\s+function\s+|function\s+|const\s+|let\s+|var\s+|class\s+)([A-Za-z_$][\w$]*)")
INIT_RE = re.compile(r"^(?:export\s+)?(const|let)\s+([A-Za-z_$][\w$]*)\s*=")
WORD_RE = lambda name: re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")

files = sorted(JS_DIR.glob("*.js"))
decl: dict[str, list[str]] = {}
for path in files:
    for text in path.read_text(encoding="utf-8").splitlines():
        match = DECL_RE.match(text)
        if match:
            decl.setdefault(match.group(1), []).append(path.name)

hits = 0
for path in files:
    lines = path.read_text(encoding="utf-8").splitlines()
    declared_here = {m.group(1) for text in lines if (m := DECL_RE.match(text))}
    i = 0
    while i < len(lines):
        match = INIT_RE.match(lines[i])
        if not match:
            i += 1
            continue
        depth = 0
        j = i
        parts = []
        while j < len(lines):
            parts.append(lines[j])
            depth += sum(lines[j].count(c) for c in "{[(")
            depth -= sum(lines[j].count(c) for c in "}])")
            j += 1
            if ";" in parts[-1] and depth <= 0:
                break
        init = "\n".join(parts)
        for name, owners in decl.items():
            if name in declared_here or len(owners) != 1 or owners[0] == path.name:
                continue
            if WORD_RE(name).search(init):
                print(f"  {path.name}:{i + 1} {match.group(1)} {match.group(2)} 引用 {name}（{owners[0]}）")
                hits += 1
        i = j
print(f"TDZ 风险 {hits} 处" + ("（需人工评估）" if hits else "（预期为空）"))
