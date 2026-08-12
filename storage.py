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
            ):
                try:
                    db.execute(f"SELECT {column} FROM conversations LIMIT 1")
                except sqlite3.OperationalError:
                    db.execute(f"ALTER TABLE conversations ADD COLUMN {column} {definition}")

    def create_conversation(self, title: str = "新对话") -> dict[str, Any]:
        now = int(time.time() * 1000)
        conversation_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                "INSERT INTO conversations(id, title, mode, title_customized, system_prompt, stream_enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, title.strip() or "新对话", "online", 0, "", 1, now, now),
            )
        return self.get_conversation(conversation_id, include_messages=False)

    def list_conversations(self, mode: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            if mode:
                rows = db.execute(
                    "SELECT id, title, mode, title_customized, system_prompt, stream_enabled, created_at, updated_at "
                    "FROM conversations WHERE mode = ? ORDER BY updated_at DESC",
                    (mode,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT id, title, mode, title_customized, system_prompt, stream_enabled, created_at, updated_at "
                    "FROM conversations ORDER BY updated_at DESC"
                ).fetchall()
        return [dict(row) for row in rows]

    def get_conversation(self, conversation_id: str, include_messages: bool = True) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id, title, mode, title_customized, system_prompt, stream_enabled, created_at, updated_at "
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

    @staticmethod
    def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        try:
            result["metadata"] = json.loads(result.get("metadata") or "{}")
        except json.JSONDecodeError:
            result["metadata"] = {}
        return result
