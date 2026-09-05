"""Focused regression checks for one-click chat-chain cancellation（无插话版本）。

覆盖：取消父 Run 时级联取消其子 Job（http_poll 等），父任务进入 cancelling，
子任务进入 stopping。插话功能已于 2026-09-06 废弃（后端实现全部移除），
原 interjection stop / follow-up claim 用例随之删除。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from async_tasks import ConversationRunManager
from storage import ChatStorage


AGENT = {"id": "agent", "name": "Agent", "system_prompt": "", "skill_ids": []}


class RecordingJobs:
    def __init__(self, storage: ChatStorage):
        self.storage = storage
        self.cancelled: list[str] = []

    def cancel(self, job_id: str, owner=None, reason=None):
        job = self.storage.get_background_task(job_id)
        if not job or (owner and job.get("owner_session_id") != owner):
            return None
        self.cancelled.append(job_id)
        return self.storage.update_job(
            job_id,
            status="stopping",
            cancel_requested=True,
            detail={"message": reason or "cancelled"},
        )


class FakeApp:
    def __init__(self, database: Path):
        self.storage = ChatStorage(database)
        self.jobs = RecordingJobs(self.storage)


def new_app() -> tuple[FakeApp, str]:
    directory = Path(tempfile.mkdtemp(prefix="naiba-cancel-regression-"))
    app = FakeApp(directory / "chat.db")
    return app, str(app.storage.create_conversation()["id"])


def create_chat(app: FakeApp, conversation_id: str):
    run, _ = app.storage.create_chat_run(
        conversation_id,
        "hello",
        [],
        AGENT,
        {"agent": AGENT, "interaction_mode": "craft"},
        "craft",
    )
    app.storage.update_background_task(str(run["id"]), status="running", started=True)
    return app.storage.get_background_task(str(run["id"]))


def test_cancel_cascades_to_child_job() -> None:
    app, conversation_id = new_app()
    parent = create_chat(app, conversation_id)
    parent_id = str(parent["id"])
    child = app.storage.create_run(
        conversation_id,
        "poll child",
        AGENT,
        {},
        kind="http_poll",
        parent_job_id=parent_id,
        owner_session_id=conversation_id,
    )
    app.storage.update_job(str(child["id"]), status="running", started=True)

    manager = ConversationRunManager(app)
    cancelled = manager.cancel(parent_id)

    assert cancelled and cancelled["cancel_requested"]
    assert cancelled["status"] == "cancelling"
    assert str(child["id"]) in app.jobs.cancelled
    stopped_child = app.storage.get_background_task(str(child["id"]))
    assert stopped_child and stopped_child["status"] == "stopping"


if __name__ == "__main__":
    test_cancel_cascades_to_child_job()
    print("PASS cancel cascades to child Jobs")
