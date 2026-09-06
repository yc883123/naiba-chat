"""search/recall 域工具 Provider：web_search / recall_history 单一定义。

处理器自 app.py 原样抽取（self→显式依赖参数）：web_search 运行时与 ChatStorage 经构造注入。
"""
from __future__ import annotations

import dataclasses
import time
from typing import Any

from naiba.tools.registry import ToolSpec, build_recall_tool_specs, build_search_tool_specs


def _web_search_handler(
    web_search: Any, args: dict[str, Any], _skills: Any, _ctx: Any,
) -> tuple[bool, str]:
    query = str(args.get("query") or args.get("q") or "")
    max_results = args.get("max_results")
    return web_search.search(query, int(max_results) if isinstance(max_results, (int, float)) else None)


def _recall_history_handler(
    storage: Any, args: dict[str, Any], _skills: Any, _ctx: Any,
) -> tuple[bool, str]:
    """历史会话检索：只读本机会话库，按关键词匹配会话标题与消息文本。"""
    query = str(args.get("query") or "").strip()
    raw_max = args.get("max_results")
    max_results = min(max(int(raw_max) if isinstance(raw_max, (int, float)) else 5, 1), 20)
    if not query:
        return False, "query 不能为空"
    try:
        with storage._connect() as db:
            rows = db.execute(
                "SELECT c.id AS cid, c.title AS title, "
                "       c.updated_at AS conv_updated, "
                "       m.role AS role, m.content AS content, "
                "       m.created_at AS msg_created "
                "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                "WHERE instr(lower(m.content), lower(?)) > 0 "
                "ORDER BY m.created_at DESC LIMIT 400",
                (query,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        return False, f"检索失败：{type(exc).__name__}: {exc}"
    if not rows:
        return True, f"未在历史会话中找到与「{query}」相关的内容"
    # 按会话聚合：每个会话取时间最新的前 3 条命中消息
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        cid = str(row["cid"])
        bucket = grouped.setdefault(
            cid,
            {"title": str(row["title"] or "（无标题）"),
             "updated": int(row["conv_updated"] or 0),
             "hits": []},
        )
        if len(bucket["hits"]) < 3:
            bucket["hits"].append(
                {"role": str(row["role"] or ""), "content": str(row["content"] or ""),
                 "created": int(row["msg_created"] or 0)}
            )
    ordered = sorted(grouped.values(), key=lambda item: item["updated"], reverse=True)[:max_results]
    now_ms = int(time.time() * 1000)
    output = [f"在历史会话中找到 {len(ordered)} 个相关会话（关键词「{query}」，仅本机检索）："]
    for index, bucket in enumerate(ordered, 1):
        age_days = max(0, (now_ms - bucket["updated"]) / 86400000)
        when = f"{age_days:.1f} 天前" if age_days >= 1 else "今天"
        output.append(f"{index}. 《{bucket['title']}》（{when} 更新）")
        for hit in bucket["hits"]:
            role_label = "用户" if hit["role"] == "user" else "助手"
            snippet = " ".join(hit["content"].split())[:200]
            output.append(f"   - [{role_label}] {snippet}{'…' if len(hit['content']) > 200 else ''}")
        output.append("")
    output.append("如需确认是哪一次对话，请把上面的标题与时间给用户核对；内容仅作回忆上下文，引用前应回到对应会话复核。")
    return True, "\n".join(output).strip()


class SearchRecallProvider:
    """search/recall 域：web_search + recall_history 单一定义。"""

    def __init__(self, web_search: Any, storage: Any) -> None:
        self._handlers = {
            "web_search": lambda args, skills, ctx: _web_search_handler(web_search, args, skills, ctx),
            "recall_history": lambda args, skills, ctx: _recall_history_handler(storage, args, skills, ctx),
        }

    def tools(self) -> list[ToolSpec]:
        rows: list[ToolSpec] = []
        for spec in (*build_search_tool_specs(), *build_recall_tool_specs()):
            rows.append(dataclasses.replace(spec, execute=self._handlers[spec.name], system=True))
        return rows
