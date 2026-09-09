# -*- coding: utf-8 -*-
"""P5 写回执行器：模拟"异步 Job 跑完"，用真实 JobMediaWriter 把产物写回消息。

由 `verify/p5_writeback_smoke.cjs` 在浏览器打开会话后调用（不重启服务、不刷新页面），
用于验证"前端既有轮询即可感知 metadata 变化"的设计。

用法：python verify/p5_writeback_apply.py [会话标题关键字]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.storage.job_media import JobMediaWriter  # noqa: E402
from naiba.storage.media_collect import MediaCollector  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402

P5_RUN_ID = "seedjobrun0001"


class _ConfigStub:
    def __init__(self, data_dir: Path) -> None:
        self.data = {"imaging": {}}
        self._data_dir = data_dir

    def resolve_data_dir(self) -> Path:
        return self._data_dir


def main() -> int:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "媒体内嵌"
    data_dir = ROOT / "data"
    storage = ChatStorage(data_dir / "chat.db")
    conversation = next(
        (item for item in storage.list_conversations() if keyword in str(item.get("title") or "")),
        None,
    )
    if not conversation:
        print(json.dumps({"error": f"未找到会话：{keyword}"}, ensure_ascii=False))
        return 1
    conversation_id = str(conversation["id"])
    messages = storage.get_conversation(conversation_id)["messages"]
    target = next(
        (
            message for message in messages
            if str((message.get("metadata") or {}).get("run_id") or "") == P5_RUN_ID
        ),
        None,
    )
    if not target:
        print(json.dumps({"error": "未找到 P5 播种消息"}, ensure_ascii=False))
        return 1

    fixture = data_dir / "generated" / "seed_fixture" / "seed_job.png"
    from PIL import Image

    fixture.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (240, 160), (220, 120, 40)).save(fixture)

    job = storage.create_run(
        conversation_id,
        "ComfyUI 批量生成",
        {"id": "", "name": "Job", "system_prompt": "", "skill_ids": []},
        {},
        kind="comfyui",
        parent_job_id=P5_RUN_ID,
        owner_session_id=conversation_id,
    )
    job_id = str(job["id"])
    # 真实链路：工具返回里就是真实 job_id（comfyui_batch wait=false）
    metadata = dict(target.get("metadata") or {})
    metadata["tool_runs"] = [dict(metadata["tool_runs"][0])]
    metadata["tool_runs"][0]["result"] = json.dumps({"job_id": job_id, "status": "queued", "total": 1}, ensure_ascii=False)
    storage.update_message_metadata(conversation_id, str(target["id"]), metadata)

    storage.update_job(
        job_id,
        status="completed",
        result={"completed_shots": [{"index": 0, "prompt_id": "p5", "files": [str(fixture)]}]},
        finished=True,
    )
    writer = JobMediaWriter(storage, _ConfigStub(data_dir), None, MediaCollector(_ConfigStub(data_dir)))
    written = writer.write_back(storage.get_background_task(job_id))
    print(json.dumps({"job_id": job_id, "written": written}, ensure_ascii=False))
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
