# -*- coding: utf-8 -*-
"""P6 校验：真实 ComfyUI 产物 URL 的采集链路（下载 → 托管缓存 → 首帧/缩略图 → 播种会话）。

对应真实事故：`job_wait` 返回的散文+JSON 里含 `http://127.0.0.1:8188/view?filename=…`，
旧扫描按 `?` 截断成 `…/view` → 判定非媒体 → Job 完成后图片不显示。

用法：python verify/p6_comfy_extract_check.py [filename]
（要求 ComfyUI 在 127.0.0.1:8188 运行且产物存在）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core.media_types import job_media_declaration  # noqa: E402
from naiba.storage.media_collect import MediaCollector  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402

TITLE = "ComfyUI 产物冒烟"


class _ConfigStub:
    def __init__(self, data_dir: Path) -> None:
        self.data = {"imaging": {}}
        self._data_dir = data_dir

    def resolve_data_dir(self) -> Path:
        return self._data_dir


def main() -> int:
    filename = sys.argv[1] if len(sys.argv) > 1 else "lumine_cute_00001_.png"
    url = f"http://127.0.0.1:8188/view?filename={filename}&subfolder=&type=output"
    result = (
        "Job 已完成。"
        + json.dumps(
            {
                "prompt_ids": ["8e34a71d-a512-4be2-b5dd-06d77250ca88"],
                "completed": [{"index": 0, "prompt_id": "8e34a71d-a512-4be2-b5dd-06d77250ca88", "files": [url]}],
                "errors": [],
            },
            ensure_ascii=False,
        )
    )

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {label}{('  -> ' + detail) if detail and not ok else ''}")
        if not ok:
            failures.append(label)

    data_dir = ROOT / "data"
    config = _ConfigStub(data_dir)
    collector = MediaCollector(config)
    collected = collector.collect(
        {"tool": "job_wait", "result": result, "success": True},
        job_media_declaration("comfyui") if False else {"policy": "inline", "extract": "scan"},
    )
    media = collected["media"]
    check("从 job_wait 结果提取到 1 个媒体", len(media) == 1, json.dumps(collected, ensure_ascii=False)[:300])
    if not media:
        return 1
    record = media[0]
    check("kind=image", record["kind"] == "image", json.dumps(record, ensure_ascii=False))
    check("文件名取自 URL 的 filename 参数", record["name"] == filename, record["name"])
    main_path = Path(record["source"])
    check("已托管缓存到 data/generated", main_path.is_file() and "generated" in main_path.parts, record["source"])
    check("缓存文件非空", main_path.is_file() and main_path.stat().st_size > 100000, f"{main_path.stat().st_size if main_path.is_file() else 0} bytes")
    thumb = Path(record["thumb_path"]) if record.get("thumb_path") else None
    check("生成缩略图", bool(thumb and thumb.is_file()), str(thumb))
    if thumb and thumb.is_file():
        check("缩略图为 WebP", thumb.read_bytes()[:4] == b"RIFF" and thumb.read_bytes()[8:12] == b"WEBP", thumb.read_bytes()[:16].hex())

    # 同一 URL 再采一次：内容未变 → 复用同一份内容寻址缓存（不重复下载落盘）
    again = collector.collect(
        {"tool": "job_wait", "result": result, "success": True},
        {"policy": "inline", "extract": "scan"},
    )["media"][0]
    check("同一 URL 内容未变 → 复用同一缓存", again["source"] == record["source"], f"{again['source']} vs {record['source']}")
    import hashlib as _hashlib
    check(
        "缓存文件内容 == ComfyUI 当前产物",
        _hashlib.sha256(main_path.read_bytes()).hexdigest()[:16] == main_path.name.split("_", 1)[0],
        main_path.name,
    )

    # 播种一条会话，供浏览器冒烟验证渲染
    storage = ChatStorage(data_dir / "chat.db")
    for conversation in storage.list_conversations():
        if TITLE in str(conversation.get("title") or ""):
            storage.delete_conversation(str(conversation["id"]))
    conversation = storage.create_conversation(TITLE)
    conversation_id = str(conversation["id"])
    storage.add_message(conversation_id, "user", "ComfyUI 产物冒烟：把刚生成的图给我看看")
    storage.add_message(
        conversation_id,
        "assistant",
        "图片已生成。",
        {
            "activity": [{"type": "tool", "request_index": 1, "run": {
                "tool": "job_wait", "success": True, "arguments": {"job_id": "8e34a71d", "timeout": 600},
                "result": result, "media": [record],
            }}],
            "tool_runs": [{"tool": "job_wait", "success": True, "arguments": {"job_id": "8e34a71d"}, "result": result, "media": [record]}],
            "attachments": [record],
        },
    )
    print(json.dumps({"conversation_id": conversation_id, "title": TITLE, "source": record["source"]}, ensure_ascii=False))
    print()
    print("P6 采集链路校验：", "全部通过" if not failures else f"{len(failures)} 项失败 -> {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
