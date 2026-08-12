#!/usr/bin/env python3
"""ComfyUI HTTP 调用最小示例（不依赖 MCP 框架，用于调试/学习）。

用法:
  python example_client.py --prompt "a cute cat" --width 1024 --height 1024 --steps 25
"""
import argparse
import json
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path

COMFYUI_URL = "http://127.0.0.1:8188"


def post(path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(COMFYUI_URL + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def get(path):
    with urllib.request.urlopen(COMFYUI_URL + path, timeout=30) as r:
        return json.loads(r.read())


def run(workflow: dict, prompt_text: str, width, height, steps, timeout=300):
    # 简单参数注入
    for n in workflow.values():
        ct = n.get("class_type")
        if ct == "CLIPTextEncode" and n["inputs"].get("text"):
            n["inputs"]["text"] = prompt_text
        if ct == "EmptyLatentImage":
            if width: n["inputs"]["width"] = width
            if height: n["inputs"]["height"] = height
        if ct == "KSampler" and steps:
            n["inputs"]["steps"] = steps

    pid = post("/prompt", {"prompt": workflow, "client_id": str(uuid.uuid4())})["prompt_id"]
    print("prompt_id:", pid)
    start = time.time()
    while time.time() - start < timeout:
        try:
            hist = get(f"/history/{pid}")
        except urllib.error.HTTPError:
            hist = {}
        if pid in hist:
            for out in hist[pid]["outputs"].values():
                for img in out.get("images", []):
                    url = f"{COMFYUI_URL}/view?filename={img['filename']}&subfolder={img.get('subfolder','')}&type={img.get('type','output')}"
                    print("IMAGE:", url)
            return
        time.sleep(1.5)
    print("timeout")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", default=None, help="API 格式工作流 JSON 路径")
    ap.add_argument("--prompt", default="a masterpiece")
    ap.add_argument("--width", type=int, default=0)
    ap.add_argument("--height", type=int, default=0)
    ap.add_argument("--steps", type=int, default=0)
    args = ap.parse_args()

    wf = {}
    if args.workflow:
        wf = json.loads(Path(args.workflow).read_text(encoding="utf-8"))
    run(wf, args.prompt, args.width, args.height, args.steps)


if __name__ == "__main__":
    main()
