"""AI 回复中的交互选项识别（原 server.py _detect_choice_groups/_detect_choices）。

纯文本解析，无状态无依赖。规则：只认明确的"选择"意图（中/英文完整词组）、
选项组须紧邻意图行、选项文本有长度上限、编号须连续、代码块/Skill 文档不识别。
"""

from __future__ import annotations

import re
from typing import Any


def _detect_choice_groups(text: str) -> list[dict[str, Any]]:
    """检测回复中的交互选项组，并保留每组前面的提示语。"""
    # 代码示例中的列表不是交互选项，先排除 fenced code block。
    # Long Skill manuals and MCP instructions are not interactive choices.
    raw_text = str(text or "")
    if len(raw_text) > 5000 or re.search(r"<skill\b|mcp_servers\s*:|official-comfy-mcp", raw_text, re.I):
        return []
    visible_text = re.sub(r"```[\s\S]*?```", "", raw_text)
    lines = visible_text.splitlines()

    MAX_CHOICE_LEN = 40   # 选项文本过长（整段话）不算交互选项
    MAX_CUE_LEN = 40      # 意图行过长（长句里恰好含"选择/choose"）不算意图
    MAX_CUE_DISTANCE = 2  # 意图行与选项组首行的最大行距（允许少量空行间隔）

    def clean(value: str) -> str:
        value = re.sub(r"^(?:\[[ xX]\]\s*)", "", value.strip())
        value = re.sub(r"^(?:\*\*|__)", "", value)
        value = re.sub(r"\s*(?:\*\*|__)$", "", value)
        # 剥离行首编号残留（如 bullet 行 "- 1. 安装依赖" → "安装依赖"）。
        value = re.sub(r"^\d{1,2}\s*[.、:：)）\]】]\s*", "", value.strip())
        return value.strip()

    circled_numbers = {char: index for index, char in enumerate("①②③④⑤⑥⑦⑧", start=1)}
    # 只接受明确的"选择"意图。英文只保留完整词组，避免正文/代码里的
    # 裸 select / choose / pick 触发误判。
    choice_cue = re.compile(
        r"请(?:先|再)?选择|请(?:你|您)?选|再选(?:一下|一个|个)?|供(?:你|您)?选择|可供选择|"
        r"选哪个|选一个|pick one|choose one|select one|which one|which of|choose from|select from",
        re.IGNORECASE,
    )

    numbered_pattern = re.compile(
        r"^\s*(?:\*\*|__)?(?:[（(\[【]?\s*(\d{1,2})\s*[.、):：）\]】])"
        r"\s*(?:\*\*|__)?\s*(.+?)\s*$"
    )
    lettered_pattern = re.compile(
        r"^\s*(?:\*\*|__)?(?:[（(\[【]?\s*([A-Ha-h])\s*[.、):：）\]】])"
        r"\s*(?:\*\*|__)?\s*(.+?)\s*$"
    )
    named_pattern = re.compile(
        r"^\s*(?:\*\*|__)?(?:选项|方案)\s*[一二三四五六七八\dA-Ha-h]+\s*[.、:：)）]"
        r"\s*(?:\*\*|__)?\s*(.+?)\s*$",
        re.IGNORECASE,
    )
    bullet_pattern = re.compile(r"^\s*[-+*•]\s+(?:\[[ xX]\]\s*)?(.+?)\s*$")

    groups: list[tuple[str, list[tuple[Any, str]], str, int, int]] = []
    current_kind = ""
    current_items: list[tuple[Any, str]] = []
    current_prompt = ""
    current_start_line = -1
    current_cue_line = -1  # 组首行时最近一个意图行的快照（避免被后续 cue 覆盖）
    preceding_prompt = ""
    recent_cue_prompt = ""
    cue_line = -1  # 最近一个意图行所在行号

    def finish_group() -> None:
        nonlocal current_kind, current_items, current_prompt, current_start_line, current_cue_line
        if current_items:
            groups.append(
                (current_kind, current_items, current_prompt, current_start_line, current_cue_line)
            )
        current_kind = ""
        current_items = []
        current_prompt = ""
        current_start_line = -1
        current_cue_line = -1

    for line_index, raw_line in enumerate(lines):
        # Models frequently put compact choices on one line, for example
        # "1. 文生视频 2. 图生视频" or "请选择语言：1. 中文 2. 英文".
        # Split at a choice marker preceded by whitespace or a CJK/ASCII
        # punctuation so such compact prompts yield separate lines, while
        # decimal numbers in prose are left untouched.
        expanded_lines = re.sub(
            r"(?<=[\s：:、。；;，,])(?=\d{1,2}\s*[.、):：）\]】])",
            "\n",
            raw_line,
        ).splitlines() or [""]
        for line in expanded_lines:
            parsed: tuple[str, Any, str] | None = None
            match = numbered_pattern.match(line)
            if match:
                parsed = ("numbered", int(match.group(1)), clean(match.group(2)))
            if not parsed:
                match = lettered_pattern.match(line)
                if match:
                    parsed = ("lettered", match.group(1).upper(), clean(match.group(2)))
            if not parsed:
                match = named_pattern.match(line)
                if match:
                    parsed = ("named", len(current_items), clean(match.group(1)))
            stripped = line.strip()
            if not parsed and stripped and stripped[0] in circled_numbers:
                value = clean(stripped[1:].lstrip(".、):：） "))
                if value:
                    parsed = ("numbered", circled_numbers[stripped[0]], value)
            if not parsed:
                match = bullet_pattern.match(line)
                if match:
                    parsed = ("bullet", len(current_items), clean(match.group(1)))
            # 选项文本非空且不能是整段话
            if parsed and (not parsed[2] or len(parsed[2]) > MAX_CHOICE_LEN):
                parsed = None
            # 断行展开出的裸 bullet 符号（"- " / "• "）不是内容行，
            # 跳过以免打断正在收集的选项组。
            if not parsed and re.fullmatch(r"[-+*•]\s*", stripped):
                continue

            # A numbered heading such as "**1. 请选择时长：**" introduces the
            # following choices; it is not itself an option. Treat it as the
            # prompt so the option markers remain consecutive.
            if parsed and parsed[0] in {"numbered", "lettered"} and choice_cue.search(parsed[2]):
                finish_group()
                preceding_prompt = clean(parsed[2])
                if len(preceding_prompt) <= MAX_CUE_LEN:
                    recent_cue_prompt = preceding_prompt
                    cue_line = line_index
                continue

            if parsed:
                kind, marker, value = parsed
                if current_items and kind != current_kind:
                    finish_group()
                if not current_items:
                    current_kind = kind
                    current_start_line = line_index
                    current_cue_line = cue_line
                    current_prompt = (
                        preceding_prompt
                        if choice_cue.search(preceding_prompt)
                        and len(preceding_prompt) <= MAX_CUE_LEN
                        else recent_cue_prompt
                    )
                current_items.append((marker, value))
                continue

            finish_group()
            if stripped:
                preceding_prompt = clean(re.sub(r"^(?:#{1,6}\s*)", "", stripped))
                if choice_cue.search(preceding_prompt) and len(preceding_prompt) <= MAX_CUE_LEN:
                    recent_cue_prompt = preceding_prompt
                    cue_line = line_index

    finish_group()

    candidates: list[dict[str, Any]] = []
    for kind, items, prompt, start_line, cue_at_start in groups:
        if len(items) < 2:
            continue
        markers = [marker for marker, _ in items]
        if kind == "numbered" and markers != list(range(markers[0], markers[0] + len(items))):
            continue
        if kind == "lettered":
            expected = [chr(ord(markers[0]) + offset) for offset in range(len(items))]
            if markers != expected:
                continue
        # 意图行必须紧跟选项组（允许少量空行/单行间隔），否则视为普通列表。
        has_cue = (
            bool(choice_cue.search(prompt))
            and 0 <= start_line - cue_at_start <= MAX_CUE_DISTANCE
        )
        if not has_cue:
            continue
        candidates.append(
            {
                "prompt": prompt,
                "choices": [value for _, value in items][:8],
            }
        )

    # 一旦判定是"请选择"场景，就保留全部组（含 cue 未命中的后续问题），
    # 避免漏掉第二个问题导致"点第一题就发"。
    return candidates


def _detect_choices(text: str) -> list[str]:
    """兼容旧调用方：返回检测到的第一组选项。"""
    groups = _detect_choice_groups(text)
    return groups[0]["choices"] if groups else []
