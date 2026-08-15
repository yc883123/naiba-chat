# 按 DeepSeek Harness 模型修复异步任务

## 总结

把异步运行从独立的后台聊天系统改为“对话拥有的持久 Run”：

- Craft、Ask、Plan 都在原对话中创建 Run。
- 用户切换对话或关闭流后，Run 继续执行。
- 异步任务列表只显示状态并跳转到所属对话，不提供执行、确认、拒绝或取消按钮。
- Plan 执行、工具确认和停止操作只能在所属对话中完成。
- 保持 Agent 无限步骤循环，仅由完成、显式取消、超时或异常终止。
- 每个对话同时只允许一个主 Run，不同对话可以并行运行。

## 实现变更

### 1. 统一运行管理

- 将 `BackgroundTaskManager` 重构为 `ConversationRunManager`，统一 Craft、Ask、Plan 准备和 Plan 执行的生命周期。
- `/api/chat` 原子保存用户消息并创建 Run，冻结历史、Agent、模型、模式、Skills、附件和权限配置。
- 后台线程执行 Run，HTTP 请求只订阅持久事件，不拥有执行线程。
- 客户端断开、切换对话或关闭事件流只解除订阅；显式停止才设置 Run 的取消事件。
- 同一对话已有活动 Run 时返回 `409` 和 `active_run_id`；不同对话允许并行。

### 2. 持久状态与事件

- 沿用 `background_tasks` 保存 Run，并记录 `kind`、`interaction_mode`、`input_message_id`、`plan_id` 和执行快照。
- 使用 `run_events` 按 `run_id + sequence` 保存状态、文本、推理、工具、确认及终止事件。
- 文本增量批量写入，避免逐 Token 写 SQLite。
- Run 状态统一为 `queued/running/waiting/cancelling/completed/failed/cancelled`。
- 服务重启时将未结束 Run 标记失败，并写入“服务重启，运行已中断”事件。
- 应用关闭时向活动 Run 发送取消信号并等待协作式结束。

### 3. API 与模式语义

- `POST /api/chat` 首先返回 `run_started`，随后输出 Run 的 NDJSON 事件。
- 提供 `GET /api/runs`、`GET /api/runs/{run_id}` 和 `GET /api/runs/{run_id}/events?after=`。
- `POST /api/chat/cancel` 显式取消持久 Run。
- `/api/tasks` 保留兼容，但不作为前端默认发送入口。
- 工具确认和拒绝携带 `run_id`，后端验证确认项归属。
- Craft 自动执行工作区操作；Ask 与 Plan 准备只读。
- `/api/plans/{id}/execute` 创建 `plan_execute` Run，Plan 取消同时取消关联 Run。

### 4. 前端交互

- 普通发送后订阅 Run；切换对话只关闭当前订阅。
- 打开存在活动 Run 的对话时重放事件，恢复生成文本、工具状态、权限确认和 Plan 状态。
- 当前对话有活动 Run 时发送按钮显示停止操作，点击后显式取消。
- 异步任务面板只显示模式、摘要、所属对话、状态和耗时，点击条目进入所属对话。
- 所有异步回调固定使用创建 Run 时的 `conversation_id`。

## 测试与验收

- 切换对话后 Run 继续执行，重新进入可恢复事件与确认状态。
- 关闭事件流不取消 Run；点击停止可以取消。
- 同一对话不能并行两个主 Run，不同对话可以并行。
- Run 使用提交时冻结的消息和配置。
- Craft 保持自动执行；Ask 和 Plan 准备不可写。
- Plan 仅在对话内确认后创建执行 Run。
- 任务列表只读并负责导航。
- 服务重启不会遗留永久活动状态。
- Agent 超过 8 次工具调用仍可完成。

## 默认决策

- 采用 DeepSeek Harness 的 session 所有权模型，不增加独立“后台发送”按钮。
- 所有发送天然可以脱离当前页面继续运行。
- 每个对话最多一个主 Run，并行任务通过不同对话实现。
- 后台任务列表只做观察和导航。
- 不修改 `deepseek-harness`，只参考其 owner 隔离、事件投递和会话内交互设计。
