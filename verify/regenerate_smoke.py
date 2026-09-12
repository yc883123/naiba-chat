# -*- coding: utf-8 -*-
"""冒烟：「重新生成」（AI 回复）+「编辑」（用户消息）回归的自编排入口。

用**独立数据目录**（`verify/regenerate_tmp/`）与独立端口起源码 server，不碰开发配置与开发库：
播种 1 个在线 API + 1 个「已绑定模型」的会话（三轮问答），浏览器侧拦 `/api/providers/models`
注入假目录让「发送前置校验」成立，并拦 `POST /api/chat` 只记录请求体、回一段最小 NDJSON。

关键点：**截断走真实后端**（`POST /api/messages/edit`），因此「后端剩余消息 == 预期前缀」
是真实断言，不是模拟。

三条「引用链」都要在重发后原样活着（这是本冒烟的重点）：
  · `/ref` 技能引用 —— 留在 `display_message` 原文里，`message` 里必须已剥离；
  · `@` 工作区引用 —— 原文里逐字保留（后端再按会话工作区解析成绝对路径）；
  · 图片等附件 —— 由 `/api/messages/edit` 回传，重发时必须带回 `path`（以及 `thumb_path`）。

用法：.venv\\Scripts\\python.exe verify\\regenerate_smoke.py
"""
from __future__ import annotations

import base64
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
PORT = 8796
BASE = f"http://127.0.0.1:{PORT}"
DATA_DIR = ROOT / "verify" / "regenerate_tmp"
PROVIDER = {"id": "smoke-a", "name": "冒烟 API A", "model": "smoke-default-a"}
BOUND_MODEL = "smoke-pro"
TITLE = "重新生成冒烟"
Q1 = "重新生成冒烟：第一问"
A1 = "第一答：冒烟历史。"
# 第二问是「富消息」：/ref + @ 工作区引用 + 一张真实图片附件，用来钉住三条引用链。
Q2_PLAIN = "第二问：继续"
A2 = "第二答：继续的回复。"
AT_REF = "人物资产/主角.png"
IMG_NAME = "smoke-att.png"
IMG_THUMB_NAME = "smoke-thumb.png"
# 第三问只带 /ref，用来当「最后一轮」（验证不弹确认框）。
Q3_PLAIN = "第三问：收尾"
A3 = "第三答：收尾的回复。"
EDITED = "第一问：改过之后的问题"
EDITED_RICH = "第二问：改过之后的问题（带引用重发）"

# 1x1 透明 PNG：足够让 `mediaKind` 判成图片、让浏览器真的解码成功（避免 404/破图
# 在控制台留下 console.error，污染「零页面错误」那条断言）。
IMAGE_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _first_skill_ref() -> str:
    """取一个已安装 Skill 的引用名（目录名 = SKILL.md 的 name，前端 skillByRef 两者都认）。

    用它把「带引用的提问」种进会话，好在浏览器侧断言：重发时**原文保留 /ref**、
    而发给模型的 message 已剥离 /ref（引用不能在路上丢，也不能被当成正文塞给模型）。
    """
    skills_dir = ROOT / "skills"
    if not skills_dir.is_dir():
        return ""
    names = sorted(item.name for item in skills_dir.iterdir() if item.is_dir())
    return f"/{names[0]}" if names else ""


SKILL_REF = _first_skill_ref()

# 用户原样（含 /ref 与 @）——「编辑」回填与「重发」的 display_message 都必须是它。
Q2_DISPLAY = f"{Q2_PLAIN} {SKILL_REF} @{AT_REF}".strip()
# 模型可见版：/ref 已剥离、@ 原样保留（后端 resolve_file_references 再解析）。
Q2_CONTENT = f"{Q2_PLAIN} @{AT_REF}".strip()
Q3_DISPLAY = f"{Q3_PLAIN} {SKILL_REF}".strip()
Q3_CONTENT = Q3_PLAIN


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
    NaibaChatApp.api_upsert_model_profile(app, {
        "id": PROVIDER["id"],
        "kind": "online",
        "name": PROVIDER["name"],
        "base_url": "https://example.invalid/v1",   # 目录/模型请求都在浏览器侧被拦截，永不真连
        "api_key": "sk-smoke",
        "request_format": "openai_chat",
        "model": PROVIDER["model"],
    })
    # 图片附件要真的能取到：`/api/file` 的白名单含 data_dir，所以放进临时 data 目录的 uploads。
    uploads = DATA_DIR / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    image_path = uploads / IMG_NAME
    thumb_path = uploads / IMG_THUMB_NAME
    image_path.write_bytes(IMAGE_BYTES)
    thumb_path.write_bytes(IMAGE_BYTES)
    # 只有一个会话：启动时会自动打开它（loadConversations 打开 conversations[0]），
    # 冒烟因此不必驱动侧栏（虚拟列表 + 默认折叠，点开条目反而脆弱）。
    conversation = storage.create_conversation(
        TITLE,
        model_key=f"online:{PROVIDER['id']}",
        model_name=BOUND_MODEL,
    )
    # 首条 user 消息会自动成为会话标题（add_message 的既有行为），所以标题在 create 时先定。
    storage.add_message(conversation["id"], "user", Q1)
    storage.add_message(conversation["id"], "assistant", A1)
    # 富消息：display_content 保留用户原样（/ref + @），content 是模型可见版，另带一张图片附件。
    storage.add_message(conversation["id"], "user", Q2_CONTENT, {
        "display_content": Q2_DISPLAY,
        "attachments": [{
            "name": IMG_NAME,
            "path": str(image_path),
            "size": len(IMAGE_BYTES),
            "thumb_path": str(thumb_path),
        }],
    })
    storage.add_message(conversation["id"], "assistant", A2)
    storage.add_message(conversation["id"], "user", Q3_CONTENT, {"display_content": Q3_DISPLAY})
    storage.add_message(conversation["id"], "assistant", A3)


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
    log = (ROOT / "verify" / "regenerate_smoke_server.log").open("w", encoding="utf-8")
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
        node_env["NAIBA_SMOKE_BOUND_MODEL"] = BOUND_MODEL
        node_env["NAIBA_SMOKE_Q1"] = Q1
        node_env["NAIBA_SMOKE_A1"] = A1
        node_env["NAIBA_SMOKE_A2"] = A2
        node_env["NAIBA_SMOKE_A3"] = A3
        node_env["NAIBA_SMOKE_Q2_PLAIN"] = Q2_PLAIN
        node_env["NAIBA_SMOKE_Q2_DISPLAY"] = Q2_DISPLAY
        node_env["NAIBA_SMOKE_Q2_CONTENT"] = Q2_CONTENT
        node_env["NAIBA_SMOKE_Q3_PLAIN"] = Q3_PLAIN
        node_env["NAIBA_SMOKE_Q3_DISPLAY"] = Q3_DISPLAY
        node_env["NAIBA_SMOKE_AT_REF"] = AT_REF
        node_env["NAIBA_SMOKE_IMG_PATH"] = str(DATA_DIR / "uploads" / IMG_NAME)
        node_env["NAIBA_SMOKE_IMG_THUMB"] = str(DATA_DIR / "uploads" / IMG_THUMB_NAME)
        node = subprocess.run(  # noqa: S603 - 固定 argv
            ["node", str(ROOT / "verify" / "regenerate_smoke.cjs")],
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
