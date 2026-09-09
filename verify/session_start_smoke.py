# -*- coding: utf-8 -*-
"""冒烟：「新会话开始」边界（手动重置上下文）。

编排（全自动，跑完还原现场）：
1. config.json 的 data_dir 临时指向 verify/session_start_tmp/（独立库）；
2. 播种一个 4 条消息的会话（两轮问答）；
3. 起 8798 源码 server → 跑 session_start_smoke.cjs；
4. 收尾：kill server、还原 config.json、删临时目录。

用法：.venv\\Scripts\\python.exe verify\\session_start_smoke.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PORT = 8798
BASE = f"http://127.0.0.1:{PORT}"
DATA_DIR = ROOT / "verify" / "session_start_tmp"


def wait_health(deadline: float = 60.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - 启动期连接失败是预期分支
            time.sleep(0.5)
    return False


def seed() -> None:
    from naiba.storage.store import ChatStorage

    storage = ChatStorage(DATA_DIR / "chat.db")
    conversation = storage.create_conversation("新会话边界冒烟")
    # 首条 user 消息会自动成为会话标题（add_message 的既有行为），所以让它带上冒烟标记词，
    # 侧栏 `:has-text("新会话边界冒烟")` 才选得中。
    storage.add_message(conversation["id"], "user", "新会话边界冒烟：第一问，请介绍这个项目")
    storage.add_message(conversation["id"], "assistant", "第一答：这是冒烟用的历史消息。")
    storage.add_message(conversation["id"], "user", "第二问：继续")
    storage.add_message(conversation["id"], "assistant", "第二答：继续的回复。")


def main() -> int:
    config_path = ROOT / "config.json"
    original_config = config_path.read_bytes()
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    config = json.loads(original_config.decode("utf-8"))
    config.update({"host": "127.0.0.1", "port": PORT, "data_dir": str(DATA_DIR)})
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    code = 1
    server: subprocess.Popen | None = None
    log = (ROOT / "verify" / "session_start_smoke_server.log").open("w", encoding="utf-8")
    try:
        seed()
        env = dict(os.environ)
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        server = subprocess.Popen(  # noqa: S603 - 固定 argv
            [sys.executable, "server.py", "--host", "127.0.0.1", "--port", str(PORT)],
            cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
        )
        if not wait_health():
            print(f"server 未就绪（日志见 {log.name}）")
            return 1
        node_env = dict(os.environ)
        node_env["NODE_PATH"] = str(Path.home() / "node_modules")
        node_env["NAIBA_SMOKE_BASE"] = BASE
        node = subprocess.run(  # noqa: S603 - 固定 argv
            ["node", str(ROOT / "verify" / "session_start_smoke.cjs")],
            cwd=str(ROOT), env=node_env, check=False,
        )
        code = node.returncode
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)
        log.close()
        config_path.write_bytes(original_config)
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        print(f"已还原 config.json 并清理 {DATA_DIR.name}"
              f"；server 已退出（exit={server.returncode if server else 'n/a'}）")
    return code


if __name__ == "__main__":
    sys.exit(main())
