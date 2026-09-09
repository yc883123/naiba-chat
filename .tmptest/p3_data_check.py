# -*- coding: utf-8 -*-
"""P3 数据侧校验：播种的媒体消息形态与 /api/file 可服务性（无需浏览器）。

运行前：python .tmptest/seed_media_message.py && python server.py --port 8799
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8799"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def get_json(path: str):
    with urllib.request.urlopen(BASE + path, timeout=30) as response:
        return response.status, json.loads(response.read().decode("utf-8", "replace"))


def get_status(path: str) -> int:
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {label}{('  -> ' + detail) if detail and not ok else ''}")
        if not ok:
            failures.append(label)

    status, listing = get_json("/api/conversations")
    conversation = next(
        (item for item in (listing.get("conversations") or []) if "媒体内嵌" in str(item.get("title") or "")),
        None,
    )
    check("播种会话存在", conversation is not None, json.dumps(listing)[:160])
    if not conversation:
        return 1
    conversation_id = str(conversation["id"])
    status, detail = get_json(f"/api/conversations/{conversation_id}")
    messages = detail.get("messages") or []
    check("会话 4 条消息", len(messages) == 4, str(len(messages)))
    assistants = [m for m in messages if m.get("role") == "assistant"]
    first_meta = (assistants[0].get("metadata") or {}) if assistants else {}
    activity = first_meta.get("activity") or []
    media = [item for item in activity if item.get("type") == "tool"]
    check("activity 含 2 个工具条目", len(media) == 2, str(len(media)))
    check(
        "工具条目带 run.media",
        all(isinstance((item.get("run") or {}).get("media"), list) and (item["run"]["media"]) for item in media),
        json.dumps(media, ensure_ascii=False)[:200],
    )
    truncated = (media[0].get("run") or {}).get("media_truncated") if media else None
    check("首个工具带截断自述", bool(truncated) and truncated.get("total") == 25, json.dumps(truncated))
    attachments = first_meta.get("attachments") or []
    check("派生附件汇总 3 条", len(attachments) == 3, str(len(attachments)))

    sources = [str((item.get("run") or {})["media"][0]["source"]) for item in media]
    sources += [str((item.get("run") or {})["media"][1]["source"]) for item in media if len((item.get("run") or {}).get("media") or []) > 1]
    for source in sources:
        query = urllib.parse.urlencode({"path": source})
        check(f"/api/file 可服务 {source.split(chr(92))[-1]}", get_status(f"/api/file?{query}") == 200)

    thumbs = [str((item.get("run") or {})["media"][0]["thumb_path"]) for item in media]
    for thumb in thumbs:
        query = urllib.parse.urlencode({"path": thumb})
        check(f"/api/file 可服务缩略图 {thumb.split(chr(92))[-1]}", get_status(f"/api/file?{query}") == 200)

    print()
    print("P3 数据侧校验：", "全部通过" if not failures else f"{len(failures)} 项失败 -> {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
