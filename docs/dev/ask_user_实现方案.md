# Naiba Chat 新增 `ask_user` 结构化提问工具 —— 实现方案

> 目标版本：master（1.6.8-beta 之后的下一个功能版本）
> 形态：AI 以工具调用的方式向用户提 1–4 道选择题（可多选、可自定义输入），答案以结构化 JSON 回传模型并继续执行。对标 WorkBuddy 的 `AskUserQuestion`，但完全复用 Naiba Chat 现有的确认通道，不做架构迁移。
> 本方案只读产出（已勘察真实代码），改动待评审后另起 `feat/ask-user-question` 分支实施。

---

## 1. 背景与现状对照

### 1.1 Naiba Chat 现有的三种"人机交互"机制

| 机制 | 触发方式 | 前端形态 | 阻塞性 | 答案回传方式 | 代码位置 |
|---|---|---|---|---|---|
| **工具确认 confirm** | 副作用工具 + `confirm` 权限模式 | 消息流内嵌卡片（⚠️ 需要确认 / 允许执行 / 拒绝） | 是（run 线程 `wait_for_confirmation` 轮询，5 分钟超时） | confirm_id → 后台执行 → 结果写 `confirmation_results` 被 wait 拿到 | `skill_runtime.py` `ToolExecutor.execute`/`confirm_execute`；消费端 `_execute_with_retry:2037`；API `server.py:_confirm_tool` |
| **选项检测 choice** | 模型**文本回复**里出现选项块，后端正则 `_detect_choice_groups`（server.py:484） | composer 上方胶囊按钮组（多组可回退/进度） | 否 | 把选项文本 `fillComposer` 填进**输入框**，用户按发送 → 作为普通文本给模型 | `async_tasks.py:1195/1248`；前端 `showChoiceButtons`（app.js:5191） |
| **计划确认 plan bar** | plan 模式 ready/building | 顶部 plan bar（Approve/Edit/Confirm/Reject） | 部分 | confirm_id 走 `/api/tool/confirm` | plan bar 渲染 app.js:1651；`resolvePlanConfirmation` app.js:1790 |

### 1.2 结论：缺的是"结构化提问"通道

- **confirm**：布尔式"允许/拒绝"，只适合批准工具执行，不适合收集参数/澄清需求。
- **choice**：非阻塞、答案以文本经输入框回传（用户可改），且只支持单选一层（无 header/多选/自定义输入语义；多组选项只是"连问多道"）。适合自由对话里给用户快捷回复，不适合 agent 需要**确定性决策结果**才能继续的流程（如：用哪个方案、选哪张素材、批量参数确认）。
- 因此新增 **`ask_user` 系统工具**：本质是"需要用户输入才能返回结果"的工具调用，答案**绕开输入框**直接作为该工具的 result 回模型，跑同一套 confirm 事件的等待/唤醒骨架。

### 1.3 对齐 WorkBuddy 的语义（逆向结论）

对 WorkBuddy `AskUserQuestion`（`app.asar\cli\dist\codebuddy.js`）逆向还原的关键设计，本方案照搬：

1. schema：`questions` 1–4 个；每题 `question`(string，必填) + `header`(≤12 字徽标) + `options` 2–4 个 `{label, description}`（label 每题内唯一、question 全文唯一，schema refine 校验）+ `multiSelect`(bool)。
2. **UI 自带"或输入自定义答案…"输入框** → 工具描述中明确要求模型**不要**提供 "其他/Other" 选项（否则 UI 重复）。
3. 用户任何输入（含不在 options 里的自定义文本）都视为合法答案 —— 工具描述告知模型"不要质疑不匹配选项 label 的答案"。
4. 取消/超时 → 返回"用户未作答"，模型据上下文继续（不允许无限重问）。

---

## 2. 工具契约

### 2.1 工具名与描述

```
name:        ask_user
description: 向用户提出 1-4 道选择题。UI 会同时展示预置选项和一个自定义输入框，
             用户既可以点选预置项，也可以输入自己的答案；任何答案（即使不匹配
             预置选项 label）都应视为有效。仅在你确实需要用户决策、且现有上下文
             不足以自行推进时使用；能用默认值推进时不要打断用户。
permission:  confirm（但语义特殊：见 §3）
side_effect: false
retryable:   false
```

### 2.2 参数 JSON Schema（照 WorkBuddy 契约精简，供 tool_registry 注册）

```jsonc
{
  "type": "object",
  "properties": {
    "questions": {
      "type": "array",
      "minItems": 1,
      "maxItems": 4,
      "description": "要向用户提出的问题（1-4 道）",
      "items": {
        "type": "object",
        "properties": {
          "question": {
            "type": "string",
            "description": "完整问题文本，清晰具体，建议以问号结尾"
          },
          "header": {
            "type": "string",
            "description": "极短标签（≤12 字），渲染为徽标 chip，如「方案」「素材」"
          },
          "options": {
            "type": "array",
            "minItems": 2,
            "maxItems": 4,
            "description": "选项 2-4 个；禁止提供「其他/Other」项（UI 自带自定义输入框）",
            "items": {
              "type": "object",
              "properties": {
                "label":       { "type": "string", "description": "选项显示文本，每题内必须唯一" },
                "description": { "type": "string", "description": "选项含义/取舍说明（可选）" }
              },
              "required": ["label", "description"]
            }
          },
          "multiSelect": { "type": "boolean", "description": "是否允许多选", "default": false }
        },
        "required": ["question", "options"]
      }
    }
  },
  "required": ["questions"]
}
```

**运行时校验**（`validate_ask_user(questions)`，Python 实现）：数量 1–4；每题有非空 `question`；options 2–4 且 label 非空、**每题内唯一**；所有 question 文本全局唯一。校验失败 → 工具返回 `(False, "ask_user 参数不合法：…")`，不打断会话。

### 2.3 工具结果（回给模型的文本）

- 用户提交：`(True, json.dumps(answers))`，`answers` 形如 `{"q_0": "选项A", "q_1": ["选项B","自定义…"]}`（单选 string，多选 list，下标即 questions 序号，与 WorkBuddy 的 `q_0…q_n` 一致）。
- 用户点取消：`(False, "用户选择不回答这些问题，请根据已有信息继续当前任务，不要重复提问。")`
- 等待超时（5 分钟）：同上（"用户未在 5 分钟内作答，已自动跳过"）。

---

## 3. 执行语义与权限矩阵

`ask_user` **不是**可批准执行的副作用工具，而是"必须由用户给答案才能出结果"的阻塞工具：

| permission_mode | 行为 |
|---|---|
| confirm / auto / full | 一律弹出问题卡片等用户作答（auto/full 只是"工具自动批准执行"，不适用于本工具 —— 答案只能由人给）。**注意与现有语义的差异**：不能在 auto/full 下把问题"自动批准"掉 |
| deny | 直接返回失败："当前权限模式为 deny，无法向用户提问"（模型会自行降级） |

实现上**不混入** `pending_confirmation`（那里是 tool+arguments 等待批准执行），而是平行一套 `pending_question`，避免污染布尔批准语义、也避免并行确认与并行提问互相踩。

---

## 4. 改动清单（按文件，均以 master 现状为基准）

> 通用备注：skill_runtime.py 的 ToolExecutor 与 async_tasks.py 的 RunManager 均在「上一可用版本确认通道」上新增平行分支，**不改动**现有 NEED_CONFIRM / confirm / choice 任何逻辑。

### 4.1 `skill_runtime.py` — ToolExecutor（≈行 411 起）

`__init__` 增加两个容器（与 411 行 `pending_confirmation` 并列）：

```python
self.pending_question: dict[str, dict[str, Any]] = {}
self.question_results: dict[str, tuple[bool, str]] = {}
```

`execute()`（≈行 568，`_confirmation_reason` 判定**之前**）增加前置分支：

```python
if tool == "ask_user":
    if self.permission_mode == "deny":
        return False, "权限被拒绝：当前为 deny 模式，ask_user 不可用"
    error = _validate_ask_user_questions(arguments.get("questions"))
    if error:
        return False, f"ask_user 参数不合法：{error}"
    question_id = str(uuid.uuid4())
    with self._confirmation_lock:
        self.pending_question[question_id] = {
            "questions": arguments.get("questions"),
            "processing": False,
        }
    return False, (
        f"NEED_QUESTION:{question_id}:"
        f"{json.dumps(arguments.get('questions'), ensure_ascii=False)[:2000]}"
    )
```

> 复用现有 `execute` 返回 `NEED_*` 前缀字符串 + wait 轮询的骨架，`ask_user` 不需要 `_tool_ask_user` handler（永远不会走到 `_execute_unchecked`）。model 侧可见性由 tool_registry 的 ToolSpec 提供（§4.3）。

新增三个方法（照 `confirm_execute`/`reject_execute`/`wait_for_confirmation` 实现，行 617-700 同款模式）：

```python
def answer_question(self, question_id: str, answers: dict[str, Any]) -> tuple[bool, str]:
    """用户提交答案：写 question_results，唤醒 wait_for_question。"""
    with self._confirmation_lock:
        pending = self.pending_question.get(question_id)
        if not pending or pending.get("processing"):
            return False, "提问ID无效或已过期"
        pending["processing"] = True
        self.pending_question.pop(question_id, None)
        result = (True, json.dumps(answers, ensure_ascii=False))
        self.question_results[question_id] = result
    return result

def skip_question(self, question_id: str) -> tuple[bool, str]:
    """用户取消/超时清理。"""
    with self._confirmation_lock:
        pending = self.pending_question.pop(question_id, None)
        if not pending:
            return False, "提问ID无效或已过期"
        result = (False, "用户选择不回答这些问题，请根据已有信息继续当前任务，不要重复提问。")
        self.question_results[question_id] = result
    return result

def wait_for_question(self, question_id: str, timeout: float = 300,
                      cancel_event: threading.Event | None = None) -> tuple[bool, str]:
    """照 wait_for_confirmation（~行 684）轮询 question_results；cancel_event 置位时
    清理并抛 TaskCancelled；超时自动 skip。"""
```

### 4.2 `skill_runtime.py` — `_execute_with_retry` 消费端（≈行 2037）

在现有 `NEED_CONFIRM` 判定后追加平行分支：

```python
if not success and result.startswith("NEED_QUESTION:"):
    confirmation_requested = True          # 复用：禁止自动重试
    parts = result.split(":", 2)
    if len(parts) >= 3:
        question_id = parts[1]
        try:
            questions = json.loads(parts[2])
        except Exception:
            questions = []
        event({
            "type": "tool_question",
            "question_id": question_id,
            "tool_name": tool,             # "ask_user"
            "questions": questions,
        })
        confirmation_executor = (
            (run_context or {}).get("executor")
            if isinstance(run_context, dict) else None
        ) or self.executor
        success, result = confirmation_executor.wait_for_question(
            question_id, timeout=300, cancel_event=cancel_event
        )
```

> plan_runtime.py 若有独立事件消费循环，同样按 `kind == "tool_question"` 补分支（先在实现分支 grep `"tool_confirm"` 全仓，逐处对齐，见 §8 检查清单）。

### 4.3 `tool_registry.py` — 注册 ToolSpec（使模型与 Agent 工具目录可见）

在 `build_core_tool_specs()`（或 server.py 组装处的 specs 列表，server.py:2717 `build_tool_registry()`）追加：

```python
ToolSpec(
    name="ask_user",
    description=("向用户提出 1-4 道选择题：UI 同时展示预置选项与自定义输入框；"
                 "任何答案都有效，即使不匹配预置选项；仅当确实需要用户决策且"
                 "无法用默认值推进时使用，不要用它问能从上下文推断的问题。"),
    parameters=_ASK_USER_PARAMETERS,   # §2.2 的 JSON Schema
    side_effect=False,
    retryable=False,
    timeout=620,
    permission="confirm",
),
```

> 分发链已确认：`tool_registry.execute()` 对已注册 spec → `executor.execute(tool, …)`（tool_registry.py:217）→ 命中 §4.1 的前置分支。无需注册 execute handler。
>
> **包装 executor 放行验证（plan_runtime.py）**：craft 路径 `CraftToolExecutor.execute`（行 131）只对 `write_file`/`edit_file` 做工作区内自动放行特判，其余一律 `self._inner.execute(...)`；plan 路径 `ReadOnlyToolExecutor.execute`（行 87）的 BLOCKED_TOOLS（行 91）不含 ask_user，也放行到内层。两层包装均不会吞掉 NEED_QUESTION，直达 ToolExecutor.execute 前置分支。

### 4.4 `plan_runtime.py` — 工具白名单（行 29/40）

```python
ALL_TOOLS = ALL_TOOLS + ("ask_user",)          # 行 29 元组追加
# READONLY_TOOLS（行 40）不加：ask_user 非只读语义，plan/ask 模式的
# resolve_mode_tools（行 68）会把它过滤掉 —— 决策符合预期：plan 只读澄清
# 阶段不允许向用户提问打断？—— 见 §7 风险与决策点 R2
```

### 4.5 `async_tasks.py` — RunManager

1. `emit()`（≈行 750 的 `elif kind == "tool_confirm"` 之后）追加 detail 分支，`status = "waiting"`，`detail.message = "等待你的选择"`、携带 `question_id`/`questions`，供 run 卡片/列表状态展示。
2. 新增三个归属/回答方法，照 `owns_confirmation`（1731）/`confirm_tool_async`（1776）/`reject_tool`（1786）同款签名：

```python
def owns_question(self, run_id: str, question_id: str) -> bool: ...
def answer_question(self, run_id: str, question_id: str, answers) -> tuple[bool, str] | None:
    if not self.owns_question(run_id, question_id): return None
    executor = ...  # self._executors.get(run_id)
    return executor.answer_question(question_id, answers)
def skip_question(self, run_id: str, question_id: str) -> tuple[bool, str] | None: ...
```

3. 会话取消/新运行接管时清理（≈行 1686-1691 已有对 `pending_confirmation` 的遍历 reject，平行加一段对 `pending_question` 调 `skip_question`，避免残留问题卡片悬挂）。
4. **允许工具集常驻注入**（≈行 1003 过滤 allowed_tools 处）：`allowed_tools` 恒追加 `"ask_user"`，保证：
   - tools 数组**任何轮次稳定**（不因模型是否调用而变化）→ 不破坏 DeepSeek 前缀缓存（与视觉工具"常驻"同款理由，见 1003 行注释）。
   - 在 `tools_available` 事件（≈行 1006）中一并上报。

### 4.6 `server.py` — HTTP API

路由（行 4112 `/api/tool/confirm` 旁）：

```python
elif path == "/api/tool/answer":
    self._answer_tool(body)
elif path == "/api/tool/answer_skip":
    self._skip_tool(body)
```

Handler（照 `_confirm_tool` 4796 / `_reject_tool` 4819）：

```python
def _answer_tool(self, body):
    question_id = str(body.get("question_id") or "").strip()
    run_id = str(body.get("run_id") or "").strip()
    answers = body.get("answers")
    if not question_id or not run_id or not isinstance(answers, dict) or not answers:
        self._json({"error": "run_id、question_id 与 answers 不能为空"},
                   HTTPStatus.BAD_REQUEST); return
    result_pair = APP.runs.answer_question(run_id, question_id, answers)
    if result_pair is None:
        self._json({"error": "提问不属于该运行或已失效"}, HTTPStatus.CONFLICT); return
    success, result = result_pair
    self._json({"success": success, "result": result})

def _skip_tool(self, body):  # 同 _reject_tool 骨架，调 APP.runs.skip_question
```

> 前端提交是即时写结果、不等待模型（同 confirm_tool_async 的理由：浏览器请求不能挂几分钟），run 线程经 wait 轮询自行续跑。

### 4.7 `public/app.js` — 事件接收与卡片渲染

**A. `handleRunEvent` 新增分支**（`event.type === 'tool_confirm'` ≈行 5056 旁）：

```javascript
} else if (event.type === 'tool_question') {
  clearElapsedStatus();
  if (row.dataset.sawTool !== 'true') { moveBottomProseInline(row, answer); row.dataset.sawTool = 'true'; }
  renderQuestionCard({
    answer,                       // 卡片插到 answer 之前，同 tool_confirm
    questionId: event.question_id,
    runId: event.run_id || runId,
    questions: event.questions || [],
  });
  scrollToBottom();
}
```

**B. 渲染函数** `renderQuestionCard({...})`（建议新增在 `approveTool` 附近）：

- 外层容器 `.tool-question[data-question-id]`，结构：

```
┌───────────────────────────────────────────────┐
│ 标题行：❓ 请回答（N 道）                        │
│ ───────────────────────────────────────────── │
│ [header chip] 问题文本    (多选)                │
│   ○ 选项A — description         单选=圆点      │
│   ● 选项B — description         多选=方框+✓    │
│   或输入自定义答案… ────────────────            │
│ ───────────────────────────────────────────── │
│  [第2题] ... （1–4 题纵向排列，题间分隔线）      │
│ ───────────────────────────────────────────── │
│              [跳过]        [提交答案]          │
└───────────────────────────────────────────────┘
```

- 每题交互：单选点击即唯一选中（圆点）；`multiSelect` 多选（方框，可多选）；每题一个"或输入自定义答案…"文本框 —— 单选时输入自定义文本覆盖点选值，多选时自定义文本并入数组（与 WorkBuddy `$B` 组件行为一致）。
- 提交：收集 `{q_0: …, q_1: […]}` → `POST /api/tool/answer`。成功后把卡片内容替换为"已回答"摘要（每题一行 header + 答案），并 `last.open=false` 折叠（对齐 `tool_result` 的处理习惯）。
- 跳过：`POST /api/tool/answer_skip`，替换为"已跳过"摘要。
- 请求期间禁用按钮；失败 `toast`。同 approveTool/rejectTool（行 5309/5326）的写法。
- **清理钩子**：`renderMessages` / 会话切换 / 取消事件时，若存在 `.tool-question` 未提交，不弹窗、静默移除（后端超时兜底 5 分钟自动 skip）。

### 4.8 `public/styles.css` — 样式

复用现有变量体系（`--amber/--green/--line/--surface/--muted/--radius`，参照 326-341 行 `.tool-confirm*`）。新增类（草案名）：

```css
.tool-question            /* 卡片容器：border/radius 同 .tool-confirm，加左侧 accent 边框 */
.tool-question-header     /* ❓ + "请回答" 标题 */
.tool-question-item       /* 单题块，题间 border-top 分隔 */
.question-chip            /* header 徽标：小号圆角标签（参照设置面板 chip 视觉） */
.question-option          /* 选项按钮行：全宽、左侧 radio/checkbox 图形 */
.question-option.selected /* 选中态：绿 border + 绿 soft 底（呼应 .choice-btn:hover 配色） */
.question-radio / .question-checkbox  /* 圆点/方块图形（纯 CSS，勿用 emoji） */
.question-custom-input    /* 自定义答案输入框 */
.tool-question-actions    /* 底部按钮行 */
.tool-question-status     /* 提交中/已提交/已跳过状态文案 */
```

> 视觉基调贴近 WorkBuddy 但用 Naiba 的绿色系选中态（与 `.choice-btn:hover` 一致），避免引入新色。

---

## 5. 交互时序（一次完整提问）

```
用户消息 → 模型调用 ask_user{questions}
  → ToolExecutor.execute() 命中前置分支，pending_question 入表
  → 返回 "NEED_QUESTION:<qid>:<questions-json>"
  → _execute_with_retry 解析 → event({type:"tool_question", question_id, questions})
    → RunManager.emit() 写事件 + background_task.status="waiting"
    → SSE/事件流 → 前端 handleRunEvent → renderQuestionCard 插入消息流
  → （run 线程进入 wait_for_question 轮询，最多 5 分钟）
  → 用户点选项/填自定义 → [提交答案]
    → POST /api/tool/answer → RunManager.answer_question → executor.answer_question
      → question_results[qid] = (True, '{"q_0": ...}')
    → wait_for_question 拿到结果 → 作为 ask_user 工具 result 回模型
  → 模型继续（可引用答案继续执行）
```

与现有 confirm 的唯一差别：答案内容由用户填写而非"批准后执行工具"，等待/唤醒/超时/取消/清理骨架完全复用。

---

## 6. 与现有 choice 机制的分工（产品语义）

| | choice（已有） | ask_user（新增） |
|---|---|---|
| 发起 | 模型文本里的选项块，**被动检测** | 模型**主动工具调用**，有 schema 约束 |
| 答案通道 | 填输入框 → 用户可改 → 普通文本发送 | 直接作为工具结果回传（不经输入框） |
| 结构 | 单选文本；无 header/多选 | header 徽标 / 单选 / 多选 / 自定义输入 |
| 阻塞 | 否 | 是（agent 等答案） |
| 适用 | 闲聊、快捷回复、让用户选择后继续对话 | 参数决策、方案选择、批量确认等**必须拿到确定答案**的流程 |

两者共存不冲突：choice 保留给轻量场景，ask_user 供 agent/计划流程做决策点。

---

## 7. 边界、风险与待决策点

- **R1 auto/full 模式下 ask_user 照常弹窗**（答案必须人给）。若产品上希望 auto/full 下模型**不得**调用 ask_user（避免打扰），可在 system prompt 追加一句"当前权限模式下不要调用 ask_user"，而不是拒绝工具（模型可能重试）。倾向：**照常可用**，这是用户主动授权给 AI 提问权的显式语义。
- **R2 plan 只读模式**：`resolve_mode_tools` 会把 ask_user 从 plan 模式过滤掉（READONLY_TOOLS 不含它）。但 plan prepare 阶段恰恰最常需要澄清 → **决策点：是否在 plan 模式也放行 ask_user**（建议放行：把它加进 resolve_mode_tools 的 plan 分支白名单）。若放行，plan 阶段提问走同一卡片。
- **R3 前缀缓存**：tools 数组必须稳定常驻 ask_user（§4.5-4），严禁按"本轮是否调用"动态增删。
- **R4 超时**：5 分钟与 confirm 一致；超时结果文本引导模型"不要重复提问，用已有信息继续"。
- **R5 取消竞态**：用户在新一轮消息打断提问 → 事件流消费端按行 1686 同款逻辑清理 pending_question，避免旧卡片悬挂、新 run 误收旧答案（ownership 校验已防：`owns_question` 按 run_id 隔离）。
- **R6 视觉一致**：不与现有 emoji 图标体系冲突（tool-confirm 用 ⚠️ emoji），question 卡片建议沿用同款 emoji 风格（❓）或纯 CSS 图标，与产品一致即可。

## 8. 测试与验证清单

1. `python -m py_compile` 全部改动文件。
2. 单测级（可用 `_smoke_harness.py` 的 FakeExecutor 模式扩展）：`answer_question`/`skip_question`/`wait_for_question` 的超时、取消、重复 answer（第二答应判过期）。
3. 参数校验：0 道题 / 5 道题 / options 1 个 / label 重复 / question 重复 → 返回参数错误不阻塞会话。
4. 手工 UI（源码模式起服务）：
   - craft 对话让模型调用 ask_user（可用 System Prompt 或直接要求"用 ask_user 问我用哪个方案"）→ 卡片出现、run 状态 waiting；
   - 单选/多选/自定义输入各提交一次 → 卡片变"已回答"摘要、模型下一轮引用答案继续；
   - 点跳过 → 模型收到"用户选择不回答"，继续不重问；
   - 5 分钟超时路径（把 timeout 临时改 10s 验证）→ 自动跳过文本；
   - auto / full 模式各验证一次：照常弹卡（不被 auto 批准吞掉）；
   - 并行：先触发一个 confirm 再触发 ask_user → 两卡片互不干扰；
   - 提问中直接发新消息 → 旧卡片清理，无悬挂；
   - confirm / choice / plan bar 三件套回归不回归退化。
5. `tools_available` 事件里能看到 ask_user，会话工具目录勾选页出现 ask_user。

## 9. 后续可迭代（不在本版范围）

- 多题分页 tab + "审核答案"汇总页（WorkBuddy 形态，目前先纵向全排）。
- 已答卡片折叠为时间线一行（同 tool-run 折叠习惯）。
- 前端"自定义答案"富化：Enter 提交、长输入自适应。
