# 工具系统增强 · 阶段交接文档

> **性质**：阶段性交接文档（当前会话窗口将结束，供新会话窗口接手）。
> 完成后随使命删除（与 docs/dev 既有约定一致）；期间以 `项目维护说明（修改代码前必读）.md` 为权威现状说明。

---

## 一、当前状态（接手即此）

- **分支**：`master`，HEAD = **`21162ab`**（视觉调用统一为模型驱动：自动路由移除）；
- **验证基线**：`python -m unittest discover -s tests` = **153 用例 OK**；`tests.golden_replay` = 3 基线 OK；`.tmptest/scan_undef_all.py` = 0 候选；工具 def 总数 = **32**；
- **冻结版**：`dist\naiba-chat.exe`（含 701debd 全部修复；本阶段产物未重新编译，发布时按 §五 重编）；
- **分支说明**：`test1` 为诊断实验分支（仍保留文件日志 + `NAIBA_NO_REASONING_PASSBACK` 开关，未并入 master）；master 工作树除 `build_info.json` 外干净（符合规则）。

## 二、本阶段目标与已完成（master 提交链）

| 提交 | 内容 |
|---|---|
| 54061b4 | 删除 `call_mcp` 网关 + 退役名引导（`RETIRED_TOOL_GUIDE/MAP`；配置迁移清洗旧名） |
| 04a838c | 视觉单入口重构：8→2（`vision_analyze` 同名会话化分流 + `vision_image_ops` PIL 三合一）；删除模型能力映射族与 3 处 `startswith("vision_")` 过滤；RunContext 新契约键 `model_has_vision`/`tool_defs`（20 键） |
| 9e75ebb | 破坏性变更落点（release_notes/update/README/维护说明） |
| 4562198 | `tool_rejection` 精确匹配（裸词 `tools`/`unsupported` 防误触发） |
| 586203f | Revert b0f8cdf（错误形态回退——历史，勿再回踩） |
| 6ad8ee9 | DeepSeek 思考回传（首版草案）+ 无工具兜底（400 时去 tools 重试一次） |
| 25193d7 | 回传顺序修正（function_call 配对）+ 图片记忆作用域收敛（`_path_cache` 跨会话残留，用后即焚） |
| 701debd | **最终合法形态**：`{"type":"reasoning","content":[{"type":"reasoning_text","text":"…"}]}` 块数组；顺序 `reasoning → assistant 消息 → function_call → function_call_output`；实测通过 |
| `<21162ab>` | **视觉调用统一为模型驱动**：移除「自动路由」选项与后台识图注入（配置默认/迁移清洗/`prepare_history` 纯占位）；删除路由缓存与图片记忆死代码；`vision_start/vision_done/vision_error` 事件契约与前端处理一并移除（视觉工具与其它工具同构呈现）；图片处理策略提示改写；前端设置页删除自动路由行 |

**其余成果**（更早阶段，已在 master）：工具单一定义架构（`ToolSpec`/`providers/*`/单插槽分发/引擎瘦身）、确认链修复（policy 工作区/NEED_CONFIRM 冒号/前端委托）、golden 基线体系。

## 三、关键设计终态（已实测确证，接手勿改）

1. **DeepSeek reasoning 回传**（`naiba/llm/protocols.py::_responses_input`）：
   - 形态必须是 `content` 块数组（理由见维护说明 §九 12 条：`reasoning_text` 字段服务端不认、明文 content 被 serde 拒）；
   - 顺序：reasoning item → assistant 消息（有值时）→ function_call → function_call_output（配对相邻，中间插任何 item 会 "No tool output found"）；
   - 无 reasoning 时不产 item。
2. **视觉调用统一由模型驱动（已实现的终态，接手勿改）**：
   - 「自动路由」选项已移除：纯文本模型上传图片时，`prepare_history` 只做安全占位改写
     （路径引用 + 调用提示），**不再后台调用视觉后端**；何时看图、问什么由模型按需
     调用 `vision_analyze` 工具决定，结果与其它工具一样以工具块呈现。
   - 多模态模型仍直接收到原图（不做占位改写），无需调用视觉工具。
   - 路由缓存 / 图片记忆（`_route_cache*` / `_path_cache*` / `_apply_image_memory`）已随
     自动路由一并删除，勿复活；`vision_start/vision_done/vision_error` 事件已从契约与前端移除。
   - 配置残留键 `vision.auto_route` 由 ConfigStore 启动时清洗（`config.py`）。
3. **视觉模型差异是服务端特性**：DeepSeek 只对 `deepseek-v4-flash`/`v4-pro` 校验 thinking 回传，`vision-exp` 不校验（诊断证实两模型客户端请求完全同构）。**无需为 vision-exp 做适配**。
4. **诊断纪律**：windowed exe 无控制台、应用无 logging handler → **诊断写文件**（test1 分支的 `_write_model_debug` 模式可复用；`AllocConsole` 方案实测无效）。

## 四、未完成 / 下一步（供接手选择）

1. **清理「影子态」死代码**：`naiba/run/manager.py` 顶部仍保留一份被 `chat.py::ConversationRunMixin`
   遮蔽的旧版 `ConversationRunManager`（旧 `submit_chat/_run_chat` 全文副本，末尾已重绑定回新类），
   无法被执行，但内容停留在「自动路由时代」——删除该旧类（第 43–1320 行区间）即完成
   run/manager 与 chat 拆分遗留；删除前先跑 `scan_undef_all` + 全量 unittest 确认无引用。
2. **剩余优化批次**（用户此前已认可方案、尚未实施）：
   - Harness 别名 5 组（read/write/edit/glob/grep）从模型可见集隐藏（执行兼容保留）；
   - 描述/schema 瘦身：recall_history（删 200 字协作规则）、comfyui_batch（删"改文件再引用"指令）、run_in_background、job_status、技能三件套、pwsh（删"Harness"内部术语）、http_request method enum、三个"找文件"边界句；
   - 实现前按风险分级：纯文本改动=低风险可直接做；工具可见集变化=行为变更（需用户确认 + 更新守门/文档 + 发布文件）。
3. **发布清单执行**（发布时）：`naiba-chat-update.json` 的 `commit`/`sha256` 由 workflow 覆盖；`README.md` 能力列表按需微调；冻结版编译（见 §五）。
4. 浏览器冒烟（Playwright）需用户本机 `npm install playwright` + Edge 权限，本 DSH 沙箱被拦（已知）。

## 五、验证与复测命令

```powershell
# 环境（本 DSH 沙箱必须；正常本机仅需 venv）
$env:TEMP = "D:\naiba-chat\.tmptest"; $env:TMP = "D:\naiba-chat\.tmptest"; $env:PYTHONPATH = "D:\naiba-chat\.tmptest"

# 全量测试
& "D:\naiba-chat\.venv\Scripts\python.exe" -m unittest discover -s tests
& "D:\naiba-chat\.venv\Scripts\python.exe" -m unittest tests.golden_replay
& "D:\naiba-chat\.venv\Scripts\python.exe" .tmptest\scan_undef_all.py

# 五点冒烟（先确认 8765 无旧实例——本会话曾踩过"打到用户旧 server 导致假阳性"的坑）
# 编译（先 kill 全部 naiba-chat 进程）
$env:NAIBA_BUILD_VERSION = "2.0.0-beta"; & "D:\naiba-chat\.venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean naiba-chat.spec
```

**用户实测要点**：文本模型（deepseek-v4-flash，思考模式）+ 工具调用（含 vision_analyze 识图）全流程应无 400；文本模型传图后应看到「路径占位 + 模型主动调用 vision_analyze（工具块呈现）」，不再出现自动识图 status。

## 六、本次触碰文件（master 增量）

- 后端（视重构/视觉/协议/配置）：`naiba/{vision/runtime,run/chat,run/stream,core/contracts,config}.py`
- 前端：`public/{index.html,styles.css,js/09-settings.js,js/11-run-stream.js,js/12-chat-input.js}`
- 测试：`tests/{test_config_migration,test_vision_prepare_history}.py`（后者为新增守门）
- 发布/文档：`release_notes.json`、`README.md`、`docs/manual/README.md`、`项目维护说明（修改代码前必读）.md`、`docs/dev/tools-enhancement-handoff.md`（本文档）、`版本更新说明.md`（本地，gitignored）

## 七、交接结语

工具系统统一架构、增强与视觉驱动化已全部落地并通过验证；当前没有任何已知未修 bug。剩余工作均为增量优化、影子类死代码清理与发布动作。接手第一步：`git status` 确认干净 + 跑 §五 全量测试（30 秒）确认基线。
