from __future__ import annotations

import json
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator


# 当前数据库 schema 版本（user_version）。每次新增迁移 +1。
CURRENT_SCHEMA_VERSION = 9


def _migrate_to_v1(db: sqlite3.Connection) -> None:
    """Schema 版本 1 的迁移：补齐历史列并回填 legacy model_key。

    全部操作幂等：列已存在时通过 `try: SELECT ... except OperationalError` 跳过 ALTER。
    """
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
        ("model_key", "TEXT NOT NULL DEFAULT ''"),
        ("agent_id", "TEXT NOT NULL DEFAULT ''"),
        ("interaction_mode", "TEXT NOT NULL DEFAULT 'craft'"),
        ("permission_mode", "TEXT NOT NULL DEFAULT 'confirm'"),
    ):
        try:
            db.execute(f"SELECT {column} FROM conversations LIMIT 1")
        except sqlite3.OperationalError:
            db.execute(f"ALTER TABLE conversations ADD COLUMN {column} {definition}")
    # 旧会话回填 model_key：legacy 仅使用 online 前缀（provider_id 一律按 online 处理）。
    try:
        db.execute(
            "UPDATE conversations SET model_key = 'online:' || provider_id "
            "WHERE model_key = '' AND provider_id != ''"
        )
    except sqlite3.OperationalError:
        pass
    for column, definition in (
        ("kind", "TEXT NOT NULL DEFAULT 'chat'"),
        ("interaction_mode", "TEXT NOT NULL DEFAULT 'craft'"),
        ("input_message_id", "TEXT NOT NULL DEFAULT ''"),
        ("plan_id", "TEXT NOT NULL DEFAULT ''"),
    ):
        try:
            db.execute(f"SELECT {column} FROM background_tasks LIMIT 1")
        except sqlite3.OperationalError:
            db.execute(f"ALTER TABLE background_tasks ADD COLUMN {column} {definition}")


def _migrate_to_v2(db: sqlite3.Connection) -> None:
    """Persist the web-search switch per conversation."""
    try:
        db.execute("SELECT web_search_enabled FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute(
            "ALTER TABLE conversations ADD COLUMN web_search_enabled "
            "INTEGER NOT NULL DEFAULT 0"
        )
    # Harness Job 字段（增量迁移，沿用现有 background_tasks 表，不新建平行表）
    for column, definition in (
        ("parent_job_id", "TEXT NOT NULL DEFAULT ''"),
        ("owner_session_id", "TEXT NOT NULL DEFAULT ''"),
        ("progress", "REAL NOT NULL DEFAULT 0"),
        ("current_step", "TEXT NOT NULL DEFAULT ''"),
        ("attempt", "INTEGER NOT NULL DEFAULT 0"),
        ("checkpoint", "TEXT NOT NULL DEFAULT '{}'"),
        ("result", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        try:
            db.execute(f"SELECT {column} FROM background_tasks LIMIT 1")
        except sqlite3.OperationalError:
            db.execute(f"ALTER TABLE background_tasks ADD COLUMN {column} {definition}")


def _migrate_to_v3(db: sqlite3.Connection) -> None:
    """Persist the deep-reasoning switch per conversation."""
    try:
        db.execute("SELECT deep_reasoning_enabled FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute(
            "ALTER TABLE conversations ADD COLUMN deep_reasoning_enabled "
            "INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_to_v4(db: sqlite3.Connection) -> None:
    """Persist the lightweight text-chat switch per conversation."""
    try:
        db.execute("SELECT lightweight_mode FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute(
            "ALTER TABLE conversations ADD COLUMN lightweight_mode "
            "INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_to_v5(db: sqlite3.Connection) -> None:
    """Persist the user-selected features disabled by lightweight mode."""
    try:
        db.execute("SELECT lightweight_disabled_features FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute(
            "ALTER TABLE conversations ADD COLUMN lightweight_disabled_features "
            "TEXT NOT NULL DEFAULT '[\"skills_tools\", \"vision\"]'"
        )


def _migrate_to_v6(db: sqlite3.Connection) -> None:
    """Make all four lightweight capabilities explicit for existing chats."""
    rows = db.execute(
        "SELECT id, lightweight_disabled_features FROM conversations"
    ).fetchall()
    allowed = ("skills_tools", "vision", "web_search", "deep_reasoning")
    for row in rows:
        try:
            current = json.loads(row[1] or "[]")
        except (json.JSONDecodeError, TypeError):
            current = []
        features = [item for item in current if item in allowed]
        for feature in allowed:
            if feature not in features:
                features.append(feature)
        db.execute(
            "UPDATE conversations SET lightweight_disabled_features = ? WHERE id = ?",
            (json.dumps(features, ensure_ascii=False), row[0]),
        )


def _migrate_to_v7(db: sqlite3.Connection) -> None:
    """Split the old combined lightweight switch into independent tools/skills flags."""
    rows = db.execute(
        "SELECT id, lightweight_disabled_features FROM conversations"
    ).fetchall()
    for row in rows:
        try:
            current = json.loads(row[1] or "[]")
        except (json.JSONDecodeError, TypeError):
            current = []
        features: list[str] = []
        if "skills_tools" in current:
            features.extend(("tools", "skills"))
        for feature in ("tools", "skills"):
            if feature in current and feature not in features:
                features.append(feature)
        db.execute(
            "UPDATE conversations SET lightweight_disabled_features = ? WHERE id = ?",
            (json.dumps(features, ensure_ascii=False), row[0]),
        )


def _migrate_to_v8(db: sqlite3.Connection) -> None:
    """Persist per-conversation workspace and reasoning intensity."""
    for column, definition in (
        ("workspace_dir", "TEXT NOT NULL DEFAULT ''"),
        ("reasoning_effort", "TEXT NOT NULL DEFAULT 'off'"),
    ):
        try:
            db.execute(f"SELECT {column} FROM conversations LIMIT 1")
        except sqlite3.OperationalError:
            db.execute(f"ALTER TABLE conversations ADD COLUMN {column} {definition}")
    # Preserve the old boolean switch for existing conversations.
    db.execute(
        "UPDATE conversations SET reasoning_effort = 'medium' "
        "WHERE deep_reasoning_enabled = 1 AND (reasoning_effort = '' OR reasoning_effort IS NULL OR reasoning_effort = 'off')"
    )


def _migrate_to_v9(db: sqlite3.Connection) -> None:
    """Persist per-session baked tool set (enabled_tool_ids) on conversations.

    会话启动时按当前 Agent 的工具集固化一次，之后不可改，用于实现“每 Agent 硬限制 +
    缓存稳定”。旧值默认空串，表示“未固化”。
    """
    try:
        db.execute("SELECT enabled_tool_ids FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        db.execute(
            "ALTER TABLE conversations ADD COLUMN enabled_tool_ids TEXT NOT NULL DEFAULT ''"
        )


# 目标版本 -> 迁移函数。新增版本时在此追加并提升 CURRENT_SCHEMA_VERSION。
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_to_v1,
    2: _migrate_to_v2,
    3: _migrate_to_v3,
    4: _migrate_to_v4,
    5: _migrate_to_v5,
    6: _migrate_to_v6,
    7: _migrate_to_v7,
    8: lambda db: _migrate_to_v8(db),
    9: _migrate_to_v9,
}


class ChatStorage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'online',
                    permission_mode TEXT NOT NULL DEFAULT 'confirm',
                    web_search_enabled INTEGER NOT NULL DEFAULT 0,
                    deep_reasoning_enabled INTEGER NOT NULL DEFAULT 0,
                    lightweight_mode INTEGER NOT NULL DEFAULT 0,
                    lightweight_disabled_features TEXT NOT NULL DEFAULT '["tools", "skills"]',
                    title_customized INTEGER NOT NULL DEFAULT 0,
                    system_prompt TEXT NOT NULL DEFAULT '',
                    stream_enabled INTEGER NOT NULL DEFAULT 1,
                    workspace_dir TEXT NOT NULL DEFAULT '',
                    reasoning_effort TEXT NOT NULL DEFAULT 'off',
                    enabled_tool_ids TEXT NOT NULL DEFAULT '',
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
                    kind TEXT NOT NULL DEFAULT 'chat',
                    interaction_mode TEXT NOT NULL DEFAULT 'craft',
                    input_message_id TEXT NOT NULL DEFAULT '',
                    plan_id TEXT NOT NULL DEFAULT '',
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
                CREATE TABLE IF NOT EXISTS run_events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(run_id, sequence),
                    FOREIGN KEY(run_id) REFERENCES background_tasks(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_run_events_run
                    ON run_events(run_id, sequence);
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'prepare',
                    question TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    steps TEXT NOT NULL DEFAULT '[]',
                    error TEXT NOT NULL DEFAULT '',
                    archive_path TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    started_at INTEGER,
                    finished_at INTEGER,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_plans_conversation
                    ON plans(conversation_id, created_at DESC);
                """
            )
            # 增量迁移（列新增 / 旧数据回填）由 apply_pending_migrations() 在重启清理之后统一执行。
            now = int(time.time() * 1000)
            interrupted = db.execute(
                "SELECT id FROM background_tasks "
                "WHERE status IN ('queued', 'running', 'waiting', 'cancelling')"
            ).fetchall()
            # Harness 对齐：运行中任务在服务重启后变为 interrupted，而非静默丢失
            db.execute(
                "UPDATE background_tasks SET status = 'interrupted', error = ?, updated_at = ?, finished_at = ? "
                "WHERE status IN ('queued', 'running', 'waiting', 'cancelling')",
                ("服务重启，运行已中断", now, now),
            )
            for row in interrupted:
                sequence = db.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id = ?",
                    (row["id"],),
                ).fetchone()[0]
                payload = json.dumps(
                    {"type": "error", "message": "服务重启，运行已中断"},
                    ensure_ascii=False,
                )
                db.execute(
                    "INSERT INTO run_events(run_id, sequence, event_type, payload, created_at) "
                    "VALUES (?, ?, 'error', ?, ?)",
                    (row["id"], sequence, payload, now),
                )
            # 服务重启时，仍在执行的计划标记为已取消，running 步骤回退为 pending
            stuck_plans = db.execute("SELECT id, steps FROM plans WHERE status = 'building'").fetchall()
            for plan_row in stuck_plans:
                try:
                    steps = json.loads(plan_row["steps"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    steps = []
                for step in steps:
                    if isinstance(step, dict) and step.get("status") == "running":
                        step["status"] = "pending"
                db.execute(
                    "UPDATE plans SET status = 'cancelled', error = '服务重启，执行已中断', steps = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(steps, ensure_ascii=False), now, plan_row["id"]),
                )
        # Schema 创建 + 重启清理完成后，应用尚未执行的版本化迁移。
        self.apply_pending_migrations()
        # Keep legacy data repair idempotent after the schema reaches v1.
        # Older builds may have written rows after the version was recorded.
        self._repair_legacy_model_keys()
        self._disable_legacy_plan_mode()

    def _repair_legacy_model_keys(self) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE conversations SET model_key = 'online:' || provider_id "
                "WHERE model_key = '' AND provider_id != ''"
            )

    def _disable_legacy_plan_mode(self) -> None:
        """Keep historical plans, but make every conversation use normal chat."""
        with self._connect() as db:
            db.execute("UPDATE conversations SET interaction_mode = 'craft' WHERE interaction_mode != 'craft'")

    def apply_pending_migrations(self) -> None:
        """依次应用尚未执行的迁移，直到 user_version == CURRENT_SCHEMA_VERSION。"""
        with self._connect() as db:
            while int(db.execute("PRAGMA user_version").fetchone()[0]) < CURRENT_SCHEMA_VERSION:
                target = int(db.execute("PRAGMA user_version").fetchone()[0]) + 1
                migration = MIGRATIONS.get(target)
                if migration is None:
                    # 没有对应迁移定义则向前跳版本，避免死循环。
                    db.execute(f"PRAGMA user_version = {int(target)}")
                    continue
                migration(db)
                db.execute(f"PRAGMA user_version = {int(target)}")

    def get_user_version(self) -> int:
        with self._connect() as db:
            return int(db.execute("PRAGMA user_version").fetchone()[0])

    def set_user_version(self, version: int) -> None:
        with self._connect() as db:
            db.execute(f"PRAGMA user_version = {int(version)}")

    def check_integrity(self) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute("PRAGMA integrity_check").fetchall()
        details = [str(row[0]) for row in rows]
        return {"ok": all(d == "ok" for d in details), "details": details}

    def backup_for_migration(self, backup_dir: Path) -> dict[str, Any]:
        files: list[str] = []
        error: str | None = None
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            for suffix in ("", "-wal", "-shm"):
                source = Path(str(self.db_path) + suffix)
                if source.exists():
                    destination = backup_dir / source.name
                    shutil.copy2(source, destination)
                    files.append(str(destination))
        except Exception as exc:  # noqa: BLE001 - 备份失败需要以 error 形式返回
            error = str(exc)
        return {"backup_dir": str(backup_dir), "files": files, "error": error}

    @property
    def data_dir(self) -> Path:
        return self.db_path.parent

    @property
    def health(self) -> dict[str, Any]:
        db_version = self.get_user_version()
        healthy = self.check_integrity()["ok"]
        applied = [v for v in sorted(MIGRATIONS) if v <= CURRENT_SCHEMA_VERSION]
        return {
            "db_version": db_version,
            "data_dir": str(self.data_dir),
            "healthy": healthy,
            "migrations": applied,
        }

    def create_conversation(
        self,
        title: str = "新对话",
        provider_id: str = "",
        agent_id: str = "",
        interaction_mode: str = "craft",
        model_key: str = "",
        permission_mode: str = "auto",
        web_search_enabled: bool = False,
        deep_reasoning_enabled: bool = False,
        lightweight_mode: bool = False,
        workspace_dir: str = "",
        reasoning_effort: str = "auto",
    ) -> dict[str, Any]:
        now = int(time.time() * 1000)
        conversation_id = uuid.uuid4().hex
        interaction_mode = "craft"
        if permission_mode not in ("confirm", "auto", "full"):
            permission_mode = "auto"
        resolved_model_key = str(model_key or "").strip()
        if not resolved_model_key and provider_id:
            resolved_model_key = f"online:{provider_id}"
        with self._connect() as db:
            db.execute(
                "INSERT INTO conversations(id, title, mode, permission_mode, web_search_enabled, deep_reasoning_enabled, lightweight_mode, lightweight_disabled_features, title_customized, system_prompt, stream_enabled, workspace_dir, reasoning_effort, provider_id, model_key, agent_id, interaction_mode, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    title.strip() or "新对话",
                    "online",
                    permission_mode,
                    1 if web_search_enabled else 0,
                    1 if deep_reasoning_enabled else 0,
                    1 if lightweight_mode else 0,
                    json.dumps(["tools", "skills"]),
                    0,
                    "",
                    1,
                    str(workspace_dir or ""),
                    str(reasoning_effort or ("medium" if deep_reasoning_enabled else "auto")),
                    provider_id or "",
                    resolved_model_key,
                    agent_id or "",
                    interaction_mode,
                    now,
                    now,
                ),
            )
        return self.get_conversation(conversation_id, include_messages=False)

    def list_conversations(self, mode: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            if mode:
                rows = db.execute(
                    "SELECT id, title, mode, permission_mode, web_search_enabled, deep_reasoning_enabled, lightweight_mode, lightweight_disabled_features, title_customized, system_prompt, stream_enabled, workspace_dir, reasoning_effort, enabled_tool_ids, provider_id, model_key, agent_id, interaction_mode, created_at, updated_at "
                    "FROM conversations WHERE mode = ? ORDER BY updated_at DESC",
                    (mode,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT id, title, mode, permission_mode, web_search_enabled, deep_reasoning_enabled, lightweight_mode, lightweight_disabled_features, title_customized, system_prompt, stream_enabled, workspace_dir, reasoning_effort, enabled_tool_ids, provider_id, model_key, agent_id, interaction_mode, created_at, updated_at "
                    "FROM conversations ORDER BY updated_at DESC"
                ).fetchall()
        return [self._conversation_dict(row) for row in rows]

    def get_conversation(self, conversation_id: str, include_messages: bool = True) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id, title, mode, permission_mode, web_search_enabled, deep_reasoning_enabled, lightweight_mode, lightweight_disabled_features, title_customized, system_prompt, stream_enabled, workspace_dir, reasoning_effort, enabled_tool_ids, provider_id, model_key, agent_id, interaction_mode, created_at, updated_at "
                "FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if not row:
                return None
            result = self._conversation_dict(row)
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
        model_key: str | None = None,
        agent_id: str | None = None,
        interaction_mode: str | None = None,
        permission_mode: str | None = None,
        web_search_enabled: bool | None = None,
        deep_reasoning_enabled: bool | None = None,
        lightweight_mode: bool | None = None,
        lightweight_disabled_features: list[str] | None = None,
        workspace_dir: str | None = None,
        reasoning_effort: str | None = None,
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
        if model_key is not None:
            # 切换模型只更新该会话的 model_key，不影响其他会话与正在运行的 Run。
            values["model_key"] = str(model_key or "")
        if agent_id is not None:
            values["agent_id"] = str(agent_id or "")
        if interaction_mode is not None:
            if not isinstance(interaction_mode, str):
                raise ValueError("interaction_mode 必须是文本")
            normalized = interaction_mode.strip().lower()
            if normalized not in {"plan", "craft", "ask"}:
                raise ValueError("interaction_mode 必须是 plan 或普通模式")
            values["interaction_mode"] = "craft"
        if permission_mode is not None:
            if permission_mode not in ("confirm", "auto", "full"):
                raise ValueError("permission_mode 必须是 confirm / auto / full")
            values["permission_mode"] = permission_mode
        if web_search_enabled is not None:
            values["web_search_enabled"] = 1 if bool(web_search_enabled) else 0
        if deep_reasoning_enabled is not None:
            values["deep_reasoning_enabled"] = 1 if bool(deep_reasoning_enabled) else 0
            if reasoning_effort is None:
                values["reasoning_effort"] = "medium" if bool(deep_reasoning_enabled) else "off"
        if reasoning_effort is not None:
            effort = str(reasoning_effort or "off").strip().lower()
            if effort not in {"off", "low", "medium", "high", "auto"}:
                raise ValueError("reasoning_effort 必须是 off / low / medium / high / auto")
            values["reasoning_effort"] = effort
            values["deep_reasoning_enabled"] = 0 if effort == "off" else 1
        if lightweight_mode is not None:
            values["lightweight_mode"] = 1 if bool(lightweight_mode) else 0
        if lightweight_disabled_features is not None:
            allowed = {"tools", "skills"}
            values["lightweight_disabled_features"] = json.dumps(
                [item for item in lightweight_disabled_features if item in allowed], ensure_ascii=False
            )
        if workspace_dir is not None:
            values["workspace_dir"] = str(workspace_dir or "").strip()
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

    def set_enabled_tool_ids(self, conversation_id: str, tool_ids: list[str] | tuple[str, ...] | set[str]) -> None:
        """固化某会话的启用工具集（会话启动时写入，之后不可改）。"""
        with self._connect() as db:
            db.execute(
                "UPDATE conversations SET enabled_tool_ids = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps(list(dict.fromkeys(str(item) for item in tool_ids)), ensure_ascii=False),
                    int(time.time() * 1000),
                    conversation_id,
                ),
            )

    def message_count(self, conversation_id: str) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return int(row[0]) if row else 0

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

    def clear_conversation_messages(self, conversation_id: str) -> int:
        """Clear persisted chat/tool history while retaining the conversation settings."""
        with self._connect() as db:
            db.execute("DELETE FROM tool_runs WHERE conversation_id = ?", (conversation_id,))
            cursor = db.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (int(time.time() * 1000), conversation_id),
            )
        return cursor.rowcount

    def truncate_from_message(self, conversation_id: str, message_id: str) -> int:
        """删除某条消息及其之后（同一会话内按 (created_at, rowid) 排序不早于它的）所有消息。

        返回删除的消息条数。用于"编辑历史消息后从该处重新开始"。

        使用 SQLite 隐式 rowid 做复合排序定位编辑点：同毫秒时间戳的多条消息
        也保证只删编辑点及之后，前缀消息（含 metadata.trace / reasoning）字节级不变，
        从而使编辑重发时能命中 OpenAI/DeepSeek 等提供商的自动前缀缓存。
        """
        with self._connect() as db:
            target = db.execute(
                "SELECT created_at, rowid FROM messages WHERE id = ? AND conversation_id = ?",
                (message_id, conversation_id),
            ).fetchone()
            if not target:
                return 0
            created_at = target[0]
            rowid = target[1]
            # 先删关联的 tool_runs：工具结果必然晚于对应用户消息，保持 created_at 区间删除
            # （tool_runs 无 message 外键，同毫秒边界误删风险可忽略）。
            db.execute(
                "DELETE FROM tool_runs WHERE conversation_id = ? AND created_at >= ?",
                (conversation_id, created_at),
            )
            # 精确删除编辑点及其之后的消息：按 (created_at, rowid) 复合条件，
            # 前缀消息一律保留。
            cursor = db.execute(
                "DELETE FROM messages WHERE conversation_id = ? "
                "AND (created_at > ? OR (created_at = ? AND rowid >= ?))",
                (conversation_id, created_at, created_at, rowid),
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
        return self.create_run(
            conversation_id,
            message,
            agent,
            snapshot,
            interaction_mode=str(snapshot.get("interaction_mode") or "craft"),
            plan_id=str(snapshot.get("plan_id") or ""),
        )

    def active_run(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id, conversation_id, kind, interaction_mode, input_message_id, plan_id, "
                "agent_id, agent_name, status, message, detail, error, cancel_requested, "
                "created_at, started_at, updated_at, finished_at "
                "FROM background_tasks WHERE conversation_id = ? "
                "AND status IN ('queued', 'running', 'waiting', 'cancelling') "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return self._task_dict(row) if row else None

    def add_run_interjection(
        self,
        conversation_id: str,
        run_id: str,
        content: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Create a pending user instruction for an active Run.

        Pending instructions are intentionally not visible to the agent until the
        user explicitly chooses to guide the Run with them.
        """
        now = int(time.time() * 1000)
        message_id = uuid.uuid4().hex
        metadata = {
            "attachments": attachments or [],
            "run_id": run_id,
            "interjection": True,
            "interjection_guided": False,
        }
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT id FROM background_tasks WHERE id = ? AND conversation_id = ? "
                "AND status IN ('queued', 'running', 'waiting', 'cancelling')",
                (run_id, conversation_id),
            ).fetchone()
            if not active:
                raise LookupError("运行不存在或已结束")
            db.execute(
                "INSERT INTO messages(id, conversation_id, role, content, metadata, created_at) "
                "VALUES (?, ?, 'user', ?, ?, ?)",
                (message_id, conversation_id, content, json.dumps(metadata, ensure_ascii=False), now),
            )
            db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        return {
            "id": message_id,
            "role": "user",
            "content": content,
            "metadata": metadata,
            "created_at": now,
        }

    def list_run_interjections(self, run_id: str) -> list[dict[str, Any]]:
        run = self.get_background_task(run_id)
        if not run:
            return []
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, role, content, metadata, created_at FROM messages "
                "WHERE conversation_id = ? AND role = 'user' ORDER BY created_at, rowid",
                (str(run.get("conversation_id") or ""),),
            ).fetchall()
        return [
            message for message in (self._message_dict(row) for row in rows)
            if bool(message.get("metadata", {}).get("interjection"))
            and str(message.get("metadata", {}).get("run_id") or "") == run_id
            and bool(message.get("metadata", {}).get("interjection_guided"))
            and not bool(message.get("metadata", {}).get("interjection_consumed"))
        ]

    def guide_run_interjection(
        self, conversation_id: str, run_id: str, message_id: str
    ) -> dict[str, Any]:
        """Expose one pending instruction to the active Run."""
        now = int(time.time() * 1000)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT id FROM background_tasks WHERE id = ? AND conversation_id = ? "
                "AND status IN ('queued', 'running', 'waiting', 'cancelling')",
                (run_id, conversation_id),
            ).fetchone()
            if not active:
                raise LookupError("运行不存在或已结束")
            row = db.execute(
                "SELECT id, role, content, metadata, created_at FROM messages "
                "WHERE id = ? AND conversation_id = ? AND role = 'user'",
                (message_id, conversation_id),
            ).fetchone()
            if not row:
                raise LookupError("待引导消息不存在")
            message = self._message_dict(row)
            metadata = dict(message.get("metadata") or {})
            if (not metadata.get("interjection")
                    or str(metadata.get("run_id") or "") != run_id
                    or metadata.get("interjection_guided")
                    or metadata.get("interjection_consumed")):
                raise LookupError("待引导消息不存在或已被处理")
            metadata["interjection_guided"] = True
            db.execute(
                "UPDATE messages SET metadata = ? WHERE id = ?",
                (json.dumps(metadata, ensure_ascii=False), message_id),
            )
            db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        message["metadata"] = metadata
        return message

    def mark_run_interjections_consumed(self, run_id: str, message_ids: list[str]) -> None:
        ids = [str(message_id).strip() for message_id in message_ids if str(message_id).strip()]
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        with self._connect() as db:
            rows = db.execute(
                f"SELECT id, metadata FROM messages WHERE id IN ({placeholders})", ids
            ).fetchall()
            for row in rows:
                try:
                    metadata = json.loads(row["metadata"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    metadata = {}
                if str(metadata.get("run_id") or "") != run_id or not metadata.get("interjection"):
                    continue
                metadata["interjection_consumed"] = True
                db.execute(
                    "UPDATE messages SET metadata = ? WHERE id = ?",
                    (json.dumps(metadata, ensure_ascii=False), row["id"]),
                )

    def delete_run_interjection(
        self, conversation_id: str, run_id: str, message_id: str
    ) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "DELETE FROM messages WHERE id = ? AND conversation_id = ? AND role = 'user' "
                "AND json_extract(metadata, '$.run_id') = ? "
                "AND json_extract(metadata, '$.interjection') = 1 "
                "AND COALESCE(json_extract(metadata, '$.interjection_guided'), 0) = 0 "
                "AND COALESCE(json_extract(metadata, '$.interjection_consumed'), 0) = 0",
                (message_id, conversation_id, run_id),
            )
            if cursor.rowcount:
                db.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (int(time.time() * 1000), conversation_id),
                )
        return bool(cursor.rowcount)

    def edit_run_interjection(
        self, conversation_id: str, run_id: str, message_id: str, content: str
    ) -> dict[str, Any]:
        """Update a still-queued interjection without deleting its record."""
        content = str(content or "").strip()
        if not content:
            raise ValueError("插话内容不能为空")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id, role, content, metadata, created_at FROM messages "
                "WHERE id = ? AND conversation_id = ? AND role = 'user'",
                (message_id, conversation_id),
            ).fetchone()
            if not row:
                raise LookupError("待发送消息不存在")
            message = self._message_dict(row)
            metadata = dict(message.get("metadata") or {})
            if (not metadata.get("interjection") or str(metadata.get("run_id") or "") != run_id
                    or metadata.get("interjection_guided") or metadata.get("interjection_consumed")):
                raise LookupError("待发送消息不存在或已被处理")
            db.execute("UPDATE messages SET content = ? WHERE id = ?", (content, message_id))
            db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (int(time.time() * 1000), conversation_id))
        return {**message, "content": content}

    def claim_interjections_for_followup(
        self, parent_run_id: str, snapshot: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Atomically turn remaining queued rows into one follow-up chat Run.

        The user messages remain their original records; only their metadata is
        marked as dispatched so a completion can never silently discard them.
        """
        now = int(time.time() * 1000)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            parent = db.execute(
                "SELECT conversation_id FROM background_tasks WHERE id = ? "
                "AND status = 'completed'", (parent_run_id,)
            ).fetchone()
            if not parent:
                return None
            conversation_id = str(parent["conversation_id"])
            active = db.execute(
                "SELECT id FROM background_tasks WHERE conversation_id = ? "
                "AND status IN ('queued', 'running', 'waiting', 'cancelling') LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if active:
                return None
            rows = db.execute(
                "SELECT id, role, content, metadata, created_at FROM messages "
                "WHERE conversation_id = ? AND role = 'user' ORDER BY created_at, rowid",
                (conversation_id,),
            ).fetchall()
            queued: list[dict[str, Any]] = []
            for row in rows:
                message = self._message_dict(row)
                metadata = message.get("metadata") or {}
                if (metadata.get("interjection") and str(metadata.get("run_id") or "") == parent_run_id
                        and not metadata.get("interjection_guided") and not metadata.get("interjection_consumed")
                        and not metadata.get("interjection_dispatched")):
                    queued.append(message)
            if not queued:
                return None
            run_id = uuid.uuid4().hex
            for message in queued:
                metadata = dict(message.get("metadata") or {})
                metadata["interjection_dispatched"] = True
                metadata["interjection_followup_run_id"] = run_id
                db.execute("UPDATE messages SET metadata = ? WHERE id = ?", (
                    json.dumps(metadata, ensure_ascii=False), message["id"]
                ))
            history_rows = db.execute(
                "SELECT id, role, content, metadata, created_at FROM messages "
                "WHERE conversation_id = ? ORDER BY created_at, rowid", (conversation_id,)
            ).fetchall()
            frozen = dict(snapshot)
            frozen["conversation_messages"] = [self._message_dict(row) for row in history_rows]
            message = "\n".join(str(item.get("content") or "") for item in queued).strip()
            agent = frozen.get("agent") or {}
            db.execute(
                "INSERT INTO background_tasks("
                "id, conversation_id, kind, interaction_mode, input_message_id, plan_id, "
                "agent_id, agent_name, status, message, snapshot, detail, created_at, updated_at, "
                "parent_job_id, owner_session_id"
                ") VALUES (?, ?, 'chat', ?, ?, '', ?, ?, 'queued', ?, ?, '{}', ?, ?, '', ?)",
                (run_id, conversation_id, str(frozen.get("interaction_mode") or "craft"),
                 str(queued[0].get("id") or ""), str(agent.get("id") or ""),
                 str(agent.get("name") or "Agent"), message, json.dumps(frozen, ensure_ascii=False),
                 now, now, conversation_id),
            )
            db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        return self.get_background_task(run_id)

    def create_chat_run(
        self,
        conversation_id: str,
        message: str,
        attachments: list[dict[str, Any]],
        agent: dict[str, Any],
        snapshot: dict[str, Any],
        interaction_mode: str,
        plan_id: str = "",
        parent_job_id: str = "",
        owner_session_id: str = "",
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Atomically append the user message and create its owning run."""
        now = int(time.time() * 1000)
        run_id = uuid.uuid4().hex
        message_id = uuid.uuid4().hex
        metadata = {
            "attachments": attachments,
            "run_id": run_id,
            "agent_id": str(agent.get("id") or ""),
        }
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # A parent chat Run is allowed to spawn child Jobs.  The previous
            # global conversation lock rejected those children with
            # ACTIVE_RUN, leaving the parent Agent stuck after it attempted a
            # background operation.
            if not parent_job_id:
                active = db.execute(
                    "SELECT id FROM background_tasks WHERE conversation_id = ? "
                    "AND status IN ('queued', 'running', 'waiting', 'cancelling') LIMIT 1",
                    (conversation_id,),
                ).fetchone()
                if active:
                    raise RuntimeError(f"ACTIVE_RUN:{active['id']}")
            conversation = db.execute(
                "SELECT title_customized FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if not conversation:
                raise LookupError("对话不存在")
            db.execute(
                "INSERT INTO messages(id, conversation_id, role, content, metadata, created_at) "
                "VALUES (?, ?, 'user', ?, ?, ?)",
                (message_id, conversation_id, message, json.dumps(metadata, ensure_ascii=False), now),
            )
            rows = db.execute(
                "SELECT id, role, content, metadata, created_at FROM messages "
                "WHERE conversation_id = ? ORDER BY created_at, rowid",
                (conversation_id,),
            ).fetchall()
            history = [self._message_dict(row) for row in rows]
            frozen = dict(snapshot)
            frozen["conversation_messages"] = history
            db.execute(
                "INSERT INTO background_tasks("
                "id, conversation_id, kind, interaction_mode, input_message_id, plan_id, "
                "agent_id, agent_name, status, message, snapshot, detail, created_at, updated_at, "
                "parent_job_id, owner_session_id"
                ") VALUES (?, ?, 'chat', ?, ?, ?, ?, ?, 'queued', ?, ?, '{}', ?, ?, ?, ?)",
                (
                    run_id,
                    conversation_id,
                    interaction_mode,
                    message_id,
                    plan_id,
                    str(agent.get("id") or ""),
                    str(agent.get("name") or "Agent"),
                    message,
                    json.dumps(frozen, ensure_ascii=False),
                    now,
                    now,
                    parent_job_id,
                    owner_session_id or conversation_id,
                ),
            )
            db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
            message_count = db.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            if message_count <= 2 and not conversation["title_customized"]:
                title = " ".join(message.strip().split())[:36] or "新对话"
                db.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id))
        return self.get_background_task(run_id) or {}, history

    def create_run(
        self,
        conversation_id: str,
        message: str,
        agent: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        kind: str = "chat",
        interaction_mode: str = "craft",
        input_message_id: str = "",
        plan_id: str = "",
        parent_job_id: str = "",
        owner_session_id: str = "",
    ) -> dict[str, Any]:
        now = int(time.time() * 1000)
        task_id = uuid.uuid4().hex
        owner = owner_session_id or conversation_id
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not parent_job_id:
                active = db.execute(
                    "SELECT id FROM background_tasks WHERE conversation_id = ? "
                    "AND status IN ('queued', 'running', 'waiting', 'cancelling') LIMIT 1",
                    (conversation_id,),
                ).fetchone()
                if active:
                    raise RuntimeError(f"ACTIVE_RUN:{active['id']}")
            db.execute(
                "INSERT INTO background_tasks("
                "id, conversation_id, kind, interaction_mode, input_message_id, plan_id, "
                "agent_id, agent_name, status, message, snapshot, detail, created_at, updated_at, "
                "parent_job_id, owner_session_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, '{}', ?, ?, ?, ?)",
                (
                    task_id,
                    conversation_id,
                    kind,
                    interaction_mode,
                    input_message_id,
                    plan_id,
                    str(agent.get("id") or ""),
                    str(agent.get("name") or "Agent"),
                    message,
                    json.dumps(snapshot, ensure_ascii=False),
                    now,
                    now,
                    parent_job_id,
                    owner,
                ),
            )
        return self.get_background_task(task_id) or {}

    def get_run_snapshot(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT snapshot FROM background_tasks WHERE id = ?", (run_id,)
            ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row["snapshot"] or "{}")
        except (json.JSONDecodeError, TypeError):
            value = {}
        return value if isinstance(value, dict) else {}

    def append_run_event(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time() * 1000)
        event_type = str(payload.get("type") or "event")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute(
                "SELECT 1 FROM background_tasks WHERE id = ?", (run_id,)
            ).fetchone()
            if not exists:
                raise LookupError("运行不存在")
            sequence = db.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO run_events(run_id, sequence, event_type, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, sequence, event_type, json.dumps(payload, ensure_ascii=False), now),
            )
        return {**payload, "run_id": run_id, "sequence": sequence, "created_at": now}

    def list_run_events(self, run_id: str, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT sequence, payload, created_at FROM run_events "
                "WHERE run_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
                (run_id, max(0, int(after)), max(1, min(int(limit), 2000))),
            ).fetchall()
        events = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (json.JSONDecodeError, TypeError):
                payload = {"type": "error", "message": "运行事件损坏"}
            if not isinstance(payload, dict):
                payload = {"type": "error", "message": "运行事件损坏"}
            events.append(
                {**payload, "run_id": run_id, "sequence": row["sequence"], "created_at": row["created_at"]}
            )
        return events

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
            if status not in {"queued", "running", "waiting", "stopping", "cancelling", "completed", "failed", "cancelled", "interrupted"}:
                raise ValueError("非法的 Run 状态")
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

    def update_job(
        self,
        task_id: str,
        *,
        status: str | None = None,
        progress: float | None = None,
        current_step: str | None = None,
        attempt: int | None = None,
        checkpoint: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        detail: dict[str, Any] | None = None,
        cancel_requested: bool | None = None,
        started: bool = False,
        finished: bool = False,
    ) -> dict[str, Any] | None:
        """更新 Harness Job 专用字段（沿用 background_tasks 表）。"""
        now = int(time.time() * 1000)
        values: dict[str, Any] = {"updated_at": now}
        if status is not None:
            if status not in {"queued", "running", "waiting", "stopping", "cancelling", "completed", "failed", "cancelled", "interrupted"}:
                raise ValueError("非法的 Job 状态")
            values["status"] = status
        if progress is not None:
            values["progress"] = max(0.0, min(100.0, float(progress)))
        if current_step is not None:
            values["current_step"] = str(current_step)[:2000]
        if attempt is not None:
            values["attempt"] = int(attempt)
        if checkpoint is not None:
            values["checkpoint"] = json.dumps(checkpoint, ensure_ascii=False)
        if result is not None:
            values["result"] = json.dumps(result, ensure_ascii=False)
        if error is not None:
            values["error"] = error[:50000]
        if detail is not None:
            values["detail"] = json.dumps(detail, ensure_ascii=False)
        if cancel_requested is not None:
            values["cancel_requested"] = 1 if cancel_requested else 0
        if started:
            values["started_at"] = now
        if finished:
            values["finished_at"] = now
        if not values:
            return self.get_background_task(task_id)
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
                "SELECT id, conversation_id, kind, interaction_mode, input_message_id, plan_id, "
"agent_id, agent_name, status, message, detail, error, "
"cancel_requested, created_at, started_at, updated_at, finished_at, "
"parent_job_id, owner_session_id, progress, current_step, attempt, checkpoint, result "
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
                "SELECT id, conversation_id, kind, interaction_mode, input_message_id, plan_id, "
"agent_id, agent_name, status, message, detail, error, "
"cancel_requested, created_at, started_at, updated_at, finished_at, "
"parent_job_id, owner_session_id, progress, current_step, attempt, checkpoint, result "
                f"FROM background_tasks {where} ORDER BY created_at DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [self._task_dict(row) for row in rows]

    def clear_terminal_background_tasks(self) -> int:
        """Remove completed task records and their cascaded run events, never active runs."""
        with self._connect() as db:
            cursor = db.execute(
                "DELETE FROM background_tasks WHERE status IN ('completed', 'failed', 'cancelled', 'interrupted')"
            )
        return cursor.rowcount

    # ---- 计划（Plan 模式） ----
    def create_plan(self, conversation_id: str, question: str) -> dict[str, Any]:
        now = int(time.time() * 1000)
        plan_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                "INSERT INTO plans(id, conversation_id, title, status, question, content, steps, created_at, updated_at) "
                "VALUES (?, ?, '', 'prepare', ?, '', '[]', ?, ?)",
                (plan_id, conversation_id, (question or "")[:20000], now, now),
            )
        return self.get_plan(plan_id) or {}

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id, conversation_id, title, status, question, content, steps, error, archive_path, detail, "
                "created_at, updated_at, started_at, finished_at FROM plans WHERE id = ?",
                (plan_id,),
            ).fetchone()
        return self._plan_dict(row) if row else None

    def latest_plan(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id, conversation_id, title, status, question, content, steps, error, archive_path, detail, "
                "created_at, updated_at, started_at, finished_at FROM plans "
                "WHERE conversation_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return self._plan_dict(row) if row else None

    def list_plans(self, conversation_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, conversation_id, title, status, question, content, steps, error, archive_path, detail, "
                "created_at, updated_at, started_at, finished_at FROM plans "
                "WHERE conversation_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (conversation_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [self._plan_dict(row) for row in rows]

    def update_plan(
        self,
        plan_id: str,
        *,
        title: str | None = None,
        status: str | None = None,
        question: str | None = None,
        content: str | None = None,
        steps: list[dict[str, Any]] | None = None,
        error: str | None = None,
        archive_path: str | None = None,
        detail: dict[str, Any] | None = None,
        started: bool = False,
        finished: bool = False,
    ) -> dict[str, Any] | None:
        now = int(time.time() * 1000)
        values: dict[str, Any] = {"updated_at": now}
        if title is not None:
            values["title"] = str(title).strip()[:200]
        if status is not None:
            if status not in ("prepare", "ready", "building", "finished", "failed", "cancelled"):
                raise ValueError("非法的计划状态")
            values["status"] = status
        if question is not None:
            values["question"] = str(question)[:20000]
        if content is not None:
            values["content"] = str(content)[:100000]
        if steps is not None:
            values["steps"] = json.dumps(steps, ensure_ascii=False)
        if error is not None:
            values["error"] = str(error)[:20000]
        if archive_path is not None:
            values["archive_path"] = str(archive_path)[:2000]
        if detail is not None:
            values["detail"] = json.dumps(detail, ensure_ascii=False)
        if started:
            values["started_at"] = now
        if finished:
            values["finished_at"] = now
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connect() as db:
            cursor = db.execute(
                f"UPDATE plans SET {assignments} WHERE id = ?",
                (*values.values(), plan_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_plan(plan_id)

    @staticmethod
    def _conversation_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["interaction_mode"] = "craft"
        try:
            features = json.loads(
                result.get("lightweight_disabled_features") or "[]"
            )
            result["lightweight_disabled_features"] = [
                item for item in features
                if item in {"tools", "skills"}
            ]
        except (json.JSONDecodeError, TypeError):
            result["lightweight_disabled_features"] = []
        try:
            result["enabled_tool_ids"] = json.loads(
                result.get("enabled_tool_ids") or "[]"
            )
        except (json.JSONDecodeError, TypeError):
            result["enabled_tool_ids"] = []
        return result

    @staticmethod
    def _plan_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in ("steps", "detail"):
            try:
                result[key] = json.loads(result.get(key) or ("[]" if key == "steps" else "{}"))
            except (json.JSONDecodeError, TypeError):
                result[key] = [] if key == "steps" else {}
        return result

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
        for key in ("checkpoint", "result"):
            try:
                result[key] = json.loads(result.get(key) or "{}")
            except (json.JSONDecodeError, TypeError):
                result[key] = {}
        try:
            result["progress"] = float(result.get("progress") or 0)
        except (TypeError, ValueError):
            result["progress"] = 0.0
        result["attempt"] = int(result.get("attempt") or 0)
        result["cancel_requested"] = bool(result.get("cancel_requested"))
        return result
