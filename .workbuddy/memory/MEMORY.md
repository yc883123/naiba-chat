# Naiba Chat 项目长期记忆

## 本机运行事实
- 服务端口 `http://127.0.0.1:8765`，host `0.0.0.0`，源码模式常驻运行（pywebview/server.py）。
- 本机访问（127.0.0.1）免口令；局域网/手机访问需 `config.json` 里的 `access_token`。
- 工作区默认 `D:\海螺H3提示词工程\素材4`；skills 目录 `D:\naiba-chat\skills` 与 `D:\skills`。

## 前端 UI 截图方案（踩坑结论）
`agent-browser` CLI 在此环境下不可用：`open` 会挂起不返回（SPA 持续轮询导致 networkidle 永不触发），二次调用报 `Chrome exited early (exit code 3)`。

**可行方案**：headless Chrome + CDP + Node 脚本。
1. 启动：`chrome.exe --headless=new --no-sandbox --disable-gpu --remote-debugging-port=9222 --user-data-dir=<tmp> about:blank`
   - Chrome 路径：`C:/Users/admin/.agent-browser/browsers/chrome-152.0.7977.75/chrome.exe`
2. 用 Node 22 内置 `fetch` + `WebSocket` 走 CDP：`Target.attachToTarget` → `Page.enable` / `Runtime.enable` → `Emulation.setDeviceMetricsOverride` → `Page.navigate` → `Runtime.evaluate`（点击/切 tab）→ `Page.captureScreenshot`。
3. 设置面板切 tab 直接 JS：`document.querySelectorAll('[data-settings-panel]').forEach(s => s.hidden = s.dataset.settingsPanel !== name)`。
4. dialog 打开方式：`#openSettings` → settingsDialog、`#openSkills` → skillsDialog、`#openTasks` → tasksDialog、`#openSkillImport` → skillImportDialog（都在 app.js bindEvents 里 `.showModal()`）。
5. 侧边栏会话行是**虚拟化渲染**，行内 ⚙ 按钮 class 为 `.conversation-settings`（hover 才显形，需置 opacity=1 再 click）。
6. Bash 工具默认沙箱会拦 Chrome，需 `dangerouslyDisableSandbox: true`；单张图可用 `chrome --headless --screenshot=out.png --virtual-time-budget=5000 <url>` 快截。
- 复用脚本：`scripts/screenshot.mjs`（**一次 26 张 + 1 张网页预览**，含 1.6.9 文件面板真实渲染）。
  关键技巧见 `2026-09-03.md`：
  - 演示会话法（POST + workspace_dir + chip 点击真实读文件 + 截完 DELETE）
  - 临时 demo 文件（用完即删）
  - 深度思考菜单直接 `hidden=false`（不要 click，会切换状态）
  - `python server.py` 后台启动**禁止挂管道**（`| head` 会触发 SIGPIPE 杀死服务）。

## 文档约定
- 说明书配图统一放 `docs/manual/images/`，编号 `NN-模块-页面.png`，正文按文件名引用。
- 真实产物图（`data/generated`）含有人脸，**不能直接用于产品文档**，产物卡截图脚本用 CSS 渐变占位。
- 注入式截图前必须先关所有 `dialog[open]`，否则被前一张的 dialog 遮住。
- 网页版：`docs/manual/index.html` 由 `docs/manual/build_html.py`（Python `markdown` 库，扩展 `extra`+`toc`+`tables`）渲染，含左侧 sticky 目录、图片灯箱、阅读样式。
- PDF：`chrome --headless=new --no-sandbox --no-pdf-header-footer --print-to-pdf=out.pdf file:///.../index.html`，中文 / 图片 / 表格都正确。

## 架构：server.py 已拆分（2026-09-02，develop 分支）
- server.py 从 4693 行降到 ~2406 行，Web 层职责保留（NaibaChatApp / RequestHandler / LAN 工具 / 生命周期），其余拆出：
  - `app_state.py`：路径常量 + 可变全局 DATA_DIR/STATUS_PATH/LOCK_PATH/APP（**唯一正确的访问方式**——子模块一律 `app_state.DATA_DIR`、`app_state.APP`，禁止 `from server import` 这些可变全局，会拿到重绑定前的旧值）
  - `config_store.py`：ConfigStore（配置持久化）
  - `model_media.py`：build_model_history/extract_attachments/选项组检测/能力推断
  - `image_utils.py`：图片压缩/缩略图/缓存清理/encode_image_for_model
  - `config_helpers.py`：default_config/旧数据迁移/模型常量
  - `agent_catalog.py`：内置 Agent/工具目录
- **兼容约定**：外部模块（async_tasks/plan_runtime/subagent/skill_runtime/model_runtime/vision_runtime）仍懒加载 `from server import ...`，launcher 仍 `srv.*`——这些是 server.py 顶部 import 的 re-export，不需要也不应该改成直接依赖子模块（避免循环导入）。
- 可变全局的同步点在 `NaibaChatApp.__init__` 末尾（`app_state.DATA_DIR/STATUS_PATH/LOCK_PATH/APP`），新增会重绑定这些全局的代码必须同步 app_state。
- 验证方式：`python -m py_compile *.py` + `import server` + `_smoke_harness.py` + 隔离目录真实启动（复制 *.py 和 public/ 到临时目录，写最小 config.json，构造 `NaibaChatApp` 后 `app.stop()`）。
