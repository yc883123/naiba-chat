# -*- coding: utf-8 -*-
"""护栏：「新会话开始」边界（同一会话窗口内清空模型上下文）。

语义：在会话末尾落一条 role=session 的标记行（不删任何消息）——
1. `build_model_history` 遇到该标记即清空此前历史（多个标记取最后一个）；
2. 聊天记录原样保留，前端在该位置渲染分隔条，可撤销；
3. 标记只影响模型上下文，不影响标题/分支/搜索等按 role 取数的既有逻辑。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from naiba.core.history import build_model_history  # noqa: E402
from naiba.core.messages import MESSAGE_METADATA_KEYS, MetadataKeys  # noqa: E402
from naiba.storage.store import ChatStorage  # noqa: E402


def _marker(at: int = 1789000000000, **extra) -> dict:
    return {"session_start": {"at": at, "source": "manual", **extra}}


class SessionStartContractTests(unittest.TestCase):
    def test_metadata_key_registered(self):
        self.assertEqual(MetadataKeys.SESSION_START, "session_start")
        self.assertIn("session_start", MESSAGE_METADATA_KEYS, "新增 metadata 键必须登记契约表")
        from naiba.core.contracts import MetadataKeys as Reexported

        self.assertEqual(Reexported.SESSION_START, "session_start", "契约 re-export 不能漏")


class SessionStartHistoryTests(unittest.TestCase):
    """重放侧：标记之前的消息不进模型请求，标记本身也不是消息。"""

    def test_history_cut_at_marker(self):
        history = build_model_history([
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": "旧回答"},
            {"role": "session", "content": "", "metadata": _marker()},
            {"role": "user", "content": "新问题"},
            {"role": "assistant", "content": "新回答"},
        ])
        self.assertEqual(
            [(m["role"], m["content"]) for m in history],
            [("user", "新问题"), ("assistant", "新回答")],
            "标记之前的消息必须被清空，标记本身不得进入历史",
        )

    def test_last_marker_wins(self):
        history = build_model_history([
            {"role": "user", "content": "第一段"},
            {"role": "session", "content": "", "metadata": _marker(at=1)},
            {"role": "user", "content": "第二段"},
            {"role": "session", "content": "", "metadata": _marker(at=2)},
            {"role": "user", "content": "第三段"},
        ])
        self.assertEqual([m["content"] for m in history], ["第三段"], "多个标记取最后一个")

    def test_marker_after_everything_yields_empty_history(self):
        history = build_model_history([
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": "旧回答"},
            {"role": "session", "content": "", "metadata": _marker()},
        ])
        self.assertEqual(history, [], "末尾标记后没有新消息 → 上下文为空")

    def test_marker_without_metadata_is_ignored_as_message(self):
        """role=session 但不是标记（异常数据）也不得混进模型历史。"""
        history = build_model_history([
            {"role": "user", "content": "问题"},
            {"role": "session", "content": "无标记"},
        ])
        self.assertEqual([m["content"] for m in history], ["问题"])

    def test_trace_replay_still_works_after_marker(self):
        """边界之后的 assistant 消息照旧按 trace 重放（上下文重建口径不变）。"""
        history = build_model_history([
            {"role": "user", "content": "旧"},
            {"role": "session", "content": "", "metadata": _marker()},
            {"role": "user", "content": "新"},
            {"role": "assistant", "content": "答复", "metadata": {
                "trace": [
                    {"role": "assistant", "content": "", "tool_calls": [
                        {"id": "c1", "name": "pwsh", "arguments": {"command": "dir"}},
                    ]},
                    {"role": "tool", "tool_call_id": "c1", "name": "pwsh", "content": "ok"},
                    {"role": "assistant", "content": "答复"},
                ],
            }},
        ])
        self.assertEqual(history[0]["content"], "新")
        self.assertEqual(history[1]["tool_calls"][0]["id"], "c1")
        self.assertEqual(history[2]["role"], "tool")
        self.assertEqual(history[3]["content"], "答复")


class SessionStartStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = ChatStorage(Path(self.tmp.name) / "chat.db")
        self.conversation = self.storage.create_conversation()
        self.conversation_id = self.conversation["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_marker_keeps_messages_and_records_metadata(self):
        self.storage.add_message(self.conversation_id, "user", "旧问题")
        self.storage.add_message(self.conversation_id, "assistant", "旧回答")
        marker = self.storage.add_session_start(self.conversation_id, note="交接完成")
        self.assertEqual(marker["role"], "session")
        self.assertEqual(marker["content"], "")
        info = marker["metadata"]["session_start"]
        self.assertEqual(info["source"], "manual")
        self.assertEqual(info["note"], "交接完成")
        self.assertTrue(info["at"])
        messages = self.storage.get_conversation(self.conversation_id)["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "session"], "旧消息一条不删")
        # 落盘后重放：只有标记之后的（这里为空）
        self.assertEqual(build_model_history(messages), [])

    def test_marker_bumps_conversation_updated_at(self):
        before = self.storage.get_conversation(self.conversation_id)["updated_at"]
        marker = self.storage.add_session_start(self.conversation_id)
        after = self.storage.get_conversation(self.conversation_id)["updated_at"]
        self.assertGreaterEqual(after, before, "标记要推进 updated_at，前端轮询才能感知")
        self.assertTrue(marker["id"])

    def test_delete_marker_restores_context_and_only_deletes_session_rows(self):
        self.storage.add_message(self.conversation_id, "user", "旧问题")
        marker = self.storage.add_session_start(self.conversation_id)
        self.storage.add_message(self.conversation_id, "user", "新问题")
        self.assertTrue(self.storage.delete_session_start(marker["id"]))
        messages = self.storage.get_conversation(self.conversation_id)["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "user"], "只删标记行")
        self.assertEqual([m["content"] for m in build_model_history(messages)], ["旧问题", "新问题"])
        # 普通消息（非 session）不可经此删除
        plain = messages[0]
        self.assertFalse(self.storage.delete_session_start(plain["id"]))
        self.assertEqual(len(self.storage.get_conversation(self.conversation_id)["messages"]), 2)
        self.assertFalse(self.storage.delete_session_start(marker["id"]), "重复撤销返回 False")

    def test_marker_does_not_steal_conversation_title(self):
        self.storage.add_message(self.conversation_id, "user", "真正的第一条")
        self.storage.add_session_start(self.conversation_id)
        self.storage.add_message(self.conversation_id, "user", "第二条")
        self.assertEqual(self.storage.get_conversation(self.conversation_id)["title"], "真正的第一条")

    def test_marker_is_copied_by_branch(self):
        """分支复制 metadata → 分支会话同样从最近的边界开始重算上下文。"""
        self.storage.add_message(self.conversation_id, "user", "旧问题")
        self.storage.add_session_start(self.conversation_id)
        branch_point = self.storage.add_message(self.conversation_id, "user", "新问题")
        branch = self.storage.branch_conversation(self.conversation_id, branch_point["id"])
        messages = self.storage.get_conversation(branch["conversation"]["id"])["messages"]
        self.assertIn("session", [m["role"] for m in messages], "边界标记必须随分支复制")
        self.assertEqual([m["content"] for m in build_model_history(messages)], [], "分支同样从边界之后重算")


class SessionStartFrontendTests(unittest.TestCase):
    """前端契约：入口按钮、分隔条渲染、撤销绑定、接口路径。"""

    def _read(self, name: str) -> str:
        return (ROOT / "public" / "js" / name).read_text(encoding="utf-8")

    def test_composer_button_and_style_exist(self):
        html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('id="newSessionButton"'), 1, "入口按钮必须存在且 id 唯一")
        css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
        for rule in (".message-row.session-divider", ".session-divider-bar",
                     ".session-divider-label", ".session-divider-cancel"):
            with self.subTest(rule=rule):
                self.assertIn(rule, css)

    def test_message_element_renders_session_divider(self):
        js = self._read("04-messages.js")
        self.assertIn("export function sessionDividerElement(message)", js)
        body = js[js.index("export function messageElement("):]
        body = body[: body.index("\n}", body.index("const row = document.createElement"))]
        self.assertIn("message.role === 'session'", body, "session 行必须走分隔条渲染")
        self.assertIn("sessionDividerElement(message)", body)

    def test_actions_call_api_and_refresh(self):
        js = self._read("04-messages.js")
        for snippet in (
            "export async function startNewSession()",
            "export async function cancelSessionStart(messageId)",
            "/session_start`",
            "/api/session_start/",
            "await syncCurrentConversation()",
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, js)
        # 运行中不允许重置（与「分支」同口径）
        start = js[js.index("export async function startNewSession()"):]
        start = start[: start.index("\n}")]
        self.assertIn("state.abortController", start)
        binds = self._read("15-bind-events.js")
        self.assertIn("$('#newSessionButton')?.addEventListener('click'", binds)
        self.assertIn("data-cancel-session-start", binds)

    def test_backend_routes_match_frontend_paths(self):
        http = (ROOT / "naiba" / "http.py").read_text(encoding="utf-8")
        self.assertIn('path.endswith("/session_start")', http, "POST 路由")
        self.assertIn('path.startswith("/api/session_start/")', http, "DELETE 路由")


if __name__ == "__main__":
    unittest.main()
