# 工具系统增强 · 阶段交接文档

> **性质**：阶段性交接文档（当前会话窗口将结束，供新会话窗口接手）。
> 完成后随使命删除（与 docs/dev 既有约定一致）；期间以 `项目维护说明（修改代码前必读）.md` 为权威现状说明。

---

## 一、当前状态（接手即此）

- **分支**：`master`，HEAD = **`2fd1c24`**（工具职责收敛：glob_files 并入 list_directory）；
- **验证基线**：`python -m unittest discover -s tests` = **182 用例 OK**；`tests.golden_replay` = 3 基线 OK；`.tmptest/scan_undef_all.py` = 0 候选；工具注册 def 总数 = **31**（模型/Web 可见 `schemas()` = **26**）；
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
| 21162ab | **视觉调用统一为模型驱动**：移除「自动路由」选项与后台识图注入（配置默认/迁移清洗/`prepare_history` 纯占位）；删除路由缓存与图片记忆死代码；`vision_start/vision_done/vision_error` 事件契约与前端处理一并移除（视觉工具与其它工具同构呈现）；图片处理策略提示改写；前端设置页删除自动路由行 |
| 2cc14ea | **清理 run/manager 影子旧类**：删除被 `chat.py::ConversationRunMixin` 遮蔽的旧版 `ConversationRunManager`（旧 `submit_chat/_run_chat` 全文副本）及 15 个随之失效的导入 |
| 76b84a4 | **Harness 别名隐藏**：`read/write/edit/glob/grep` 从模型/Web 可见集（`schemas()`）隐藏，只保留查询层归一（resolve/get/execute 兼容）；`HARNESS_TOOLS` 只注入规范名；守门：别名不入 schemas/allowed_tools、resolve/get 兼容、组装态执行绑定覆盖别名 def |
| e5e2567 | **工具描述瘦身（均衡档）**：描述 ≤100 字/≤3 句、删内部术语（Harness/宿主）与跨工具编排长句式；跨工具规则迁系统提示常驻区（ComfyUI 两段合一、Job ID 纪律句法优化）；http_request method 参数改 enum；守门 `tests/test_tool_description_slim.py`（4 条）；对照文档 `docs/dev/tools-description-slim.md` |
| 6280cf1 | **工具结果对模型可见性统一（去自产字段/剥机器字段/截断标记/清死管线）**：新增 `core/tool_results.py` 单一事实源；模型上下文只含 `{tool, success, result}`（arguments/reason 不进上下文），result 按工具剥离机器字段（vision 装载 path/thumb/尺寸、artifact_report sha/绝对路径、vision_image_ops 产物路径）并统一「已截断」标记；native/兼容/历史兜底三条模型通道统一；前端（stream 事件/metadata.tool_runs）与上下文同源；删除 `log_tool_run` 写入与 `tool_runs` 表（迁移 v13 DROP）与 `ToolSpec.summarize` 死 API；守门 `test_tool_results_visibility`（7 条）；golden history_images 重录（脱敏生效） |
| 2f2c68f | **工具设计修正（7 项）**：read_file 按行读取（50 行×30000 字符双预算、截断标记含行区间与续读起点、max_lines/end_line/with_line_numbers）；pwsh/run_skill_script 非零退出码与 http_request≥400 → success=False（原恒 True）；glob/list 按名称排序+绝对路径+start_after 续枚举；list_directory required 修正；search_files ignore_case 统一生效（默认区分大小写）+超大文件跳过计数+命中总数汇总；job_output 增量游标闭环；子 Agent 结果去 usage；守门 `tests/test_tool_design_fixes.py`（14 条）；golden tool_confirm 重录 |
| 2fd1c24 | **工具职责收敛（单入口）**：glob_files 并入 list_directory（新增 pattern/files_only，递归+续枚举语义等价）；read_file 的 max_chars 对模型隐藏（执行层封顶 30000）；search_files path 可选（留空=工作区根）；RETIRED_TOOL_MAP 增 glob_files→list_directory（配置/会话固化自动归一）；内置作用域/预设/指南全部替换；守门：退役名引导、单入口等价语义 |

**其余成果**（更早阶段，已在 master）：工具单一定义架构（`ToolSpec`/`providers/*`/单插槽分发/引擎瘦身）、确认链修复（policy 工作区/NEED_CONFIRM 冒号/前端委托）、golden 基线体系。

## 三、关键设计终态（已实测确证，接手勿改）

1. **DeepSeek reasoning 回传**（`naiba/llm/protocols.py::_responses_input`）：
   - 形态必须是 `content` 块数组（理由见维护说明 §九 12 条：`reasoning_text` 字段服务端不认、明文 content 被 serde 拒）；
   - 顺序：reasoning item → assistant 消息（有值时）→ function_call → function_call_output（配对相邻，中间插任何 item 会 "No tool output found"）；
   - 无 reasoning 时不产 item。
2. **视觉调用统一由模型驱动（已实现的终态，接手勿改）**：
   - 「自动路由」选项已移除：纯文本模型上传图片时，`prepare_history` 只做安全占位改写（路径引用 + 调用提示），**不再后台调用视觉后端**；何时看图、问什么由模型按需调用 `vision_analyze` 工具决定，结果与其它工具一样以工具块呈现。
   - 多模态模型仍直接收到原图（不做占位改写），无需调用视觉工具。
   - 路由缓存 / 图片记忆（`_route_cache*` / `_path_cache*` / `_apply_image_memory`）已随自动路由一并删除，勿复活；`vision_start/vision_done/vision_error` 事件已从契约与前端移除。
   - 配置残留键 `vision.auto_route` 由 ConfigStore 启动时清洗（`config.py`）。
3. **工具结果三层语义（`core/tool_results.py` 单一事实源，接手勿改）**：
   - 原始 run（内存，宿主收尾用：附件提取/file_changes/step 图片注入/search sources）→ 模型可见 `model_visible_run`（`{tool, success, result}`，arguments/reason 是模型自产不进上下文）→ 展示 `display_tool_run`（模型可见 result + arguments/reason 仅前端核对）。
   - 机器字段剥离清单：vision_analyze/vision_read_folder 装载形态（note+图片名）、artifact_report（name+size+errors，剥 sha/绝对路径）、vision_image_ops（剥 path/heatmap）；修改必须同步「模型上下文三通道」（native `role:tool`、兼容 `<untrusted_tool_result>`、历史兜底 `_content_read_tool_outputs`）与前端事实源。
   - 截断必有「已截断：原文 N 字符」标记；`metadata.tool_runs` 存展示形态（前端历史渲染 + 兜底注入数据源，注入时再剥 arguments）。
4. **工具行为终态（read_file 双预算/失败语义/确定性枚举，接手勿改）**：
   - read_file：按行读取，50 行×30000 字符双预算（先触达者截断）；**max_chars 对模型隐藏**（schema 不含该参数，执行层封顶 30000 防浪费）；截断标记含「文件共 N 行，已返回第 A-B 行；如需继续请用 start_line=…」；单行超预算时按字符截断该行且续读起点回到该行；空文件/越界 start_line 显式提示。
   - 失败语义：`_result_success`（core.py）——pwsh/run_skill_script 非零退出码、http_request ≥400 → success=False（模型失败路径正确触发）；判定为纯函数，勿在别处另写。
   - 枚举确定性：list_directory 单入口（原 glob_files 已并入：pattern/files_only/recursive/start_after），按名称排序、输出绝对路径、超限附续枚举提示；search_files 默认区分大小写（ignore_case 对子串/正则统一生效）、超大文件跳过与命中总数计入汇总。退役名 glob_files/glob 走 RETIRED_TOOL_GUIDE 引导，会话固化集在 resolve_allowed_tools 入口自动归一（RETIRED_TOOL_MAP）。
   - job_output 增量：输出尾部回传推进后的 cursor（模型凭它读取后续增量）。
5. **视觉模型差异是服务端特性**：DeepSeek 只对 `deepseek-v4-flash`/`v4-pro` 校验 thinking 回传，`vision-exp` 不校验（诊断证实两模型客户端请求完全同构）。**无需为 vision-exp 做适配**。
6. **诊断纪律**：windowed exe 无控制台、应用无 logging handler → **诊断写文件**（test1 分支的 `_write_model_debug` 模式可复用；`AllocConsole` 方案实测无效）。

## 四、未完成 / 下一步（供接手选择）

1. **发布清单执行**（发布时）：`naiba-chat-update.json` 的 `commit`/`sha256` 由 workflow 覆盖；`README.md` 能力列表按需微调；冻结版编译（见 §五）。
2. 浏览器冒烟（Playwright）需用户本机 `npm install playwright` + Edge 权限，本 DSH 沙箱被拦（已知）。
3. `docs/manual/`（手动文档与截图）当前按用户指示不再维护（旧图含已移除的自动路由行，保留现状）。
4. **待用户实测**：① 描述瘦身后的理解质量——文本模型 + 工具轮三场景（识图、后台 Job、ComfyUI「改文件再引用」）；② 可见性统一后工具块展示与模型行为（机器字段不再出现在前端/上下文，`arguments` 仅展示）；③ read_file 双预算与截断标记的续读闭环（大文件多轮读取应显著减少试错）、pwsh/http_request 失败语义（失败提示与重试行为正确）；④ glob_files 并入后，模型"找文件"是否仍顺畅（list_directory 的 pattern/files_only 应覆盖旧 glob 用法）。

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

**用户实测要点**：文本模型（deepseek-v4-flash，思考模式）+ 工具调用（含 vision_analyze 识图）全流程应无 400；文本模型传图后应看到「路径占位 + 模型主动调用 vision_analyze（工具块呈现）」；工具块内不再出现缩略图路径/SHA-256 等机器字段；超长结果带「已截断」标记。

## 六、本次触碰文件（master 增量）

- 后端：`naiba/{core/tool_results(new),core/history,skills/agent,run/chat,run/session,subagent,plans,tools/registry,tools/providers/core,storage/store,vision/runtime,run/stream,run/manager,core/contracts,config,app}.py`
- 前端：`public/{index.html,styles.css,js/09-settings.js,js/11-run-stream.js,js/12-chat-input.js}`
- 测试：`tests/{test_config_migration,test_vision_prepare_history,test_tool_registry_shape,test_tool_description_slim,test_tool_results_visibility,test_tool_design_fixes,test_core_tools_parity,golden_replay}.py`、`tests/golden/{history_images,tool_confirm}.json`（重录）
- 发布/文档：`release_notes.json`、`README.md`、`项目维护说明（修改代码前必读）.md`、`docs/dev/tools-enhancement-handoff.md`（本文档）、`docs/dev/tools-description-slim.md`（瘦身对照，评审后删除）、`版本更新说明.md`（本地，gitignored）。**`docs/manual/` 按用户指示不再维护。**
- 验证脚本（gitignored）：`.tmptest/esm_graph_check.py` 已修复（剥离注释/字符串/模板/正则后的词边界扫描，消除正则字面量误报；合成用例验证真缺 import 仍能检出）

## 七、交接结语

工具系统统一架构、增强、视觉驱动化、别名隐藏、描述瘦身与返回可见性已全部落地并通过验证；run/manager 拆分遗留（影子类）已清理；死管线（tool_runs 表/log_tool_run/summarize）已删除。当前没有任何已知未修 bug。剩余工作均为发布动作与用户实测。接手第一步：`git status` 确认干净 + 跑 §五 全量测试（30 秒）确认基线。
