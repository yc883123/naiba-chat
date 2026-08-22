# -*- coding: utf-8 -*-
"""把某单元已生成的工作流 JSON 一次性全部入队（fire-all），再逐个轮询到完成。

用法：
  python submit_unit.py 01           # 提交 01单元工作流/01-*.json 全部段
  python submit_unit.py 01 5 7       # 只提交第 5、7 段
  python submit_unit.py 01 --url http://127.0.0.1:8188
"""
import json
import sys
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_URL = "http://127.0.0.1:8188"


def load_url():
    p = BASE_DIR / "config.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("comfyui_url", DEFAULT_URL)
        except Exception:
            pass
    return DEFAULT_URL


def _post(wf, url):
    payload = json.dumps({"prompt": wf, "client_id": str(uuid.uuid4())}).encode("utf-8")
    req = urllib.request.Request(url + "/prompt", data=payload,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))["prompt_id"]


def extract_outputs(entry):
    outs = []
    for _nid, node in (entry.get("outputs") or {}).items():
        for kind in ("images", "gifs", "videos"):
            for it in node.get(kind, []):
                outs.append(f"{kind}/{it.get('filename')}")
    return outs


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--url")]
    if not args:
        print(__doc__)
        return 2
    unit = int(args[0])
    only = [int(a) for a in args[1:]] or None

    url = load_url()
    for a in sys.argv[1:]:
        if a.startswith("--url="):
            url = a.split("=", 1)[1]

    out_dir = BASE_DIR / f"{unit:02d}单元工作流"
    files = sorted(out_dir.glob(f"{unit:02d}-*.json"))
    if only:
        sel = set(only)
        files = [f for f in files if int(f.stem.split("-")[1]) in sel]
    if not files:
        print(f"没有找到 {out_dir} 下的工作流 JSON")
        return 1

    # fire-all：先把全部段提交进队列
    submitted = []
    for f in files:
        wf = json.loads(f.read_text(encoding="utf-8"))
        pid = _post(wf, url)
        submitted.append((f.stem, pid))
        print(f"[入队] {f.stem} prompt_id={pid}")

    # 再逐个轮询到完成
    for stem, pid in submitted:
        print(f"[等待] {stem} {pid}")
        while True:
            try:
                h = json.load(urllib.request.urlopen(f"{url}/history/{pid}", timeout=60))
            except urllib.error.URLError:
                time.sleep(8)
                continue
            if pid not in h:
                time.sleep(8)
                continue
            entry = h[pid]
            status = (entry.get("status") or {}).get("status_str", "?")
            if status == "success":
                print(f"[成功] {stem} outputs={extract_outputs(entry)}")
                break
            msgs = [m for m in entry.get("messages", [])
                    if m[0] in ("execution_error", "execution_cached")]
            print(f"[失败] {stem} status={status} messages={msgs[:3]}")
            break
        time.sleep(2)
    print("ALL DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
