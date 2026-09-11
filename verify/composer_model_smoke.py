"""会话模型下拉（检测目录 / 「↻ 重新检测模型」/ 不静默选首项）冒烟的自编排入口。

用**独立数据目录**（`verify/composer_model_tmp/`）与独立端口起源码 server，不碰开发配置与
开发库：播种 2 个在线 API 与 1 个「已绑定模型」的会话，浏览器侧拦 `/api/providers/models`
注入假目录，验证：

① 目录缓存判据是「有没有模型」而不是「有没有查过」——返回空列表后换个 API 再切回来必须重拉；
② 会话内「↻ 重新检测模型」能强制重拉目录，且伪选项不落库、刷新后还原原选择；
③ 主动刷新失败必须出提示（不能只藏控制台）；
④ 会话没有明确选择时下拉显示占位项，**不再静默选中目录第一项**。

用法：.venv\\Scripts\\python.exe verify\\composer_model_smoke.py
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
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # 直接跑本脚本时也能 import naiba
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
DATA_DIR = ROOT / "verify" / "composer_model_tmp"
PROVIDER_A = {"id": "smoke-a", "name": "冒烟 API A", "model": "smoke-default-a"}
PROVIDER_B = {"id": "smoke-b", "name": "冒烟 API B", "model": "smoke-default-b"}
BOUND_MODEL = "smoke-pro"


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
    from naiba.app import NaibaChatApp
    from naiba.config import ConfigStore
    from naiba.storage.store import ChatStorage

    storage = ChatStorage(DATA_DIR / "chat.db")
    config = ConfigStore(ROOT / "config.json")
    app = SimpleNamespace(config=config, storage=storage)
    for provider in (PROVIDER_A, PROVIDER_B):
        NaibaChatApp.api_upsert_model_profile(app, {
            "id": provider["id"],
            "kind": "online",
            "name": provider["name"],
            "base_url": "https://example.invalid/v1",   # 目录请求在浏览器侧被拦截，永不真连
            "api_key": "sk-smoke",
            "request_format": "openai_chat",
            "model": provider["model"],
        })
    # 只有一个会话：启动时会自动打开它（loadConversations 打开 conversations[0]），
    # 冒烟因此不必驱动侧栏（虚拟列表 + 默认折叠，点开条目反而脆弱）。
    storage.create_conversation(
        "冒烟：已绑定模型的会话",
        model_key=f"online:{PROVIDER_A['id']}",
        model_name=BOUND_MODEL,
    )


def main() -> int:
    config_path = ROOT / "config.json"
    original_config = config_path.read_bytes()
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    config = json.loads(original_config.decode("utf-8"))
    config.update({
        "host": "127.0.0.1",
        "port": PORT,
        "data_dir": str(DATA_DIR),
        "providers": [],
        "default_model_key": "",
    })
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 从改完 config.json 起，**任何异常路径都必须还原**（包括播种）——
    # 播种一抛异常就把开发配置留在临时端口 / 临时目录上。
    code = 1
    server: subprocess.Popen | None = None
    log = (ROOT / "verify" / "composer_model_smoke_server.log").open("w", encoding="utf-8")
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
        node_env["NAIBA_SMOKE_PROVIDER_A"] = f"online:{PROVIDER_A['id']}"
        node_env["NAIBA_SMOKE_PROVIDER_B"] = f"online:{PROVIDER_B['id']}"
        node_env["NAIBA_SMOKE_BOUND_MODEL"] = BOUND_MODEL
        node = subprocess.run(  # noqa: S603 - 固定 argv
            ["node", str(ROOT / "verify" / "composer_model_smoke.cjs")],
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
