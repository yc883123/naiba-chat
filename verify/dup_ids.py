# -*- coding: utf-8 -*-
"""列出 index.html 中重复的 id（重复 id 会让 $('#x') 命中错误的元素）。"""
from __future__ import annotations

import collections
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
ids = re.findall(r'\bid="([^"]+)"', html)
dupes = {key: count for key, count in collections.Counter(ids).items() if count > 1}
print(f"total ids={len(ids)} duplicates={dupes}")
