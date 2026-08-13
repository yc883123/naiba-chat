from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


class ChatStorage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'online',
                    title_customized INTEGER NOT NULL DEFAULT 0,
                    system_prompt TEXT NOT NULL DEFAULT '',
                    stream_enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                    ON messages(conversation_id, created_at);
                CREATE TABLE IF NOT EXISTS tool_runs (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments TEXT NOT NULL,
                    result TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS background_tasks (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL DEFAULT '',
                    agent_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    snapshot TEXT NOT NULL DEFAULT '{}',
                    detail TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    started_at INTEGER,
                    updated_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_background_tasks_status
                    ON background_tasks(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_background_tasks_conversation
                    ON background_tasks(conversation_id, created_at DESC);
                """
            )
            # Migration: add mode column to existing tables
            try:
                db.execute("SELECT mode FROM conversations LIMIT 1")
            except sqlite3.OperationalError:
                db.execute("ALTER TABLE conversations ADD COLUMN mode TEXT NOT NULL DEFAULT 'online'")
            for column, definition in (
                ("title_customized", "INTEGER NOT NULL DEFAULT 0"),
                ("system_prompt", "TEXT NOT NULL DEFAULT ''"),
                ("stream_enabled", "INTEGER NOT NULL DEFAULT 1"),
                ("provider_id", "TEXT NOT NULL DEFAULT ''"),
                ("agent_id", "TEXT NOT NULL DEFAULT ''"),
            ):
                try:
                    db.execute(f"SELECT {column} FROM conversations LIMIT 1")
                except sqlite3.OperationalError:
                    db.execute(f"ALTER TABLE conversations ADD COLUMN {column} {definition}")
            now = int(time.time() * 1000)
            db.execute(
                "UPDATE background_tasks SET status = 'failed', error = ?, updated_at = ?, finished_at = ? "
                "WHERE status IN ('queued', 'running', 'waiting', 'cancelling')",
                ("服务重启，任务已中断", now, now),
            )

    def create_conversation(self, title: str = "新对话", provider_id: str = "", agent_id: str = "") -> dict[str, Any]:
        now = int(time.time() * 1000)
        conversation_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                "INSERT INTO conversations(id, title, mode, title_customized, system_prompt, stream_enabled, provider_id, agent_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, title.strip() or "新对话", "online", 0, "", 1, provider_id or "", agent_id or "", now, now),
            )
        return self.get_conversation(conversation_id, include_messages=False)

    def list_conversations(self, mode: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            if mode:
                rows = db.execute(
                    "SELECT id, title, mode, title_customized, system_prompt, stream_enabled, provider_id, agent_id, created_at, updated_at "
                    "FROM conversations WHERE mode = ? ORDER BY updated_at DESC",
                    (mode,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT id, title, mode, title_customized, system_prompt, stream_enabled, provider_id, agent_id, created_at, updated_at "
                    "FROM conversations ORDER BY updated_at DESC"
                ).fetchall()
        return [dict(row) for row in rows]

    def get_conversation(self, conversation_id: str, include_messages: bool = True) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id, title, mode, title_customized, system_prompt, stream_enabled, provider_id, agent_id, created_at, updated_at "
                "FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            if include_messages:
                messages = db.execute(
                    "SELECT id, role, content, metadata, created_at FROM messages "
                    "WHERE conversation_id = ? ORDER BY created_at, rowid",
                    (conversation_id,),
                ).fetchall()
                result["messages"] = [self._message_dict(message) for message in messages]
            return result

    def update_conversation_settings(
        self,
        conversation_id: str,
        title: str | None = None,
        system_prompt: str | None = None,
        stream_enabled: bool | None = None,
        provider_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Update settings owned by one conversation and return its summary."""
        values: dict[str, Any] = {}
        if title is not None:
            clean_title = " ".join(str(title).strip().split())[:120]
            if clean_title:
                values["title"] = clean_title
            else:
                with self._connect() as db:
                    first_user_message = db.execute(
                        "SELECT content FROM messages WHERE conversation_id = ? AND role = 'user' "
                        "ORDER BY created_at, rowid LIMIT 1",
                        (conversation_id,),
                    ).fetchone()
                values["title"] = (
                    " ".join(str(first_user_message[0]).strip().split())[:36]
                    if first_user_message and str(first_user_message[0]).strip()
                    else "新对话"
                )
            values["title_customized"] = 1 if clean_title else 0
        if system_prompt is not None:
            values["system_prompt"] = str(system_prompt).strip()[:20000]
        if stream_enabled is not None:
            values["stream_enabled"] = 1 if bool(stream_enabled) else 0
        if provider_id is not None:
            values["provider_id"] = str(provider_id or "")
        if agent_id is not None:
            values["agent_id"] = str(agent_id or "")
        if not values:
            return self.get_conversation(conversation_id, include_messages=False)
        assignments = ", ".join(f"{key} = ?" for key in values)
        parameters = [*values.values(), int(time.time() * 1000), conversation_id]
        with self._connect() as db:
            cursor = db.execute(
                f"UPDATE conversations SET {assignments}, updated_at = ? WHERE id = ?",
                parameters,
            )
            if cursor.rowcount == 0:
                return None
        return self.get_conversation(conversation_id, include_messages=False)

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = int(time.time() * 1000)
        message_id = uuid.uuid4().hex
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        with self._connect() as db:
            db.execute(
                "INSERT INTO messages(id, conversation_id, role, content, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (message_id, conversation_id, role, content, metadata_json, now),
            )
            db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            message_count = db.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            customized = db.execute(
                "SELECT title_customized FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()[0]
            if role == "user" and message_count <= 2 and not customized:
                title = " ".join(content.strip().split())[:36] or "新对话"
                db.execute(
                    "UPDATE conversations SET title = ? WHERE id = ?",
                    (title, conversation_id),
                )
        return {
            "id": message_id,
            "role": role,
            "content": content,
            "metadata": metadata or {},
            "created_at": now,
        }

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        return cursor.rowcount > 0

    def truncate_from_message(self, conversation_id: str, message_id: str) -> int:
        """删除某条消息及其之后（同一会话内 created_at 不早于它的）所有消息。

        返回删除的消息条数。用于"编辑历史消息后从该处重新开始"。
        """
        with self._connect() as db:
            target = db.execute(
                "SELECT created_at FROM messages WHERE id = ? AND conversation_id = ?",
                (message_id, conversation_id),
            ).fetchone()
            if not target:
                return 0
            created_at = target[0]
            # 先删关联的 tool_runs（按被删消息的 created_at 区间）
            db.execute(
                "DELETE FROM tool_runs WHERE conversation_id = ? AND created_at >= ?",
                (conversation_id, created_at),
            )
            cursor = db.execute(
                "DELETE FROM messages WHERE conversation_id = ? AND created_at >= ?",
                (conversation_id, created_at),
            )
            db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (int(time.time() * 1000), conversation_id),
            )
        return cursor.rowcount

    def log_tool_run(
        self,
        conversation_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: str,
        success: bool,
    ) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO tool_runs(id, conversation_id, tool_name, arguments, result, success, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    conversation_id,
                    tool_name,
                    json.dumps(arguments, ensure_ascii=False),
                    result[:50000],
                    1 if success else 0,
                    int(time.time() * 1000),
                ),
            )

    def create_background_task(
        self,
        conversation_id: str,
        message: str,
        agent: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        now = int(time.time() * 1000)
        task_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                "INSERT INTO background_tasks(id, conversation_id, agent_id, agent_name, status, message, snapshot, detail, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'queued', ?, ?, '{}', ?, ?)",
                (
                    task_id,
                    conversation_id,
                    str(agent.get("id") or ""),
                    str(agent.get("name") or "Agent"),
                    message,
                    json.dumps(snapshot, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.get_background_task(task_id) or {}

    def update_background_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        detail: dict[str, Any] | None = None,
        error: str | None = None,
        cancel_requested: bool | None = None,
        started: bool = False,
        finished: bool = False,
    ) -> dict[str, Any] | None:
        now = int(time.time() * 1000)
        values: dict[str, Any] = {"updated_at": now}
        if status is not None:
            values["status"] = status
        if detail is not None:
            values["detail"] = json.dumps(detail, ensure_ascii=False)
        if error is not None:
            values["error"] = error[:50000]
        if cancel_requested is not None:
            values["cancel_requested"] = 1 if cancel_requested else 0
        if started:
            values["started_at"] = now
        if finished:
            values["finished_at"] = now
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connect() as db:
            cursor = db.execute(
                f"UPDATE background_tasks SET {assignments} WHERE id = ?",
                (*values.values(), task_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_background_task(task_id)

    def get_background_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id, conversation_id, agent_id, agent_name, status, message, detail, error, "
                "cancel_requested, created_at, started_at, updated_at, finished_at "
                "FROM background_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        return self._task_dict(row) if row else None

    def list_background_tasks(
        self,
        conversation_id: str = "",
        active_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        conditions = []
        parameters: list[Any] = []
        if conversation_id:
            conditions.append("conversation_id = ?")
            parameters.append(conversation_id)
        if active_only:
            conditions.append("status IN ('queued', 'running', 'waiting', 'cancelling')")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.append(max(1, min(int(limit), 200)))
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, conversation_id, agent_id, agent_name, status, message, detail, error, "
                "cancel_requested, created_at, started_at, updated_at, finished_at "
                f"FROM background_tasks {where} ORDER BY created_at DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [self._task_dict(row) for row in rows]

    @staticmethod
    def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        try:
            result["metadata"] = json.loads(result.get("metadata") or "{}")
        except json.JSONDecodeError:
            result["metadata"] = {}
        return result

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        try:
            result["detail"] = json.loads(result.get("detail") or "{}")
        except json.JSONDecodeError:
            result["detail"] = {}
        result["cancel_requested"] = bool(result.get("cancel_requested"))
        return result
