"""工具分类改版冒烟的自编排入口（随仓库发布的可复用资产）。

起点源 server（独立端口 8793，避免与冻结版/其它冒烟串库）→ 跑 tool_groups_smoke.cjs →
无论成败都还原 config.json 的 host/port 并清掉本轮产生的 data/server.json。

用法：.venv\\Scripts\\python.exe verify\\tool_groups_smoke.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8793
BASE = f"http://127.0.0.1:{PORT}"


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


def main() -> int:
    config_path = ROOT / "config.json"
    original_config = config_path.read_bytes()
    status_path = ROOT / "data" / "server.json"
    status_existed = status_path.exists()
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    log = (ROOT / "verify" / "tool_groups_smoke_server.log").open("w", encoding="utf-8")
    server = subprocess.Popen(  # noqa: S603 - 固定 argv
        [sys.executable, "server.py", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env,
    )
    code = 1
    try:
        if not wait_health():
            print(f"server 未就绪（日志见 {log.name}）")
            return 1
        node_env = dict(os.environ)
        node_env["NODE_PATH"] = str(Path.home() / "node_modules")
        node_env["NAIBA_SMOKE_BASE"] = BASE
        node = subprocess.run(  # noqa: S603 - 固定 argv
            ["node", str(ROOT / "verify" / "tool_groups_smoke.cjs")],
            cwd=str(ROOT), env=node_env, check=False,
        )
        code = node.returncode
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)
        log.close()
        # 还原现场：config.json（main_entry 会把 host/port 写回）与 data/server.json。
        config_path.write_bytes(original_config)
        if not status_existed and status_path.exists():
            status_path.unlink()
        print(f"已还原 config.json；server 已退出（exit={server.returncode}）")
    return code


if __name__ == "__main__":
    sys.exit(main())
