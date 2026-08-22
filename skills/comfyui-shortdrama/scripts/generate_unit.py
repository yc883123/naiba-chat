# -*- coding: utf-8 -*-
"""短剧单元批量生成（ComfyUI MiniMax H3 Ref2VA，通用模板）。

从同目录 config.json 读取 ComfyUI 地址、模板文件、参考图/参考音映射，
解析单元剧本 markdown，按 <Picture N>/<Audio N> 标签构建每段工作流并提交。

用法（先复制 config.example.json 为 config.json 并填好你的资产）：
  python generate_unit.py 01              # 生成 + 提交全部段并等待
  python generate_unit.py 01 build        # 只生成工作流 JSON，不提交
  python generate_unit.py 01 submit       # 生成 + 提交全部段
  python generate_unit.py 01 submit 3     # 只提交第 3 段（测试）
"""
import json
import re
import sys
import time
import uuid
import urllib.request
import urllib.error
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# 模板固定节点 id（对应 assets/MiniMaxH3-ref视频自动.json）
NODE_PROMPT = "372"                      # 提示词文本框
NODE_REFVIDEO = "316"                    # MiniMaxH3ReferenceToVideo
NODE_SAVE = "285"                        # SaveVideo
PICTURE_NODES = ["366", "367", "368"]    # 图1/2/3 加载器 -> ref_image_0/1/2
AUDIO_NODES = ["371", "390"]             # 音轨1/2 加载器 -> ref_audio_0/1（390 动态创建）

DEFAULT_CONFIG = {
    "comfyui_url": "http://127.0.0.1:8188",
    "template_file": "MiniMaxH3-ref视频自动.json",
    "output_prefix": "BOSS",
    "unit_glob": "单元{unit:02d}_*.md",
    "pictures": {"1": "", "2": "", "3": ""},
    "audios": {"1": "", "2": ""},
}

_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    p = BASE_DIR / "config.json"
    if p.exists():
        user = json.loads(p.read_text(encoding="utf-8"))
        for key, default in DEFAULT_CONFIG.items():
            if key not in user:
                continue
            if isinstance(default, dict):
                merged = dict(default)
                merged.update(user[key] or {})
                cfg[key] = merged
            else:
                cfg[key] = user[key]
    else:
        print("[提示] 未找到 config.json，使用默认空资产（请从 config.example.json 复制并填写）")
    return cfg


def cn_to_int(s):
    s = s.strip()
    if not s:
        return None
    if s in _CN:
        return _CN[s]
    if s == "十":
        return 10
    if s.startswith("十") and len(s) == 2:
        return 10 + _CN.get(s[1], 0)
    if s.endswith("十") and len(s) == 2:
        return _CN.get(s[0], 0) * 10
    if "十" in s:
        a, b = s.split("十", 1)
        return _CN.get(a, 0) * 10 + _CN.get(b, 0)
    try:
        return int(s)
    except ValueError:
        return None


def find_markdown(unit, glob_pattern):
    pats = list(BASE_DIR.glob(glob_pattern.format(unit=unit)))
    if not pats:
        raise FileNotFoundError(f"找不到剧本文件（模式 {glob_pattern}，unit={unit:02d}）")
    return pats[0]


def parse_markdown(path):
    """解析 markdown，返回 {段号: 英文H3提示词}（跳过中文翻译稿）。"""
    text = path.read_text(encoding="utf-8")
    parts = re.split(r"(?m)^## ", text)
    result = {}
    for part in parts[1:]:
        m = re.match(r"第([一二三四五六七八九十\d]+)段", part)
        if not m:
            continue
        idx = cn_to_int(m.group(1))
        if idx is None:
            continue
        marker = part.find("**H3 提示词**")
        if marker < 0:
            continue
        code_start = part.find("```text", marker)
        if code_start < 0:
            continue
        content_start = part.find("\n", code_start) + 1
        code_end = part.find("```", content_start)
        prompt = part[content_start:code_end].strip()
        if prompt:
            result[idx] = prompt
    return result


def build_workflow(base, prompt, cfg, prefix):
    w = json.loads(json.dumps(base))  # 深拷贝模板
    w[NODE_PROMPT]["inputs"]["text"] = prompt
    w[NODE_SAVE]["inputs"]["filename_prefix"] = prefix
    node = w[NODE_REFVIDEO]["inputs"]

    # 参考图：<Picture N> -> ref_image_(N-1)，节点 PICTURE_NODES[N-1]
    for i, node_id in enumerate(PICTURE_NODES):
        ordinal = str(i + 1)
        slot = f"ref_images.ref_image_{i}"
        filename = (cfg["pictures"].get(ordinal) or "").strip()
        if filename and f"<Picture {ordinal}>" in prompt and node_id in w:
            w[node_id]["inputs"]["upload_image"] = filename
            node[slot] = [node_id, 0]
        else:
            node.pop(slot, None)
            w.pop(node_id, None)

    # 参考音轨：<Audio N> -> ref_audio_(N-1)，节点 AUDIO_NODES[N-1]
    for i, node_id in enumerate(AUDIO_NODES):
        ordinal = str(i + 1)
        slot = f"ref_audios.ref_audio_{i}"
        filename = (cfg["audios"].get(ordinal) or "").strip()
        if filename and f"<Audio {ordinal}>" in prompt:
            if node_id not in w:
                w[node_id] = {"inputs": {"audio": filename},
                              "class_type": "LoadAudio",
                              "_meta": {"title": "載入音訊"}}
            else:
                w[node_id]["inputs"]["audio"] = filename
            node[slot] = [node_id, 0]
        else:
            node.pop(slot, None)
            w.pop(node_id, None)

    return w


def submit(wf, url):
    payload = json.dumps({"prompt": wf, "client_id": str(uuid.uuid4())}).encode("utf-8")
    req = urllib.request.Request(url + "/prompt", data=payload,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))["prompt_id"]


def wait_history(pid, url, timeout=7200, interval=8):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            h = json.load(urllib.request.urlopen(f"{url}/history/{pid}", timeout=60))
        except urllib.error.URLError:
            time.sleep(interval)
            continue
        if pid in h:
            return h[pid]
        time.sleep(interval)
    return None


def extract_outputs(entry):
    outs = []
    for _nid, node in (entry.get("outputs") or {}).items():
        for kind in ("images", "gifs", "videos"):
            for it in node.get(kind, []):
                outs.append(f"{kind}/{it.get('filename')}")
    return outs


def describe_refs(prompt, cfg):
    pics = [v for k, v in cfg["pictures"].items() if (v or "").strip() and f"<Picture {k}>" in prompt]
    auds = [v for k, v in cfg["audios"].items() if (v or "").strip() and f"<Audio {k}>" in prompt]
    return pics, auds


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--url")]
    if not args:
        print(__doc__)
        return 2
    unit = int(args[0])
    action = args[1] if len(args) > 1 else "submit"
    only = [int(a) for a in args[2:]] or None

    cfg = load_config()
    url = cfg["comfyui_url"]
    for a in sys.argv[1:]:
        if a.startswith("--url="):
            url = a.split("=", 1)[1]

    path = find_markdown(unit, cfg["unit_glob"])
    segments = parse_markdown(path)
    template_path = BASE_DIR / cfg["template_file"]
    base = json.loads(template_path.read_text(encoding="utf-8"))
    prefix = f"{cfg['output_prefix']}/{date.today().isoformat()}/{unit:02d}单元"
    out_dir = BASE_DIR / f"{unit:02d}单元工作流"
    out_dir.mkdir(exist_ok=True)

    print(f"单元 {unit:02d} | 解析到 {len(segments)} 段 | 模板 {template_path.name} | 前缀 {prefix}")

    if action == "build":
        for idx in sorted(segments):
            wf = build_workflow(base, segments[idx], cfg, prefix)
            out = out_dir / f"{unit:02d}-{idx:02d}.json"
            out.write_text(json.dumps(wf, ensure_ascii=False, indent=2), encoding="utf-8")
            pics, auds = describe_refs(segments[idx], cfg)
            print(f"[生成] {out.name}  图={'+'.join(pics) or '无'} 音频={'+'.join(auds) or '无'}")
        print("未提交（加 submit 参数提交）。")
        return 0

    targets = sorted(only) if only else sorted(segments)
    for idx in targets:
        wf = build_workflow(base, segments[idx], cfg, prefix)
        out = out_dir / f"{unit:02d}-{idx:02d}.json"
        out.write_text(json.dumps(wf, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n=== 提交 第{idx}段 ===")
        try:
            pid = submit(wf, url)
            print(f"prompt_id = {pid}")
            entry = wait_history(pid, url)
            if entry is None:
                print("[超时] 未在时限内完成")
                continue
            status = (entry.get("status") or {}).get("status_str", "?")
            if status == "success":
                print(f"[成功] {status}  outputs={extract_outputs(entry)}")
            else:
                msgs = [m for m in entry.get("messages", [])
                        if m[0] in ("execution_error", "execution_cached")]
                print(f"[失败] status={status} messages={msgs[:3]}")
        except Exception as e:
            print(f"[异常] {e}")
    print("ALL DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
