# -*- coding: utf-8 -*-
"""Golden 基线录制器（行为变更时重录）。

运行（需沙箱补丁环境）：
  $env:TEMP/$env:TMP/$env:PYTHONPATH 指向 <项目根>\\.tmptest 后执行本文件。
产出：tests/golden/{cancel_race,tool_confirm,history_images}.json
录制后请人工核查 JSON 内容正确，再提交固化。
"""

import json
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))

from golden_replay import GOLDEN_NAMES, build  # noqa: E402

golden_dir = Path(PROJECT) / "tests" / "golden"
golden_dir.mkdir(parents=True, exist_ok=True)

# 基线存在时先备份为 .stale（万一录制结果异常可回退）
for name in GOLDEN_NAMES:
    path = golden_dir / f"{name}.json"
    if path.exists():
        os.replace(path, golden_dir / f"{name}.json.stale")

for name in GOLDEN_NAMES:
    data = build(name)
    path = golden_dir / f"{name}.json"
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"recorded {name} -> {path}")
print("done")
