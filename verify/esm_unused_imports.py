# -*- coding: utf-8 -*-
"""ESM 转换后未使用 import 普查（词边界计数，剔除 import 行自身与注释）。"""
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
JS_DIR = Path(__file__).resolve().parents[1] / "public" / "js"
IMP_RE = re.compile(r'^import\s*\{([^}]*)\}\s*from')

for path in sorted(JS_DIR.glob("*.js")):
    lines = path.read_text(encoding="utf-8").splitlines()
    unused = []
    for text in lines:
        m = IMP_RE.match(text)
        if not m:
            continue
        for name in [n.strip() for n in m.group(1).split(",")]:
            if not name:
                continue
            pat = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")
            # 代码正文：剔除 import 行与行注释
            bodies = [re.sub(r"//.*$", "", t.lstrip()) for t in lines if not IMP_RE.match(t)]
            code = "\n".join(bodies)
            if not pat.search(code):
                unused.append(name)
    if unused:
        print(f"{path.name}: 未使用 import → {unused}")
print("done")
