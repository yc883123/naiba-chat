# -*- coding: utf-8 -*-
"""给某单元工作流里含 <Audio 2> 的段补第二音轨（LoadAudio -> ref_audio_1）。

第二音轨文件名从 config.json 的 audios["2"] 读取（可用参数覆盖）。

用法：
  python fix_audio2.py 01                 # 处理 01单元工作流/，音轨2 取 config
  python fix_audio2.py 01 配角声音.wav      # 自定义第二音轨文件名
"""
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def load_audio2():
    p = BASE_DIR / "config.json"
    if p.exists():
        try:
            return (json.loads(p.read_text(encoding="utf-8")).get("audios") or {}).get("2", "")
        except Exception:
            pass
    return ""


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    unit = int(sys.argv[1])
    audio2 = sys.argv[2] if len(sys.argv) > 2 else load_audio2()
    if not audio2:
        print("[错误] 未指定第二音轨文件名，且 config.json 里没有 audios.2")
        return 1
    out_dir = BASE_DIR / f"{unit:02d}单元工作流"

    fixed = []
    for f in sorted(out_dir.glob(f"{unit:02d}-*.json")):
        w = json.loads(f.read_text(encoding="utf-8"))
        prompt = w["372"]["inputs"]["text"]
        if "<Audio 2>" not in prompt:
            continue
        node = w["316"]["inputs"]
        w["390"] = {"inputs": {"audio": audio2},
                    "class_type": "LoadAudio",
                    "_meta": {"title": "載入音訊"}}
        node["ref_audios.ref_audio_1"] = ["390", 0]
        f.write_text(json.dumps(w, ensure_ascii=False, indent=2), encoding="utf-8")
        fixed.append(f.name)
        print("fixed", f.name)

    if not fixed:
        print("没有需要补 <Audio 2> 的段")
    print("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
