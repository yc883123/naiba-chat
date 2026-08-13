from __future__ import annotations

import asyncio
import os
import threading
from contextlib import AsyncExitStack
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any


class MCPServerConnection:
    def __init__(self, server_id: str, command: str, args: list[str], env: dict[str, str]):
        self.server_id = server_id
        self.command = command
        self.args = args
        self.env = env
        self.tools: list[dict[str, Any]] = []
        self.error = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session = None
        self._stack: AsyncExitStack | None = None
        self._stop_signal: asyncio.Event | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._call_lock = threading.Lock()
        self._stopping = False
        self._stopped = False

    def start(self, timeout: int = 20) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.error = ""
        self._ready = threading.Event()
        self._stopping = False
        self._stopped = False
        self._session = None
        self._stack = None
        self._loop = None
        self._stop_signal = None
        self._thread = threading.Thread(target=self._thread_main, name=f"mcp-{self.server_id}", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.error = f"MCP 服务启动超过 {timeout} 秒"

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_connection())
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
        finally:
            self._loop.close()
            if self._stopping:
                self._stopping = False
                self._stopped = True

    async def _run_connection(self) -> None:
        self._stop_signal = asyncio.Event()
        try:
            await self._connect()
            self.error = ""
            self._ready.set()
            await self._stop_signal.wait()
        finally:
            if self._stack:
                await self._stack.aclose()

    async def _connect(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stack = AsyncExitStack()
        parameters = StdioServerParameters(
            command=self.command,
            args=self.args,
            env={**os.environ, **self.env},
        )
        read_stream, write_stream = await self._stack.enter_async_context(stdio_client(parameters))
        self._session = await self._stack.enter_async_context(ClientSession(read_stream, write_stream))
        await self._session.initialize()
        result = await self._session.list_tools()
        self.tools = [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema or {},
            }
            for tool in result.tools
        ]

    def call(self, tool_name: str, arguments: dict[str, Any], timeout: int = 620) -> tuple[bool, str]:
        with self._call_lock:
            if self.error:
                return False, self.error
            if not self._session or not self._loop:
                return False, "MCP 服务尚未连接"
            future = asyncio.run_coroutine_threadsafe(
                self._session.call_tool(
                    tool_name,
                    arguments,
                    read_timeout_seconds=timeout,
                ),
                self._loop,
            )
            try:
                result = future.result(timeout=timeout)
            except FutureTimeoutError:
                future.cancel()
                return False, f"MCP 工具调用超过 {timeout} 秒"
            except Exception as exc:
                future.cancel()
                return False, f"{type(exc).__name__}: {exc}"
            blocks = []
            for item in result.content:
                text = getattr(item, "text", None)
                if text is not None:
                    blocks.append(text)
                    continue
                try:
                    blocks.append(item.model_dump_json())
                except Exception:
                    blocks.append(str(item))
            return not bool(result.isError), "\n".join(blocks)

    def stop(self) -> None:
        self._stopping = True
        if self._loop and self._loop.is_running() and self._stop_signal:
            self._loop.call_soon_threadsafe(self._stop_signal.set)
        if self._thread:
            self._thread.join(timeout=5)
        if self._thread and self._thread.is_alive():
            return
        self._thread = None
        self._session = None
        self._stack = None
        self._loop = None
        self._stop_signal = None
        self._ready = threading.Event()
        self.error = ""
        self._stopping = False
        self._stopped = True

    def state(self) -> dict[str, Any]:
        if self._stopping:
            status = "stopping"
        elif self.error:
            status = "error"
        elif self._session:
            status = "connected"
        elif self._stopped:
            status = "stopped"
        else:
            status = "idle"
        return {
            "id": self.server_id,
            "connected": status == "connected",
            "status": status,
            "error": self.error,
            "tools": self.tools,
        }


class MCPRegistry:
    def __init__(self, configs: list[dict[str, Any]]):
        self.connections: dict[str, MCPServerConnection] = {}
        self._session_count = 0
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        for config in configs:
            if not config.get("enabled", True):
                continue
            server_id = str(config.get("id") or "").strip()
            command = str(config.get("command") or "").strip()
            if not server_id or not command:
                continue
            self.connections[server_id] = self._connection(config)

    @staticmethod
    def _connection(config: dict[str, Any]) -> MCPServerConnection:
        return MCPServerConnection(
            str(config["id"]),
            str(config["command"]),
            [str(item) for item in config.get("args", [])],
            {str(key): str(value) for key, value in (config.get("env") or {}).items()},
        )

    def upsert(self, config: dict[str, Any]) -> dict[str, Any]:
        """Add or replace one server without interrupting unrelated MCP sessions."""
        server_id = str(config.get("id") or "").strip()
        command = str(config.get("command") or "").strip()
        if not server_id or not command:
            raise ValueError("MCP server id and command are required")
        normalized = {
            "id": server_id,
            "command": command,
            "args": [str(item) for item in config.get("args", [])],
            "env": {str(key): str(value) for key, value in (config.get("env") or {}).items()},
            "enabled": bool(config.get("enabled", True)),
        }
        with self._lifecycle_lock:
            with self._lock:
                previous = self.connections.pop(server_id, None)
        if previous:
            previous.stop()
        if not normalized["enabled"]:
            return {"id": server_id, "status": "disabled", "connected": False, "tools": [], "error": ""}
        connection = self._connection(normalized)
        with self._lifecycle_lock:
            with self._lock:
                self.connections[server_id] = connection
                should_start = self._session_count > 0
        if should_start:
            connection.start()
        return connection.state()

    def start(self) -> None:
        with self._lifecycle_lock:
            with self._lock:
                connections = list(self.connections.values())
        for connection in connections:
            connection.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            with self._lock:
                self._session_count = 0
                connections = list(self.connections.values())
        for connection in connections:
            connection.stop()

    def acquire(self) -> None:
        with self._lifecycle_lock:
            with self._lock:
                should_start = self._session_count == 0
                self._session_count += 1
                connections = list(self.connections.values()) if should_start else []
        for connection in connections:
            connection.start()

    def release(self) -> None:
        with self._lifecycle_lock:
            with self._lock:
                if self._session_count == 0:
                    return
                self._session_count -= 1
                connections = list(self.connections.values()) if self._session_count == 0 else []
        for connection in connections:
            connection.stop()

    def call(self, server_id: str, tool_name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        with self._lock:
            connection = self.connections.get(server_id)
        if not connection:
            return False, f"未注册 MCP 服务：{server_id}"
        return connection.call(tool_name, arguments)

    def states(self) -> list[dict[str, Any]]:
        with self._lock:
            connections = list(self.connections.values())
        return [connection.state() for connection in connections]

    def tool_guide(self) -> str:
        rows = []
        with self._lock:
            connections = list(self.connections.items())
        for server_id, connection in connections:
            if connection.error:
                rows.append(f"- {server_id}: 不可用（{connection.error}）")
                continue
            for tool in connection.tools:
                schema = tool.get("input_schema") or {}
                rows.append(
                    f"- {server_id}.{tool['name']}: {tool['description'][:300]} "
                    f"参数={schema}"
                )
        return "\n".join(rows)
