# -*- coding: utf-8 -*-
"""Golden 录制-回放基线（收官线 ②；交接报告 §6.3 第 1 步）。

用途：把三类"行为敏感序列"固化为输入→输出快照（tests/golden/*.json），
供「包内细分」等后续结构性改动逐字节比对——回放器断言本次运行输出与基线完全一致。

运行方式：
  录制（一次性；脚本在 .tmptest 下，不入库）：
    .venv\\Scripts\\python.exe .tmptest\\record_golden.py
  回放（每次重构验证）：
    .venv\\Scripts\\python.exe -m unittest tests.golden_replay
    或：.venv\\Scripts\\python.exe tests\\golden_replay.py

基线清单：
  cancel_race.json          取消防线：emit 状态映射 / cancelling 冻结 / aborted 重建与幂等 / 看门狗兜底
  tool_confirm.json         工具确认：NEED_CONFIRM→确认/拒绝/过期/deny/二次消费 全序列
  history_images.json       多轮图片历史：2 轮多图 + trace/reasoning/tool_runs → 模型消息快照

规范化约定（录制与回放共用，保证跨运行稳定）：
  - uuid 一律替换为 <CONFIRM_ID>；
  - 临时目录根替换为 <TMP>；
  - 图片 data（base64）替换为 sha256 摘要（固定环境下 JPEG 重编码逐字节确定；摘要既防漂移又缩减体积）。
"""

import hashlib
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.core.history import build_model_history  # noqa: E402
from naiba.mcp import MCPRegistry  # noqa: E402
from naiba.run.manager import ConversationRunManager  # noqa: E402
from naiba.tools.executor import ToolExecutor  # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
GOLDEN_NAMES = ("cancel_race", "tool_confirm", "history_images")

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def normalize(value, tmp_root: str) -> object:
    """递归规范化：uuid→<CONFIRM_ID>；tmp_root→<TMP>；图片 data→sha256。

    临时根可能出现 1–4 层反斜杠转义形态（json.dumps 嵌套时逐层翻倍），
    如 NEED_CONFIRM 参数段或 untrusted_tool_result 内的结果 JSON；
    按转义层级从高到低逐一替换，保证换临时根回放不会误报漂移。
    """
    tmp_variants = [str(tmp_root)]
    for times in (2, 3, 4):
        tmp_variants.append(str(tmp_root).replace("\\", "\\" * times))
    if isinstance(value, str):
        text = value
        for variant in tmp_variants:
            text = text.replace(variant, "<TMP>")
        return UUID_RE.sub("<CONFIRM_ID>", text)
    if isinstance(value, dict):
        if value.get("type") == "image" and isinstance(value.get("data"), str):
            return {**value, "data": sha(value["data"])}
        return {key: normalize(item, tmp_root) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize(item, tmp_root) for item in value]
    return value


# ---------------------------------------------------------------------------
# 场景一：取消防线（cancel_race）
# ---------------------------------------------------------------------------

CANCEL_INPUTS = {
    "run_id": "RUN-ID-X",
    "conversation_id": "CONV-ID-X",
    "skills": [{"id": "s1", "name": "演示技能"}],
    # 运行中 emit 的状态映射序列
    "emit_sequence": [
        {"type": "status", "message": "开始执行"},
        {"type": "tool_start", "tool": "pwsh"},
        {"type": "tool_confirm", "tool_name": "write_file", "tool_desc": "写文件",
         "arguments": {"path": "<TMP>/w.txt", "content": "x"}, "confirm_id": "c1"},
        {"type": "tool_result", "tool": "pwsh"},
        {"type": "skills", "skills": [{"id": "s1", "name": "演示技能"}]},
    ],
    # cancelling 状态下再 emit：状态冻结（status 不落库）
    "freeze_emit": {"type": "status", "message": "不应改写状态"},
    # _persist_aborted_message 重建输入：事件流（模拟已累积的部分输出）
    "abort_events": [
        {"type": "reasoning_start"},
        {"type": "reasoning_delta", "content": "先分析"},
        {"type": "reasoning_delta", "content": "再执行"},
        {"type": "reasoning_end"},
        {"type": "tool_result", "tool": "read_file", "success": True, "result": "文件内容摘要"},
        {"type": "tool_result", "tool": "pwsh", "success": True, "result": "命令输出"},
        {"type": "delta", "content": "写到一半的回复"},
    ],
    # 幂等场景：该 run 已有 aborted 消息
    "existing_aborted": {"aborted": True, "run_id": "RUN-ID-X", "content": "（已中止）"},
}


class _CancelStorage:
    """最小语义桩：记录 append/update 调用，按 sequence 提供分页读取。"""

    def __init__(self):
        self.events: list[dict] = []
        self._seq = 0
        self.task = {"status": "running", "conversation_id": "CONV-ID-X", "detail": {}}
        self.conversation = {"id": "CONV-ID-X", "messages": []}
        self.updates: list[tuple[str, dict]] = []
        self.added: list[dict] = []

    def append_run_event(self, run_id, payload):
        self._seq += 1
        record = {**payload, "run_id": run_id, "sequence": self._seq}
        self.events.append(record)
        return {"run_id": run_id, "sequence": self._seq}

    def list_run_events(self, run_id, after=0, limit=500):
        return [e for e in self.events if e.get("sequence", 0) > after][:limit]

    def get_background_task(self, run_id):
        return dict(self.task)

    def update_background_task(self, run_id, **kw):
        self.updates.append((run_id, kw))
        self.task.update(kw)

    def get_conversation(self, conversation_id):
        return json.loads(json.dumps(self.conversation))

    def add_message(self, conversation_id, role, content, metadata):
        message = {"id": "MSG-1", "conversation_id": conversation_id,
                   "role": role, "content": content, "metadata": metadata}
        self.added.append(message)
        self.conversation["messages"].append(message)
        return dict(message)


class _ConfigStub:
    def __init__(self, data_dir):
        self._data_dir = data_dir
        self.data = {"imaging": None}

    def resolve_data_dir(self):
        return self._data_dir


class _AppStub:
    def __init__(self, storage, config):
        self.storage = storage
        self.config = config


def _make_cancel_env(tmp_root: Path, storage: _CancelStorage):
    config = _ConfigStub(str(tmp_root / "data"))
    return ConversationRunManager(_AppStub(storage, config))


def scenario_cancel_race(tmp_root: Path) -> dict:
    storage = _CancelStorage()
    manager = _make_cancel_env(tmp_root, storage)

    # 1) emit 状态映射（运行中）
    mapping = []
    for payload in CANCEL_INPUTS["emit_sequence"]:
        manager.emit(CANCEL_INPUTS["run_id"], dict(payload))
        run_id, kw = storage.updates[-1]
        mapping.append({
            "type": payload.get("type"),
            "status": kw.get("status"),
            "detail_message": (kw.get("detail") or {}).get("message"),
        })

    # 2) cancelling 冻结
    storage.task["status"] = "cancelling"
    updates_before = len(storage.updates)
    manager.emit(CANCEL_INPUTS["run_id"], dict(CANCEL_INPUTS["freeze_emit"]))
    run_id, kw = storage.updates[-1]
    freeze = {"type": CANCEL_INPUTS["freeze_emit"].get("type"),
              "status": kw.get("status"), "updates_count": len(storage.updates) - updates_before}

    # 3) aborted 重建（首次）
    storage.task = {"status": "cancelled", "conversation_id": "CONV-ID-X", "detail": {}}
    storage.events = [
        {**item, "run_id": CANCEL_INPUTS["run_id"], "sequence": idx + 1}
        for idx, item in enumerate(CANCEL_INPUTS["abort_events"])
    ]
    restored = manager._persist_aborted_message(
        CANCEL_INPUTS["run_id"], CANCEL_INPUTS["conversation_id"], CANCEL_INPUTS["skills"]
    )
    rebuild = {
        "add_message_count": len(storage.added),
        "content": restored.get("content") if restored else None,
        "metadata": restored.get("metadata") if restored else None,
    }

    # 4) aborted 幂等（已有 aborted 消息）
    storage.added = []
    storage.conversation = {"id": "CONV-ID-X", "messages": [
        {"id": "M1", "role": "assistant", "content": "（已中止）",
         "metadata": dict(CANCEL_INPUTS["existing_aborted"])}
    ]}
    second = manager._persist_aborted_message(
        CANCEL_INPUTS["run_id"], CANCEL_INPUTS["conversation_id"], CANCEL_INPUTS["skills"]
    )
    idempotent = {"returned": second, "add_message_count": len(storage.added)}

    # 5) 看门狗兜底：状态卡在 cancelling，3 秒兜底强制置 cancelled 并发事件
    storage = _CancelStorage()
    manager = _make_cancel_env(tmp_root, storage)
    storage.task = {"status": "cancelling", "conversation_id": "CONV-ID-X", "detail": {}}
    with mock.patch("naiba.run.manager.time.sleep", return_value=None):
        manager._schedule_forced_cancel(CANCEL_INPUTS["run_id"])
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if any(kw.get("status") == "cancelled" for _rid, kw in storage.updates):
            break
        time.sleep(0.02)
    cancelled_updates = [
        {"status": kw.get("status"), "detail": kw.get("detail")}
        for _rid, kw in storage.updates if kw.get("status") == "cancelled"
    ]
    watchdog = {
        "cancelled_updates": cancelled_updates,
        "cancelled_events": [p for p in storage.events if p.get("type") == "cancelled"],
        "add_message_count": len(storage.added),
    }
    return {
        "emit_mapping": mapping,
        "cancel_freeze": freeze,
        "abort_rebuild": rebuild,
        "abort_idempotent": idempotent,
        "watchdog": watchdog,
    }


# ---------------------------------------------------------------------------
# 场景二：工具确认序列（tool_confirm）
# ---------------------------------------------------------------------------

TOOL_INPUTS = {
    "target": "<TMP>/w1.txt",
    "content": "Hello golden",
    "seed_file": "<TMP>/seed.txt",
    "invalid_id": "00000000-0000-0000-0000-000000000000",
}


def scenario_tool_confirm(tmp_root: Path) -> dict:
    rows = []
    executor = ToolExecutor(tmp_root, sys.executable, 60, MCPRegistry([]),
                            permission_mode="confirm")
    target = Path(TOOL_INPUTS["target"].replace("<TMP>", str(tmp_root)))
    seed = Path(TOOL_INPUTS["seed_file"].replace("<TMP>", str(tmp_root)))
    seed.write_text("seed", encoding="utf-8")

    def record(name, ok, out, file_exists=None, file_content=None, extra=None):
        row = {"step": name, "ok": ok, "out": out}
        if file_exists is not None:
            row["file_exists"] = file_exists
        if file_content is not None:
            row["file_content"] = file_content
        if extra:
            row.update(extra)
        rows.append(row)

    # T1: confirm 模式下高风险写工具 → NEED_CONFIRM，不落盘
    ok, out = executor.execute("write_file", {"path": str(target), "content": TOOL_INPUTS["content"]}, [])
    record("confirm-request", ok, out, file_exists=target.exists(), extra={"pending_count": len(executor.pending_confirmation)})
    confirm_id = out.split(":")[1] if out.startswith("NEED_CONFIRM:") else ""

    # T2: 确认 → 落盘 + 结果；再次消费同 id → 无效
    ok2, out2 = executor.confirm_execute(confirm_id)
    record("confirm-accept", ok2, out2, file_exists=target.exists(),
           file_content=target.read_text(encoding="utf-8") if target.exists() else None,
           extra={"pending_count": len(executor.pending_confirmation)})
    ok3, out3 = executor.confirm_execute(confirm_id)
    record("confirm-second-consumption", ok3, out3)

    # T3: 拒绝分支
    target.unlink(missing_ok=True)
    ok4, out4 = executor.execute("write_file", {"path": str(target), "content": TOOL_INPUTS["content"]}, [])
    reject_id = out4.split(":")[1] if out4.startswith("NEED_CONFIRM:") else ""
    ok5, out5 = executor.reject_execute(reject_id)
    record("confirm-reject", ok5, out5, file_exists=target.exists(),
           extra={"pending_count": len(executor.pending_confirmation)})

    # T4: deny 模式直接拒绝
    deny_executor = ToolExecutor(tmp_root, sys.executable, 60, MCPRegistry([]),
                                 permission_mode="deny")
    ok6, out6 = deny_executor.execute("write_file", {"path": str(target), "content": "x"}, [])
    record("deny-mode", ok6, out6, file_exists=target.exists())

    # T5: 过期/未知 id
    ok7, out7 = executor.confirm_execute(TOOL_INPUTS["invalid_id"])
    record("invalid-id", ok7, out7)

    # T6: 只读工具免确认直接执行
    ok8, out8 = executor.execute("list_directory", {"path": str(tmp_root)}, [])
    record("readonly-auto", ok8, out8)
    return {"rows": rows}


# ---------------------------------------------------------------------------
# 场景三：多轮图片历史（history_images）
# ---------------------------------------------------------------------------

HISTORY_INPUTS = {
    "img_a": "<TMP>/img_a.png",
    "img_b": "<TMP>/img_b.png",
    "note_txt": "<TMP>/config.txt",
    "messages": [
        {
            "role": "user",
            "content": "先看看第一张图",
            "metadata": {"attachments": [{"path": "<TMP>/img_a.png"}, {"path": "<TMP>/config.txt"}]},
        },
        {
            "role": "assistant",
            "content": "好的，看到了",
            "metadata": {
                "reasoning": ["我先确认图片"],
                "trace": [{"role": "assistant", "content": "好的，看到了"}],
            },
        },
        {
            "role": "user",
            "content": "再看第二张",
            "metadata": {"attachments": [{"path": "<TMP>/img_b.png"}]},
        },
        {
            "role": "assistant",
            "content": "第二张也看到了",
            "metadata": {
                "tool_runs": [
                    {"tool": "read_file", "success": True, "result": "笔记内容"},
                    {"tool": "vision_read_folder", "success": True,
                     "result": json.dumps({
                         "note": "两张图",
                         "images": [{"name": "img_b.png", "path": "<TMP>/img_b.png",
                                     "thumb_path": "<TMP>/t.jpg", "width": 64, "height": 64}],
                     }, ensure_ascii=False)},
                ],
            },
        },
    ],
}


def _make_png(color: tuple[int, int, int]) -> bytes:
    from PIL import Image
    import io
    image = Image.new("RGB", (64, 64), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def scenario_history_images(tmp_root: Path) -> dict:
    img_a = tmp_root / "img_a.png"
    img_b = tmp_root / "img_b.png"
    img_a.write_bytes(_make_png((180, 60, 60)))
    img_b.write_bytes(_make_png((60, 120, 180)))
    (tmp_root / "config.txt").write_text("备注文本", encoding="utf-8")

    messages = json.loads(json.dumps(HISTORY_INPUTS["messages"]))
    # 把 <TMP> 占位符替换为真实临时根
    def substitute(item):
        if isinstance(item, str):
            return item.replace("<TMP>", str(tmp_root))
        if isinstance(item, dict):
            return {k: substitute(v) for k, v in item.items()}
        if isinstance(item, list):
            return [substitute(x) for x in item]
        return item
    messages = substitute(messages)
    history = build_model_history(messages)
    # 语义快照：角色、文本、图片摘要、reasoning/tool_calls 结构（data 一律摘要化）
    snapshot = []
    for message in history:
        content = message.get("content")
        if isinstance(content, list):
            summarized = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image":
                    summarized.append({"type": "image", "sha256": sha(part["data"]),
                                       "media_type": part.get("media_type"), "name": part.get("name")})
                else:
                    summarized.append(part)
            entry = {"role": message["role"], "content": summarized}
        else:
            entry = {"role": message["role"], "content": content}
        for key in ("reasoning_content", "tool_calls", "tool_call_id", "name"):
            if message.get(key):
                entry[key] = message[key]
        snapshot.append(entry)
    return {"history": snapshot}


# ---------------------------------------------------------------------------
# 统一入口：build(name) → {"inputs": ..., "record": ...}
# ---------------------------------------------------------------------------

_BUILDERS = {
    "cancel_race": lambda tmp: (CANCEL_INPUTS, scenario_cancel_race(tmp)),
    "tool_confirm": lambda tmp: (TOOL_INPUTS, scenario_tool_confirm(tmp)),
    "history_images": lambda tmp: (HISTORY_INPUTS, scenario_history_images(tmp)),
}


def build(name: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        inputs, record = _BUILDERS[name](tmp_root)
        return {"inputs": inputs, "record": normalize(record, tmp_root)}


class GoldenReplayTests(unittest.TestCase):
    def _replay(self, name: str):
        baseline_path = GOLDEN_DIR / f"{name}.json"
        self.assertTrue(baseline_path.exists(), f"缺少基线文件 {baseline_path}（先运行 .tmptest/record_golden.py 录制）")
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        current = build(name)
        self.assertEqual(baseline["record"], current["record"], f"基线 {name} 行为漂移")

    def test_cancel_race_baseline(self):
        self._replay("cancel_race")

    def test_tool_confirm_baseline(self):
        self._replay("tool_confirm")

    def test_history_images_baseline(self):
        self._replay("history_images")


if __name__ == "__main__":
    unittest.main()
