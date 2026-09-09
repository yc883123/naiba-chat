# -*- coding: utf-8 -*-
"""冒烟：消息列表懒加载（默认渲染最近 N 轮，向上滚动预渲染）。

编排（全自动，跑完还原现场）：
1. config.json 的 data_dir 临时指向 verify/lazy_messages_tmp/（独立库）；
2. 播种 30 轮问答（60 条消息），其中第 5 轮 AI 回复带「新会话」分割线标记；
3. 起 8796 源码 server → 跑 lazy_messages_smoke.cjs；
4. 收尾：kill server、还原 config.json、删临时目录。

用法：.venv\\Scripts\\python.exe verify\\lazy_messages_smoke.py
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
PORT = 8796
BASE = f"http://127.0.0.1:{PORT}"
DATA_DIR = ROOT / "verify" / "lazy_messages_tmp"
TURNS = 30
MARKER_TURN = 5


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
    conversation = storage.create_conversation("懒加载冒烟")
    conversation_id = conversation["id"]
    for turn in range(1, TURNS + 1):
        storage.add_message(conversation_id, "user", f"懒加载冒烟：第 {turn} 问，请简要回答。")
        metadata = {}
        if turn == MARKER_TURN:
            metadata["session_start"] = {
                "at": 1789000000000, "source": "manual", "handoff_path": "", "note": "",
            }
        storage.add_message(conversation_id, "assistant", f"第 {turn} 答：这是第 {turn} 轮的回答内容。", metadata)


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
    log = (ROOT / "verify" / "lazy_messages_smoke_server.log").open("w", encoding="utf-8")
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
        node_env["NAIBA_LAZY_TURNS"] = str(TURNS)
        node_env["NAIBA_LAZY_MARKER_TURN"] = str(MARKER_TURN)
        node = subprocess.run(  # noqa: S603 - 固定 argv
            ["node", str(ROOT / "verify" / "lazy_messages_smoke.cjs")],
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
