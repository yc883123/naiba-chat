import json
import tempfile
import threading
import time
import traceback
from pathlib import Path

from storage import ChatStorage
from tool_registry import build_tool_registry
from job_registry import JobRegistry, JobSpec
from skill_runtime import SkillAgent, ToolExecutor, SkillCatalog, TaskCancelled

tmp = Path(tempfile.mkdtemp())


class FakeMCP:
    connections = {}

    def tool_guide(self):
        return ""


class FakeConfig:
    data = {"workspace_dir": str(tmp), "agent_max_steps": 32}


class FakeExecutor(ToolExecutor):
    def __init__(self):
        self.workspace = Path(tmp)
        self.python_executable = "python"
        self.command_timeout = 10
        self.mcp_registry = FakeMCP()
        self.mcp_register = None
        self.permission_mode = "full"
        self.pending_confirmation = {}
        self.confirmation_results = {}
        self._confirmation_lock = threading.RLock()


class FakeApp:
    def __init__(self):
        self.storage = ChatStorage(tmp / "t.db")
        self.config = FakeConfig()
        self.executor = FakeExecutor()
        self.tool_registry = build_tool_registry()
        self.tool_registry.bind_executor(self.executor)
        self.catalog = SkillCatalog([], base_dir=Path("."))
        self.jobs = JobRegistry(self)


def test_tool_registry():
    reg = build_tool_registry()
    names = reg.names()
    assert "read_file" in names and "call_mcp" in names, names
    assert "subagent" in names and "run_in_background" in names, names
    assert reg.retryable("read_file") is True
    assert reg.retryable("write_file") is False
    assert reg.side_effect("write_file") is True
    assert reg.side_effect("list_directory") is False
    print("PASS test_tool_registry: %d tools" % len(names))


def test_job_shell():
    app = FakeApp()
    conv = app.storage.create_conversation()
    cid = conv["id"]

    spec = JobSpec(
        kind="shell",
        conversation_id=cid,
        params={"command": "echo HELLO_JOB", "timeout": 30},
        label="echo test",
    )
    job_id = app.jobs.start(spec, owner=cid)
    job = app.jobs.wait(job_id, timeout=15, owner=cid)
    assert job is not None, "wait returned None"
    assert job["status"] == "completed", job
    out = app.jobs.read(job_id, owner=cid)
    assert any("HELLO_JOB" in str(e.get("line", "")) for e in out["events"]), out["events"]
    # owner mismatch
    assert app.jobs.get(job_id, owner="other") is None
    assert app.jobs.get(job_id, owner=cid) is not None
    print("PASS test_job_shell: status=%s events=%d" % (job["status"], len(out["events"])))


def test_agent_final_and_maxsteps():
    app = FakeApp()
    captured = []

    def fake_model(profile, messages, options, event=None):
        # always answer final text
        return "这是最终答复"

    agent = SkillAgent(app.catalog, app.executor, fake_model)
    resp, runs, reasonings, usage = agent.run(
        "你好", [], {}, {}, False, [], "", [],
        lambda *a, **k: None, lambda *a: None, None, 32, app.tool_registry,
    )
    assert "最终答复" in resp, resp
    assert runs == [], runs
    print("PASS test_agent_final_and_maxsteps")

    # max steps: model always asks for a disabled tool -> should halt at max steps
    def fake_loop_model(profile, messages, options, event=None):
        return json.dumps({"type": "tool", "tool": "read_file", "arguments": {"path": "x"}})

    agent2 = SkillAgent(app.catalog, app.executor, fake_loop_model)
    resp2, runs2, _, _ = agent2.run(
        "循环", [], {}, {}, False, [], "", [],
        lambda *a, **k: None, lambda *a: None, None, 5, app.tool_registry,
    )
    assert ("最大步骤" in resp2) or ("重复" in resp2), resp2
    assert len(runs2) <= 5, len(runs2)
    print("PASS test_agent_maxsteps: runs=%d msg=%s" % (len(runs2), resp2[:30]))


def test_agent_retry():
    from tool_registry import ToolSpec

    app = FakeApp()
    state = {"n": 0}
    agent_events = []

    def flaky(arguments, active_skills, run_context=None):
        state["n"] += 1
        if state["n"] < 3:
            return False, "MCP 超时，连接被重置"
        return True, "成功于第 %d 次" % state["n"]

    # 注册为可重试工具（有 ToolSpec 才会触发自动重试）
    app.tool_registry.register(ToolSpec(
        name="flaky_tool", description="", parameters={"type": "object", "properties": {}},
        side_effect=False, retryable=True, timeout=10, permission="confirm",
    ))
    app.tool_registry.register_system_handler("flaky_tool", flaky)

    step = {"first": True}

    def fake_model(profile, messages, options, event=None):
        if step["first"]:
            step["first"] = False
            return json.dumps({"type": "tool", "tool": "flaky_tool", "arguments": {}})
        return "完成"

    agent = SkillAgent(app.catalog, app.executor, fake_model)
    resp, runs, _, _ = agent.run(
        "重试测试", [], {}, {}, False, [], "", ["flaky_tool"],
        lambda p: agent_events.append(p), lambda *a: None, None, 32, app.tool_registry,
    )
    assert len(runs) == 1, runs
    assert runs[0]["success"] is True, runs
    assert any(e.get("type") == "retry" for e in agent_events), "no retry event"
    print("PASS test_agent_retry: attempts=%d retries_emitted=%d" % (
        state["n"], sum(1 for e in agent_events if e.get("type") == "retry")))


def test_job_cleaned_lookup():
    """清理后的 Job 应返回「记录已被清理」，未创建的 Job 返回「不存在」，均非含糊的“无权访问”。"""
    from subagent import job_tool_handler_factory

    app = FakeApp()
    handlers = job_tool_handler_factory(app)
    conv = app.storage.create_conversation()
    cid = conv["id"]

    spec = JobSpec(
        kind="shell",
        conversation_id=cid,
        params={"command": "echo CLEANED_LOOKUP", "timeout": 30},
        label="cleaned lookup",
    )
    job_id = app.jobs.start(spec, owner=cid)
    app.jobs.wait(job_id, timeout=15, owner=cid)

    # 跨对话（不同 owner）只读查询可用
    ok, msg = handlers["job_status"]({"job_id": job_id}, [], {"conversation_id": "other-conv"})
    assert ok and "completed" in msg, msg

    # 未创建的 Job ID：提示不存在，而非“无权访问”
    ok, msg = handlers["job_status"]({"job_id": "no-such-job"}, [], {"conversation_id": "other-conv"})
    assert not ok and "不存在" in msg and "无权访问" not in msg, msg

    # 清理后：提示“已被清理”
    assert app.storage.clear_terminal_background_tasks() >= 1
    assert app.storage.is_job_cleaned(job_id) is True
    ok, msg = handlers["job_status"]({"job_id": job_id}, [], {"conversation_id": "other-conv"})
    assert not ok and "已被清理" in msg, msg
    ok, msg = handlers["job_output"]({"job_id": job_id}, [], {"conversation_id": "other-conv"})
    assert not ok and "已被清理" in msg, msg
    print("PASS test_job_cleaned_lookup")


if __name__ == "__main__":
    try:
        test_tool_registry()
        test_job_shell()
        test_agent_final_and_maxsteps()
        test_agent_retry()
        test_job_cleaned_lookup()
        print("\nALL SMOKE TESTS PASSED")
    except Exception:
        traceback.print_exc()
        raise
