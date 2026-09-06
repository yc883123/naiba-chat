// ============================================================
// 11-run-stream.js —— 拆分自 public/app.js 第 4710-5237 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, api, escapeHtml, state, toast } from "./01-core.js";
import { messageElement, scrollToBottom, setStickToBottom } from "./04-messages.js";
import { loadTasks } from "./06-tasks-plans.js";
import { createConversation, loadConversations, openConversation } from "./08-conversations.js";
import { renderPendingFiles } from "./10-upload.js";
import { handleChatEvent, hideChoiceButtons, setBusy } from "./12-chat-input.js";
import { hideSkillPopup, parseSkillReferences, renderInputMirror, resizeTextarea, stripSkillReferences } from "./13-skill-refs.js";
export function clearRunReconnectTimers() {
  state.runReconnectTimers.forEach((timer) => window.clearTimeout(timer));
  state.runReconnectTimers.clear();
}

export function detachRunConnection() {
  state.abortController?.abort();
  clearVisionProgress();
  stopRunWatchdog();
  clearElapsedStatus();
  state.runWaitShown = false;
  state.runWaitPrevText = '';
  state.runGeneration += 1;
  state.abortController = null;
}

export function detachRunSubscription() {
  detachRunConnection();
  clearRunReconnectTimers();
  state.chatRunId = '';
  state.runConversationId = '';
  state.runSequence = 0;
  state.runRow = null;
  state.runAttempt = 0;
  state.runReconnectAt = 0;
  state.runProbeMisses = 0;
  state.checkRunEligible = false;
  state.cancelRequested = false;
  state.cancelConversationId = '';
  setConnectionState('connected');
  setBusy(false);
}

// ---- Run 事件流看门狗 / 断线自动重连 / 轮询兜底 / 等待计时 ----
export const RUN_WATCHDOG_INTERVAL = 5000;          // 看门狗扫描周期
export const RUN_WATCHDOG_IDLE = 45000;             // 超 45s 无数据 → 判定为“可能是死流”（>3 次 heartbeat）
export const RUN_WATCHDOG_PROBE_MAX = 2;            // 连续判定空闲超过该次数才真正探针动作
export const RUN_RECONNECT_BASE = 500;              // 退避基准 ms
export const RUN_RECONNECT_MAX = 10000;             // 退避上限 ms
export const RUN_RECONNECT_ATTEMPTS = 3;            // 自动重连上限，超限交还轮询兜底
export const RUN_RECONNECT_COOLDOWN = 15000;        // “服死”后的保守重连冷却 ms，防风暴
export const RUN_STREAM_OPEN_TIMEOUT = 15000;       // 建流（流式 fetch 打开）超时护栏 ms
export const RUN_WAIT_STATUS_IDLE = 6000;           // 无任何新进展字节超过此阈值 → 显示“等待中 · 已等待 X 秒”

export function backoffDelay(attempt) {
  const cap = Math.min(RUN_RECONNECT_MAX, RUN_RECONNECT_BASE * 2 ** Math.max(0, attempt - 1));
  return Math.min(cap, cap / 2 + Math.random() * (cap / 2));
}

// 校验当前仍处于“给定代际的连接所对应的活动流”。
export function isRunGenerationActive(generation, controller = null) {
  if (state.cancelRequested) return false;
  if (state.runGeneration !== generation) return false;
  if (controller && state.abortController !== controller) return false;
  return true;
}

// 带超时的 fetch，避免建流永久挂起（carrier 不 fire onOpen 也不 return）。
export async function fetchRunEvents(runId, controller) {
  let timeoutId = null;
  const timeout = new Promise((_, reject) => {
    timeoutId = window.setTimeout(
      () => reject(new Error('建立事件流超时')),
      RUN_STREAM_OPEN_TIMEOUT,
    );
  });
  try {
    const response = await Promise.race([
      fetch(`/api/runs/${encodeURIComponent(runId)}/events?after=${state.runSequence}`, {
        headers: { Authorization: `Bearer ${state.token}` },
        signal: controller.signal,
      }),
      timeout,
    ]);
    return response;
  } finally {
    if (timeoutId) window.clearTimeout(timeoutId);
  }
}

// 连接状态去重设置（角标依据）。
export function setConnectionState(next) {
  if (state.connectionState === next) return;
  state.connectionState = next;
}

// 显示 “{base} · 已等待 X 秒” 到 #runtimeStatus，每秒刷新；先清除旧计时。
export function showElapsedStatus(base) {
  clearElapsedStatus();
  const since = Date.now();
  state.elapsedBase = String(base || '');
  state.elapsedSince = since;
  const update = () => {
    const seconds = Math.max(0, Math.floor((Date.now() - since) / 1000));
    const el = $('#runtimeStatus');
    if (el && state.elapsedBase) el.textContent = `${state.elapsedBase} · 已等待 ${seconds} 秒`;
  };
  update();
  state.elapsedTimer = window.setInterval(update, 1000);
}

export function clearElapsedStatus() {
  if (state.elapsedTimer) window.clearInterval(state.elapsedTimer);
  state.elapsedTimer = null;
  state.elapsedBase = '';
}

export function stopRunWatchdog() {
  if (state.runWatchdogTimer) window.clearInterval(state.runWatchdogTimer);
  state.runWatchdogTimer = null;
  if (state.runWaitTimer) window.clearInterval(state.runWaitTimer);
  state.runWaitTimer = null;
  state.runProbeMisses = 0;
}

export function startRunWatchdog() {
  stopRunWatchdog();
  state.runWatchdogTimer = window.setInterval(runWatchdogTick, RUN_WATCHDOG_INTERVAL);
  // 轻量等待计时：每秒检查“长时间无新进展”，用于显示“等待中 · 已等待 X 秒”。
  state.runWaitTimer = window.setInterval(runWaitTick, 1000);
}

// 无进展等待提示：run 活跃但长时间没有真实内容进展（delta/reasoning/tool/status）
// 时显示“等待中 · 已等待 X 秒”，让用户明白“还在工作而非卡死”。
// 用 runContentActivityAt（不含 heartbeat）作为依据，后台心跳不会重置计数；
// 一有真实进展事件，runContentActivityAt 被刷新，本逻辑自动恢复展示前文本。
export function runWaitTick() {
  if (!state.abortController || !state.runContentActivityAt) return;
  if (state.connectionState === 'reconnecting') return;      // 重连中已单独提示
  if (state.elapsedBase) return;                             // 已有思考/工具计时在展示，不重复
  if (!state.chatRunId && !state.runConversationId) return;
  const idle = Date.now() - state.runContentActivityAt;
  const el = $('#runtimeStatus');
  if (idle >= RUN_WAIT_STATUS_IDLE) {
    if (!state.runWaitShown) {
      state.runWaitShown = true;
      state.runWaitPrevText = el ? el.textContent : '';
    }
    const waitSeconds = Math.max(1, Math.floor(idle / 1000));
    if (el) el.textContent = `等待中 · 已等待 ${waitSeconds} 秒`;
  } else if (state.runWaitShown) {
    // 有进展了，还原此前展示（或回到就绪）。
    state.runWaitShown = false;
    if (el) el.textContent = state.runWaitPrevText || (state.chatRunId ? '正在处理' : '就绪');
    state.runWaitPrevText = '';
  }
}

// 看门狗：检测“流既不推数据也不报错”的死流，并用真实探针区分“流死/服死”。
export function runWatchdogTick() {
  if (state.cancelRequested) return;
  if (!state.abortController || !state.runLastActivityAt) return;
  if (state.runRecovering) return;
  // 冷却期内不行动，避免风暴
  if (state.runReconnectAt && Date.now() < state.runReconnectAt) return;
  if (!state.runConversationId && !state.conversationId) return;
  const idle = Date.now() - state.runLastActivityAt;
  if (idle < RUN_WATCHDOG_IDLE) {
    state.runProbeMisses = 0;
    setConnectionState('connected');
    return;
  }
  state.runProbeMisses += 1;
  if (state.runProbeMisses < RUN_WATCHDOG_PROBE_MAX) return;
  state.runProbeMisses = 0;
  void probeAndRecoverRun();
}

export async function probeAndRecoverRun() {
  if (state.cancelRequested) return;
  const controller = state.abortController;
  const conversationId = state.runConversationId || state.conversationId;
  const runId = state.chatRunId;
  const generation = state.runGeneration;
  if (!controller || !runId || !conversationId) return;
  if (!isRunGenerationActive(generation, controller)) return;
  state.runRecovering = true;
  try {
    // 真实探针：loadTasks 能否成功 → 区分“流死 server 活”与“server 死”。
    await api('/api/tasks');
    if (!isRunGenerationActive(generation, controller)) return;
    console.warn('[naiba] 看门狗：流空闲超限但服务存活，重启流续传 run=', runId);
    if (state.conversationId !== conversationId) return;
    void resumeRun({ id: runId, conversation_id: conversationId }, { fromWatchdog: true });
  } catch (error) {
    // server 死 → 进入带冷却的保守重连，等待轮询兜底
    if (!isRunGenerationActive(generation, controller)) return;
    console.warn('[naiba] 看门狗：检测到服务不可达，进入冷却重连');
    enterReconnectCoolDown(true);
  } finally {
    state.runRecovering = false;
  }
}

export function enterReconnectCoolDown(showTimer = true) {
  setConnectionState('reconnecting');
  state.runReconnectAt = Date.now() + RUN_RECONNECT_COOLDOWN;
  if (showTimer) showElapsedStatus('重连中…');
}

// 断线自动重连：非 AbortError 且流仍当前时，指数退避 + 抖动后重建流。
export function scheduleRunReconnect(run, controller, generation) {
  if (state.cancelRequested || state.cancelledRunIds.has(String(run?.id || ''))) return;
  if (!run?.id) return;
  if (!isRunGenerationActive(generation, controller)) return;
  state.runAttempt += 1;
  if (state.runAttempt > RUN_RECONNECT_ATTEMPTS) {
    // 超限：解绑当前（已死）控制器，交还轮询兜底，避免状态永远“活动中”却无法被兜底。
    detachRunConnection();
    state.checkRunEligible = true;
    enterReconnectCoolDown(true);
    return;
  }
  setConnectionState('reconnecting');
  state.checkRunEligible = true;
  const delay = backoffDelay(state.runAttempt);
  showElapsedStatus('重连中…');
  const timer = window.setTimeout(() => {
    state.runReconnectTimers.delete(timer);
    if (state.cancelRequested || state.cancelledRunIds.has(String(run.id))) return;
    if (!isRunGenerationActive(generation, controller)) return;
    // resumeRun 内部会自增 runGeneration，建立新一代连接
    void resumeRun(run, { fromReconnect: true, generation });
  }, delay);
  state.runReconnectTimers.add(timer);
}

export function clearVisionProgress() {
  if (state.visionTimer) window.clearInterval(state.visionTimer);
  state.visionTimer = null;
  state.visionStartedAt = 0;
}

export function clearStreamingAnswer(answer) {
  if (!answer) return;
  answer.dataset.raw = '';
  answer.dataset.renderScheduled = '0';
  answer.replaceChildren();
}

export function createStreamingReasoningBlock(answer) {
  const block = document.createElement('details');
  block.className = 'reasoning-block';
  block.open = true;
  block.dataset.streaming = 'true';
  block.dataset.active = 'true';
  block.innerHTML = '<summary>Thinking</summary><div class="reasoning-content"></div>';
  answer.before(block);
  return block;
}

export function collapseToolReasoningBlock() {
  // 工具调用步骤的思考：坍缩为单行摘要，可点击展开（tool_start 到来时调用）。
  const block = state.streamingReasoningBlock;
  if (!block) return;
  const content = block.querySelector('.reasoning-content');
  block.open = false;
  block.classList.add('tool-reasoning');
  block.dataset.tool = 'true';
  const summary = block.querySelector('summary');
  const preview = (content?.dataset.raw || '').trim().replace(/\s+/g, ' ').slice(0, 80);
  if (summary) summary.textContent = preview ? `工具思考：${preview}…` : '工具思考';
  state.streamingReasoningBlock = null;
}

export function createRunRow(run) {
  const row = messageElement({ role: 'assistant', content: '' }, true);
  row.dataset.runId = String(run.id || '');
  row.dataset.runKind = String(run.kind || 'chat');
  row.dataset.lightweightMode = String(state.lightweightMode);
  $('#messages').append(row);
  state.runRow = row;
  scrollToBottom();
  return row;
}

export async function consumeRunStream(response, row, conversationId, runId, controller, generation = state.runGeneration) {
  state.runLastActivityAt = Date.now();
  state.runContentActivityAt = Date.now();
  startRunWatchdog();
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const { value, done } = await reader.read();
      // 任何字节（含 heartbeat）都算活跃，防止心跳流被误判死流
      state.runLastActivityAt = Date.now();
      // 代际变了（旧流被 detach/重连取代）则立即中止本消费
      if (!isRunGenerationActive(generation, controller)) break;
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) {
        if (!line.trim()) continue;
        let event;
        try {
          event = JSON.parse(line);
        } catch (_) {
          continue; // 单行解析失败不致命，跳过
        }
        if (event.type === 'heartbeat') continue;
        if (state.cancelRequested || state.cancelledRunIds.has(String(event.run_id || runId || ''))) break;
        // 真实内容事件（非 heartbeat）→ 更新“内容进展”时间，供“等待中”计时使用。
        state.runContentActivityAt = Date.now();
        const eventRunId = String(event.run_id || runId || state.chatRunId || '');
        if (eventRunId) {
          const cached = state.runEvents[eventRunId] || [];
          cached.push(event);
          state.runEvents[eventRunId] = cached.slice(-2000);
        }
        state.runSequence = Math.max(state.runSequence, Number(event.sequence || 0));
        handleChatEvent(event, row, conversationId, runId);
        if (eventRunId && ['done', 'error', 'cancelled'].includes(event.type)) {
          delete state.runEvents[eventRunId];
        }
      }
      if (done) break;
    }
    if (buffer.trim()) {
      let event;
      try {
        event = JSON.parse(buffer);
      } catch (_) {
        event = null;
      }
      if (event && event.type !== 'heartbeat') handleChatEvent(event, row, conversationId, runId);
    }
  } finally {
    // 消费循环正常结束（done/代际失效/被 abort）都停止看门狗；finishRunSubscription 还会再兜底清一次。
    if (state.runWatchdogTimer && state.abortController === controller) stopRunWatchdog();
  }
}

export async function finishRunSubscription(conversationId, controller) {
  if (state.abortController === controller) {
    stopRunWatchdog();
    clearElapsedStatus();
    state.runWaitShown = false;
    state.runWaitPrevText = '';
    state.abortController = null;
    state.chatRunId = '';
    state.runConversationId = '';
    state.runSequence = 0;
    state.runRow = null;
    state.runAttempt = 0;
    state.runReconnectAt = 0;
    state.runProbeMisses = 0;
    state.checkRunEligible = false;
    setConnectionState('connected');
    setBusy(false);
  }
  await loadTasks();
  if (state.conversationId === conversationId && !state.abortController) {
    await loadConversations();
    if (state.conversationId === conversationId) await openConversation(conversationId);
  }
}

export async function resumeRun(run, options = {}) {
  const conversationId = String(run?.conversation_id || '');
  const runId = String(run?.id || '');
  if (!runId || conversationId !== state.conversationId) return;
  if (state.cancelRequested || state.cancelledRunIds.has(runId)) return;
  const sameRun = state.chatRunId === runId && state.runConversationId === conversationId;
  detachRunConnection();
  const generation = state.runGeneration;
  let row = sameRun ? state.runRow : null;
  if (!row?.isConnected) row = createRunRow(run);
  const controller = new AbortController();
  state.abortController = controller;
  state.chatRunId = runId;
  state.runConversationId = conversationId;
  if (!sameRun) state.runSequence = 0;
  setBusy(true);
  void (async () => {
    let reconnectScheduled = false;
    try {
      const response = await fetchRunEvents(runId, controller);
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error || `HTTP ${response.status}`);
      }
      setConnectionState('connected');
      await consumeRunStream(response, row, conversationId, runId, controller, generation);
    } catch (error) {
      if (error.name !== 'AbortError' && !state.cancelRequested && state.conversationId === conversationId) {
        row.querySelector('.answer-content').innerHTML = `<p>恢复 Run 失败：${escapeHtml(error.message)}</p>`;
      }
      if (error.name !== 'AbortError' && isRunGenerationActive(generation, controller)) {
        // 断线自动重连（指数退避 + 抖动 + 超限交还轮询）。
        // 已排程重连时，状态由 reconnect 计时器/轮询接管，不再 finishRunSubscription 清空。
        reconnectScheduled = true;
        scheduleRunReconnect({ id: runId, conversation_id: conversationId }, controller, generation);
      }
    } finally {
      // 正常结束或被用户 abort（代际失效）时统一收尾；重连接管时不重复清空。
      if (!reconnectScheduled && state.abortController === controller) {
        await finishRunSubscription(conversationId, controller);
      }
    }
  })();
}

export async function resumeConversationRun(conversationId) {
  if (!conversationId || conversationId !== state.conversationId || state.abortController || state.cancelRequested) return;
  try {
    const result = await api(`/api/runs?conversation_id=${encodeURIComponent(conversationId)}&active_only=1`);
    if (conversationId !== state.conversationId || state.abortController || state.cancelRequested) return;
    const run = (result.runs || [])[0];
    if (run) await resumeRun(run);
    else setBusy(false);
  } catch (error) {
    console.debug('[naiba] Run 恢复失败:', error.message);
  }
}

export async function sendChatMessage(textOverride = '') {
  const input = $('#messageInput');
  const inputText = String(input.value || '').trim();
  const buttonText = String(textOverride || '').trim();
  // 点击按钮发送时，把按钮附带的内容与输入框已有内容合并，避免丢失预设（如 agent 预设的 skill 引用）。
  const text = buttonText
    ? (inputText ? `${inputText}\n${buttonText}` : buttonText)
    : inputText;
  if (!text || state.taskSubmitting || state.cancelRequested) return;
  if (state.pendingFiles.some((file) => file.uploading)) {
    toast('请等待文件上传完成');
    return;
  }
  if (!state.conversationId) await createConversation();
  if (state.chatRunId || state.abortController) {
    toast('回复进行中，请等待完成或先点击停止');
    return;
  }
  // 用户新发起一轮：恢复跟随，让新答复从底部开始流式显示。
  setStickToBottom(true);
  hideChoiceButtons();
  const referencedIds = parseSkillReferences(text).map((tok) => tok.skill.id);
  const messageText = stripSkillReferences(text);
  const conversationId = state.conversationId;
  const attachments = state.pendingFiles.map(({ name, path, size, thumb_path }) => ({ name, path, size, thumb_path }));
  state.pendingFiles = [];
  renderPendingFiles();
  input.value = '';
  resizeTextarea();
  renderInputMirror();
  hideSkillPopup();
  if ($('#emptyState')) $('#emptyState').hidden = true;
  $('#messages').append(messageElement({ role: 'user', content: messageText, metadata: { attachments, display_content: text } }));
  const row = createRunRow({ id: '', kind: 'chat' });
  const controller = new AbortController();
  const runGeneration = ++state.runGeneration; // 本段对话流的新一代
  state.abortController = controller;
  state.runConversationId = conversationId;
  state.runSequence = 0;
  state.cancelRequested = false;
  state.cancelConversationId = '';
  setBusy(true);
  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${state.token}` },
      body: JSON.stringify({
        conversation_id: conversationId,
        message: messageText,
        display_message: text,
        attachments,
        model_key: $('#modelSelect').value,
        skill_policy: {
          mode: 'exclusive',
          referenced_ids: referencedIds,
        },
        web_search_enabled: state.webSearchEnabled,
      }),
      signal: controller.signal,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      if (response.status === 409 && payload.active_run_id) {
        detachRunSubscription();
        if (state.conversationId === conversationId) await openConversation(conversationId);
        toast('当前对话已有 Run，已恢复其进度');
        return;
      }
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    await consumeRunStream(response, row, conversationId, state.chatRunId, controller, runGeneration);
  } catch (error) {
    if (error.name !== 'AbortError' && state.conversationId === conversationId) {
      row.querySelector('.answer-content').innerHTML = `<p>请求失败：${escapeHtml(error.message)}</p>`;
    }
  } finally {
    if (state.abortController === controller) await finishRunSubscription(conversationId, controller);
  }
}

export const SKILL_INSTALL_PRESET =
  '用户希望在本应用内通过你安装一个 Skill。本会话已为你启用 install_skill / unpack_skill_archive（以及读取/编辑/写入文件）工具。'
  + '请按以下流程执行，并【先等待用户给出具体指令】：\n'
  + '1. 等待用户说明要安装来源。来源只支持：本地文件夹、单个 .md 文件、或一个 .zip 压缩包（rar/7z 暂不支持，提醒用户先转成 zip）。\n'
  + '2. 拿到来源后：\n'
  + '   - 文件夹：直接用 read_file 确认其顶层或下一级存在 SKILL.md；\n'
  + '   - 单个 .md：直接用 read_file 读取；\n'
  + '   - 压缩包：先调 unpack_skill_archive{archive_path}（后端会做强校验并解压到工作区 .skill_incoming），再用 read_file 确认解压出的 SKILL.md。\n'
  + '3. 校验 SKILL.md：确认它能被识别为 Skill——必须包含 YAML frontmatter，且同时有 name 与 description。若不合法（缺 frontmatter、缺 name/description、格式错误），用 edit_file/write_file 帮用户修正后再继续。\n'
  + '4. 安装：\n'
  + '   - 文件夹/解压后的文件夹 → install_skill{source_path: <该文件夹绝对路径>}；\n'
  + '   - 单个 .md → install_skill{source_path: <该 .md 绝对路径>}。\n'
  + '5. 安装成功后：清理工作区 .skill_incoming 下的临时解压目录（unpack_skill_archive 留下的那个），并告知用户该 Skill 已安装、如何再次使用（可通过 /技能名 引用）。\n'
  + '6. 若用户给的来源不是有效的 Skill（无合法 SKILL.md 或不是上述类型），不要强行安装，向用户说明并请其提供正确的来源。';

