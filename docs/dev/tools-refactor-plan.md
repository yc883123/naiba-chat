# 工具系统重构方案（统一工具架构）

> **本文档性质**：本次「统一工具架构」重构的工作方案与决策记录。重构完成后随其使命结束删除
> （与先例一致：重构期的方案/交接文档完成后即删除）；期间以 `项目维护说明.md` 为唯一权威现状说明书，
> 本文档只描述"将要变成什么样 + 怎么变"，不描述现状细节。
>
> 状态：**已确认开工**（2026-09-xx）。所有阶段以逻辑单元提交，每阶段全量验证。

---

## 一、重构目标

把当前"三层三通道"的工具系统收敛为**单一工具结构 + 单一注册入口 + 单一分发通道**：

- **一个工具 = 一个 `ToolSpec`**：名称/描述/参数 schema/副作用/可重试/超时/权限/**执行函数**/结果摘要/**策略**/别名/元数据，全部一处落地；
- **来源无差别**：内置 core 工具、Job 系统工具、vision、search、comfyui、skill 能力工具、MCP 动态工具——统一结构、统一注册、统一分发，为未来用户自定义工具（= 又一个 provider）铺平道路；
- **权限与副作用同源**：风险评估是 def 的属性（`policy`，默认由 side_effect/permission/annotations 推导），消灭 `DANGEROUS_TOOLS`/`_confirmation_reason` 双源漂移。

## 二、现状问题（重构动因）

| # | 问题 | 证据 |
|---|---|---|
| 1 | 一个工具的定义被劈成 3~4 处 | schema 在 `registry.py` builder；实现散在 `_tool_*` 方法 / app.py 闭包 / jobs.py factory / vision.py；权限行为在 `DANGEROUS_TOOLS` + `_confirmation_reason` |
| 2 | 分发是 If 链猜类型 | `registry.execute`：`mcp__` 前缀 → executor；`system_handlers` → handler；spec+executor → executor；带 `.` → executor |
| 3 | MCP 双路径 | `register_mcp_tools` 同时注册 spec+handler，但 `mcp__` 分支优先命中 executor，handler 是影子路径 |
| 4 | 权限策略双源 | ToolSpec.permission 与 ToolExecutor 内部 `_confirmation_reason`/`DANGEROUS_TOOLS` 各自为政（如 `http_request` 声明 side_effect=True 但 GET 实际无副作用） |
| 5 | 系统处理器无 schema 关联 | 注册即闭包，声明与实现只靠"名字碰巧对上"，无强校验 |
| 6 | 文件膨胀 | `registry.py` 909 行（声明+构建+分发+查询混合）、`executor.py` 737 行（模式状态机+11 实现+路径/搜索逻辑混合） |

## 三、重构哲学（六条 · 已对齐）

1. **单一定义**：一个工具 = 一个 ToolSpec，全部信息一处落地；
2. **按域一模块**：`ToolProvider` 按自然域分组（core/jobs/capability/vision/search/comfyui/mcp），不做"一工具一文件"；
3. **分发零猜测**：`execute()` 只查 def → 调 `def.execute`；别名在查询层解析（def 字段 `aliases`），不进执行层；
4. **权限同源**：`policy` 是 def 属性（默认由 side_effect/permission/annotations 推导），引擎不再特判 MCP/工具类型；
5. **依赖注入、组装根唯一**：provider 不摸 app 对象，显式构造参数，`app.py` 只做组装；
6. **不过度设计**：不做装饰器 DSL、不做"内置用户自定义工具"抽象（外部扩展通道 = MCP）、不建 ToolFramework 基类。
   硬约束：不重命名工具、不改参数名/协议、不改事件契约；行为优化仅限权限/副作用标注类修正。

## 四、决策记录（用户确认）

| 决策点 | 结论 |
|---|---|
| 重构目标 | 统一工具架构（用户后续补充：先对齐哲学 → 本文件 §三 为对齐结果） |
| 交付节奏 | 分阶段落地（Phase 0–5） |
| 行为等价 | 允许小幅行为优化（权限/副作用标注修正，golden 需重录则人工核查） |
| 注册形态 | 显式对象列表（provider 返回 `list[ToolSpec]`），不做装饰器 DSL |
| core 实现归属 | **拆成函数迁入 provider**（11 个 `_tool_*` → `providers/core.py` 独立函数，`self`→`ctx`）——最高风险项，缓解见 §六 |
| 迁移细则 | 双轨兼容、最后拆除（旧接口/旧路径保留至 Phase 5 统一删除） |
| ToolSpec 扩展 | 新增 `metadata: dict`（通用扩展位，默认空；为未来自定义工具展示/分组字段预留）、`execute`、`aliases`、`policy` |
| 查询泛化 | `readonly_mcp_tools()` → 泛型 `readonly_tools()`（按 side_effect/annotations 过滤），旧名保留为兼容 |

## 五、目标架构

```
naiba/tools/
├── registry.py      # def 表 + 别名解析 + 查询 + 单一插槽分发 + MCP 动态注册（影子路径删除）
├── engine.py        # ToolExecutor：权限状态机（confirm/auto/full/deny + NEED_CONFIRM 协议）+
│                    #   路径解析助手（策略需要）；公共 API 完全不变；不实现任何工具
├── providers/
│   ├── core.py      # ToolContext + 11 个实现函数 + core_tool_specs()
│   ├── jobs.py      # run_in_background/job_*/subagent/todo_write/artifact_report
│   ├── capability.py# inspect_installed_skill/install_skill/unpack_skill_archive
│   ├── vision.py    # 7 分析 + 2 写入 + vision_read_folder
│   ├── search.py    # web_search/recall_history
│   └── comfyui.py   # comfyui_prepare_workflow/comfyui_batch
│   (mcp 动态工具：def.execute 绑定 mcp.call 闭包，走同一 register_tools 入口，生命周期动态为唯一来源差异)
└── __init__.py
```

**ToolSpec 最终字段**：`name / description / parameters / side_effect(default True) / retryable / timeout / permission / execute / summarize / aliases: tuple / policy / annotations / metadata`。

## 六、风险与应对

| 风险 | 等级 | 应对 |
|---|---|---|
| 拆函数迁移（改写型手术） | 高 | 函数体字节级原样抽取仅改 `self`→`ctx`；双轨期新旧实现采样对拍；旧 `_tool_*` 至 Phase 5 才删 |
| plans.py 包装器摸私有成员 | 中 | `ReadOnly/CraftToolExecutor` 依赖 `_inner.workspace/_resolve_tool_path/_path_within/_execute_unchecked`；迁移期保留这些成员，Phase 5 同提交内改走公开接口 |
| 测试覆盖薄弱 | 中 | Phase 0 补 `test_tool_registry_shape.py` 守门；每域迁移时补该域对拍用例 |
| golden 基线受影响 | 中 | 行为变更必须重录并人工核查后再固化 |
| 双轨期新旧行为不一致 | 中 | 每阶段全量 unittest + golden + 未定义名扫描（复用 `.tmptest/scan_undef_all.py`） |

- 影响面：`naiba/tools/*` + `app.py` 装配段 + `plans.py` 包装器 + `server.py` re-export；前端/事件契约零改动；
- 可回滚：每阶段独立 commit 可 revert；双轨期全路径保留；
- 涉密：无。

## 七、分阶段计划（每阶段 = 一个逻辑单元提交）

### Phase 0｜基线固化与护栏补齐（docs + tests，无行为变化）
- [ ] 全量 unittest + golden 基线确认绿；
- [ ] `.tmptest/scan_tools.py`：工具盘点对照表（工具×builder×执行位置×权限来源×别名），AST 静态+registry 运行时双源交叉；
- [ ] `tests/test_tool_registry_shape.py` 守门：名字唯一/description 非空/parameters 结构/别名唯一且目标存在/每个工具恰好一个执行通道（executor 方法或 system handler 或 MCP 或 def.execute）/permission 取值合法；
- 提交：`test: 工具系统重构基线盘点与护栏测试`

### Phase 1｜新骨架（双轨开始）
- [ ] `ToolSpec` 扩展 `execute/aliases/policy/metadata`（默认兼容现有语义）；
- [ ] `ToolRegistry`：`register_provider(provider)` + 单插槽分发（def 优先）+ 查询层别名解析；`bind_executor/register_system_handler/get/has/schemas/mcp 动态`全部原样保留；
- [ ] 守门测试扩展：新字段一致性（alias 解析=TOOL_ALIASES 等价、metadata 不进 schemas() 输出）；
- 提交：`refactor: 工具注册表单一定义与 Provider 骨架（双轨）`

### Phase 2｜权限同源（行为优化落点）
- [ ] `_confirmation_reason` 改造为 def 级 `policy`（默认由 side_effect/permission/annotations 推导）；`DANGEROUS_TOOLS` 并入策略；
- [ ] 行为优化：`http_request` 按 method 判副作用；
- [ ] 审查 golden「工具确认」基线，需重录则人工核查；
- 提交：`refactor: 工具权限策略并入 ToolSpec 同源`

### Phase 3｜core 域迁移（双轨对拍）
- [ ] `providers/core.py`：ToolContext + 11 函数 + core_tool_specs()；原样抽取；
- [ ] core def.execute 绑定新函数；旧 `_tool_*` 保留；确定性工具（read/list/glob/search/edit/write）新旧对拍采样；
- 提交：`refactor: core 工具实现迁入 provider（双轨）`

### Phase 4｜域迁移（每域一提交）
- [ ] `providers/jobs.py`：`job_tool_handler_factory`/`subagent_handler_factory`/app 内 todo_write/artifact_report 闭包收敛；
- [ ] `providers/capability.py`、`providers/vision.py`、`providers/search.py`、`providers/comfyui.py`；
- [ ] MCP 动态注册统一签名（def.execute 绑 mcp.call，删影子系统处理器）；
- [ ] `app.py` 装配段收敛为 `for p in providers: registry.register_provider(p)`；旧 `register_system_handler` 保留（双轨）；
- 提交：每域一个 `refactor: <域>工具迁入 Provider`（+ MCP 一并）

### Phase 5｜单轨收尾
- [ ] 删 If 链旧分发 / `TOOL_ALIASES` / `DANGEROUS_TOOLS` / 旧 `_tool_*` / 旧 builder / `register_system_handler`（调用清零后）；
- [ ] plans.py 包装器改走公开接口；`readonly_mcp_tools` → `readonly_tools` 泛型查询；
- [ ] `registry.py` 瘦身、executor 改名/归位（engine）+ `naiba/tools/__init__.py` 导出；
- [ ] 同步改写 `项目维护说明.md` §3.3 模块表 + 数字；删除本文档；
- 提交：`refactor: 工具系统单轨收敛与文档同步`

## 七.5、迁移核对单（Phase 0 盘点产出 · 39 工具）

> 由 `.tmptest/scan_tools.py` 生成并人工核对（当前无漂移）。`→` 为目标 provider 模块。

### 通道 A：executor 实现（11 工具 + 5 别名 → `providers/core.py`）

| 工具 | builder | 通道 | 迁移目标 |
|---|---|---|---|
| read_file / write_file / list_directory / search_files / glob_files / edit_file | core | `_tool_*` 方法 | core 域函数 |
| pwsh / run_skill_script / http_request | core | `_tool_*` 方法 | core 域函数 |
| call_mcp / register_mcp | core | `_tool_*` 方法 | core 域函数（含 MCP 兼容） |
| read / write / edit / glob / grep | harness_alias | `TOOL_ALIASES` → 上述 | core 别名 def（aliases 字段） |

### 通道 B：系统处理器（28 工具 → 各域 provider）

| 域 | 工具（数量） | 当前注册点 | 迁移目标 |
|---|---|---|---|
| job/subagent | run_in_background / job_output / job_status / job_wait / job_kill（5） | `subagent.job_tool_handler_factory` | `providers/jobs.py` |
| app 内联 handler | subagent / todo_write / artifact_report / comfyui_prepare_workflow / comfyui_batch / web_search / recall_history（7） | `app.py` 闭包 `register_system_handler` | jobs.py / comfyui.py / search.py |
| capability | inspect_installed_skill / install_skill / unpack_skill_archive（3） | `capability.tool_handlers` | `providers/capability.py` |
| vision | 8 个 vision_*（含 read_folder） | `vision/runtime.py::tool_handlers` | `providers/vision.py` |

### 通道 C：MCP 动态（数量随连接浮动）

`mcp__<server>__<tool>` 由 `mcp.py::on_tools_discovered → register_mcp_tools` 注册（spec + 影子 handler，
`mcp__` 分支实际优先走 executor）。迁移目标：def.execute 绑 `mcp_registry.call` 闭包，删影子 handler。

## 八、验证矩阵

| 阶段 | unittest | golden | 对拍 | 未定义名扫描 | 五点冒烟 | 冻结编译 |
|---|---|---|---|---|---|---|
| 0 | ✅ | ✅ | – | – | – | – |
| 1 | ✅ | ✅ | – | ✅ | – | – |
| 2 | ✅ | ✅（重录则以人工核查为准） | – | ✅ | ✅ | – |
| 3 | ✅ | ✅ | ✅ | ✅ | – | – |
| 4（每域）| ✅ | ✅ | ✅ | ✅ | – | – |
| 5 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

## 九、完成标准

1. 全部阶段完成，单轨收敛，`registry.py`/`executor.py` 职责清晰无影子路径；
2. 全量验证通过（§八），golden 与行为变更均已人工核查；
3. `项目维护说明.md` 与代码严格一致（模块表/数字/契约），本文档已删除；
4. 用户实测（源码跑通 + 冻结版分发形态跑通）。
