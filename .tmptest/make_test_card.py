# -*- coding: utf-8 -*-
"""生成一张 SillyTavern 格式的角色卡 PNG（Agent 页「导入角色卡」冒烟用）。

用法：python .tmptest/make_test_card.py
输出：data/generated/seed_fixture/收藏冒烟角色卡.png
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, PngImagePlugin  # noqa: E402

CARD_NAME = "收藏冒烟角色"
CARD_DESC = "沉默寡言的图书馆管理员，说话简短，喜欢用书比喻。"
OUT = ROOT / "data" / "generated" / "seed_fixture" / f"{CARD_NAME}卡.png"


def main() -> int:
    payload = {
        "name": CARD_NAME,
        "description": CARD_DESC,
        "personality": "冷静、克制",
        "scenario": "深夜的旧图书馆",
        "first_mes": "……需要什么书？",
        "creator": "naiba-smoke",
        "tags": ["冒烟", "测试"],
    }
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    info = PngImagePlugin.PngInfo()
    info.add_text("chara", encoded)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (256, 384), (38, 40, 58)).save(OUT, pnginfo=info)
    print(f"已生成 {OUT}（{OUT.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
