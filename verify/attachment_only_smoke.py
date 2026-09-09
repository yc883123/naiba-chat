# -*- coding: utf-8 -*-
"""冒烟：纯附件轮次发送链路（源码 server，端口 8799）。

覆盖：五点半烟 + 上传一个真实图片 + 空文字/仅附件提交被接受 + 空文字无附件被拒。
运行前先启动 `python server.py --port 8799`，运行后立刻 kill。
"""
from __future__ import annotations

import json
import sys
import urllib.request
import uuid
from pathlib import Path

BASE = "http://127.0.0.1:8799"
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def get(path: str) -> tuple[int, str]:
    with urllib.request.urlopen(BASE + path, timeout=30) as response:
        return response.status, response.read().decode("utf-8", "replace")


def post_json(path: str, payload: dict) -> tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        BASE + path, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # 4xx 是预期结果之一
        return exc.code, exc.read().decode("utf-8", "replace")


def post_multipart(path: str, filename: str, content: bytes) -> tuple[int, str]:
    boundary = "----naibasmoke" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    request = urllib.request.Request(
        BASE + path,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {label}{('  -> ' + detail) if detail and not ok else ''}")
        if not ok:
            failures.append(label)

    for path in ("/", "/api/health", "/api/bootstrap", "/api/tool_catalog"):
        status, _ = get(path)
        check(f"GET {path} 200", status == 200, str(status))
    status, _ = post_json("/api/skills/scan", {})
    check("POST /api/skills/scan 200", status == 200, str(status))

    # 真实上传一个 1x1 PNG（走 multipart 流式端点）
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000154a24f5b0000000049454e44ae426082"
    )
    status, text = post_multipart("/api/uploads", "冒烟图片.png", png)
    check("POST /api/uploads 200", status == 200, f"{status} {text[:200]}")
    upload = json.loads(text) if status == 200 else {}
    upload_path = str(upload.get("path") or "")
    check("上传返回 path", bool(upload_path), text[:200])

    status, text = post_json("/api/conversations", {"title": "附件冒烟"})
    check("POST /api/conversations 200/201", status in (200, 201), f"{status} {text[:200]}")
    conversation = json.loads(text) if status in (200, 201) else {}
    conversation_id = str(conversation.get("id") or "")
    check("会话 id 返回", bool(conversation_id), text[:200])
    if not conversation_id:
        print()
        print("冒烟中断：无法创建会话")
        return 1

    # 纯附件轮次：空文字 + 附件 → 必须被接受（返回 NDJSON 流而非 400）
    status, text = post_json(
        "/api/chat",
        {
            "conversation_id": conversation_id,
            "message": "",
            "display_message": "",
            "attachments": [{"name": "冒烟图片.png", "path": upload_path, "size": len(png)}],
        },
    )
    check("POST /api/chat 纯附件被接受（非 400）", status == 200, f"{status} {text[:300]}")
    first_event = text.splitlines()[0] if text.strip() else ""
    check("流首事件为 run_started/status", ("run_started" in first_event or "status" in first_event), first_event[:200])

    # 会话里应出现一条空文字的用户消息，且附件已落库
    status, text = get(f"/api/conversations/{conversation_id}")
    check("GET 会话 200", status == 200, str(status))
    detail = json.loads(text) if status == 200 else {}
    user_messages = [m for m in (detail.get("messages") or []) if m.get("role") == "user"]
    check("用户消息 1 条", len(user_messages) == 1, str(len(user_messages)))
    if user_messages:
        check("用户消息文字为空", user_messages[0].get("content") == "", repr(user_messages[0].get("content"))[:120])
        attachments = (user_messages[0].get("metadata") or {}).get("attachments") or []
        check("附件已落库", bool(attachments), str(attachments)[:200])
    check("会话标题回退为附件名", detail.get("title") == "冒烟图片.png", str(detail.get("title")))

    # 空文字 + 无附件 → 必须拒绝
    status, text = post_json("/api/chat", {"conversation_id": conversation_id, "message": "", "attachments": []})
    check("POST /api/chat 空文字无附件 400", status == 400, f"{status} {text[:200]}")

    print()
    print("冒烟结果：", "全部通过" if not failures else f"{len(failures)} 项失败 -> {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
