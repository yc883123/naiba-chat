# -*- coding: utf-8 -*-
"""回填历史消息里"当时没提取到"的媒体（修复 job_wait / ComfyUI URL 截断事故前的消息）。

背景：`_text_media_sources` 曾把 `?` 当散文终止符，导致
`http://127.0.0.1:8188/view?filename=x.png&...` 被截成 `…/view` → 判定非媒体 →
Job 完成后的图片不显示（已修复）。本脚本把历史消息按**修复后的口径**重新提取一遍。

语义：**只做加法**——给该消息的 `tool_runs[i]` / `activity[].run` 补 `media`，
并按 `union_run_media` 重算消息级 `attachments`；不删不改任何已有字段。
安全性：只挂"落地可服务"的记录（本地文件存在，或 ComfyUI URL 已成功下载缓存），
避免给老会话塞进破图；默认 **dry-run 只读**，加 `--apply` 才写库。

用法：
  python verify/backfill_media.py                        # 只读报告（源码库）
  python verify/backfill_media.py --db "<chat.db>"       # 只读报告（指定库）
  python verify/backfill_media.py --db "<chat.db>" --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from naiba.core.attachments import union_run_media  # noqa: E402
from naiba.core.media_types import MEDIA_EXTRACTORS, media_kind_of  # noqa: E402
from naiba.core.messages import MetadataKeys  # noqa: E402
from naiba.storage.media_collect import MediaCollector, _candidates_from_result  # noqa: E402
from naiba.tools.registry import build_tool_registry  # noqa: E402


class _ConfigStub:
    def __init__(self, data_dir: Path) -> None:
        self.data = {"imaging": {}}
        self._data_dir = data_dir

    def resolve_data_dir(self) -> Path:
        return self._data_dir


def _declaration_for(registry, tool: str) -> dict[str, str]:
    try:
        return registry.media_declaration(tool)
    except Exception:  # noqa: BLE001 - 老消息里的退役名等
        return {"policy": "never", "extract": "none"}


def _iter_targets(metadata: dict):
    """产出 (容器, run) —— 只挑"没有 media 且声明允许提取"的工具调用。"""
    for run in metadata.get("tool_runs") or []:
        if isinstance(run, dict):
            yield run
    for item in metadata.get("activity") or []:
        run = item.get("run") if isinstance(item, dict) else None
        if isinstance(run, dict):
            yield run


def _would_extract(run: dict, declaration: dict[str, str]) -> list[dict]:
    if run.get("media") or not run.get("success"):
        return []
    if declaration["extract"] not in MEDIA_EXTRACTORS or declaration["extract"] == "none":
        return []
    result = str(run.get("result") or "")
    if not result or result.startswith("NEED_CONFIRM:"):
        return []
    return [c for c in _candidates_from_result(result, declaration["extract"], tool=str(run.get("tool") or ""))
            if media_kind_of(c.get("source"))]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "data" / "chat.db"))
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只读报告）")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    db_path = Path(args.db)
    if not db_path.is_file():
        print(json.dumps({"error": f"数据库不存在：{db_path}"}, ensure_ascii=False))
        return 1

    registry = build_tool_registry()
    rows: list[tuple[str, str, str, dict]] = []
    # 只读 URI：Windows 路径必须写成 file:///C:/... 形式（裸 file:C:\... 会 open 失败）
    read_only_uri = "file:///" + db_path.resolve().as_posix().lstrip("/") + "?mode=ro"
    with sqlite3.connect(read_only_uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        for row in db.execute(
            "SELECT id, conversation_id, role, metadata FROM messages WHERE role='assistant' ORDER BY created_at DESC"
        ):
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except json.JSONDecodeError:
                continue
            if not isinstance(metadata, dict):
                continue
            hits = []
            for run in _iter_targets(metadata):
                declaration = _declaration_for(registry, str(run.get("tool") or ""))
                hits.extend(_would_extract(run, declaration))
            if hits:
                rows.append((str(row["id"]), str(row["conversation_id"]), "", {"hits": hits}))
            if args.limit and len(rows) >= args.limit:
                break

    report = [
        {
            "message_id": message_id,
            "conversation_id": conversation_id,
            "candidates": [c["source"] for c in payload["hits"]],
        }
        for message_id, conversation_id, _tool, payload in rows
    ]
    print(json.dumps({"db": str(db_path), "apply": args.apply, "messages": len(report), "detail": report[:20]}, ensure_ascii=False, indent=2))
    if not args.apply or not rows:
        print()
        print("dry-run：未写库。确认无误后加 --apply 执行。")
        return 0

    # ---- 写库（--apply）----
    from naiba.storage.store import ChatStorage

    storage = ChatStorage(db_path)
    collector = MediaCollector(_ConfigStub(db_path.parent))
    changed = 0
    added_total = 0
    for message_id, conversation_id, _tool, _payload in rows:
        conversation = storage.get_conversation(conversation_id)
        if not conversation:
            continue
        message = next((m for m in conversation.get("messages") or [] if str(m.get("id")) == message_id), None)
        if message is None:
            continue
        metadata = dict(message.get("metadata") or {})
        runs = [run for run in (metadata.get("tool_runs") or []) if isinstance(run, dict)]
        activity_runs = [
            item["run"] for item in (metadata.get("activity") or [])
            if isinstance(item, dict) and isinstance(item.get("run"), dict)
        ]
        added = 0
        for run in runs + activity_runs:
            declaration = _declaration_for(registry, str(run.get("tool") or ""))
            if not _would_extract(run, declaration):
                continue
            collected = collector.collect(
                {"tool": str(run.get("tool") or ""), "result": str(run.get("result") or ""), "success": True},
                declaration,
            )
            existing = {str(item.get("source") or "") for item in (run.get("media") or []) if isinstance(item, dict)}
            fresh = [
                item for item in collected.get("media") or []
                if str(item.get("source") or "") not in existing and Path(str(item.get("source") or "")).is_file()
            ]
            if not fresh:
                continue
            run["media"] = list(run.get("media") or []) + fresh
            added += len(fresh)
        if not added:
            continue
        union, union_truncated = union_run_media(runs)
        metadata[MetadataKeys.ATTACHMENTS] = union
        if union_truncated:
            metadata[MetadataKeys.ATTACHMENTS_TRUNCATED] = union_truncated
        if storage.update_message_metadata(conversation_id, message_id, metadata):
            changed += 1
            added_total += added
    print()
    print(json.dumps({"applied_messages": changed, "added_media": added_total}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
