"""本轮"修改过的文件"汇总（原 server.py file_changes_from_runs 族）。"""

from __future__ import annotations

from typing import Any

from naiba.core.attachments import _is_media_product_path

# 会实际改动磁盘文件的工具：只有这些成功调用会被记录到 assistant 消息的
# metadata.files（消息末尾"修改文件"总结 + 右侧文件面板的数据来源）。
FILE_MODIFY_TOOLS = frozenset({"write_file", "edit_file"})


def file_changes_from_runs(runs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """从一轮工具调用里汇总"本轮修改过的文件"。

    规则：
    - 只统计 FILE_MODIFY_TOOLS 且 success 的调用；
    - 参数里取 path（write_file/edit_file 的既有字段），相对路径保留原样，
      由前端/读取接口按该会话工作区再解析；
    - 同一路径多次写入只保留一条，操作类型以后一次为准（edit 覆盖 write）。

    返回 [{path, name, op}]，op ∈ {write, edit}；无改动返回 []。
    """
    result: list[dict[str, str]] = []
    index_by_key: dict[str, int] = {}
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        tool = str(run.get("tool") or "")
        if tool not in FILE_MODIFY_TOOLS or not run.get("success"):
            continue
        arguments = run.get("arguments")
        if not isinstance(arguments, dict):
            continue
        raw = str(arguments.get("path") or "").strip()
        if not raw or raw.startswith(("http://", "https://")):
            continue
        # 图片/视频/音频这类多媒体产物由消息附件预览负责（原逻辑），
        # 不当作"修改过的文件"收进总结与右侧面板。
        if _is_media_product_path(raw):
            continue
        op = "edit" if tool == "edit_file" else "write"
        # Windows 路径大小写不敏感：统一小写斜杠作去重键，展示仍用原始路径。
        key = raw.replace("\\", "/").rstrip("/").lower()
        if key in index_by_key:
            result[index_by_key[key]]["op"] = op
            continue
        name = raw.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or raw
        index_by_key[key] = len(result)
        result.append({"path": raw, "name": name, "op": op})
    return result
