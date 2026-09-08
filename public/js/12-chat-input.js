// ============================================================
// 12-chat-input.js —— 拆分自 public/app.js 第 5238-6068 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, $$, api, escapeHtml, state, toast } from "./01-core.js";
import { markdown } from "./02-markdown.js";
import { updateContextComposerLock, updateContextUsage, usageMarkup } from "./03-media.js";
import { getStreamingProseSegment, messageElement, moveBottomProseInline, refreshFirstTurnCard, renderMessages, scheduleStreamingMarkdown, scrollToBottom } from "./04-messages.js";
import { loadTasks } from "./06-tasks-plans.js";
import { updateUnloadModelButton } from "./07-models-agents.js";
import { createConversation, openConversation, renderConversationRuleBar } from "./08-conversations.js";
import { uploadFiles } from "./10-upload.js";
import { SKILL_INSTALL_PRESET, clearElapsedStatus, clearRunReconnectTimers, clearVisionProgress, collapseToolReasoningBlock, createStreamingReasoningBlock, detachRunConnection, sendChatMessage, setConnectionState, stopRunWatchdog } from "./11-run-stream.js";
import { renderInputMirror, resizeTextarea, updateSkillPopup } from "./13-skill-refs.js";
export async function startSkillInstall() {
  if (state.chatRunId || state.abortController) {
    toast('请先等待当前任务结束或停止后再安装 Skill');
    return;
  }
  if (!state.conversationId) await createConversation();
  const cid = state.conversationId;
  try {
    const result = await api(`/api/conversations/${cid}/tools`, {
      method: 'POST',
      body: { tools: ['install_skill', 'unpack_skill_archive', 'read_file', 'edit_file', 'write_file'] },
    });
    toast(`已启用 Skill 安装工具${result.added?.length ? `（新增 ${result.added.length} 个）` : ''}`);
  } catch (error) {
    toast(`启用安装工具失败：${error.message}`);
    return;
  }
  sendMessage(SKILL_INSTALL_PRESET);
}

// ---- 自定义指令（开始新对话页的“+”按钮）：固化到用户 config，可快速复用 ----
export async function loadStarterPrompts() {
  try {
    const r = await api('/api/starter-prompts');
    state.customPrompts = Array.isArray(r.prompts) ? r.prompts : [];
    renderStarterPrompts();
  } catch (error) {
    state.customPrompts = [];
  }
}

export function renderStarterPrompts() {
  const grid = document.querySelector('.starter-grid');
  const addBtn = $('#starterAddBtn');
  if (!grid || !addBtn) return;
  grid.querySelectorAll('.custom-starter').forEach((el) => el.remove());
  state.customPrompts.forEach((p, i) => {
    if (!p || !p.text) return;
    const wrap = document.createElement('div');
    wrap.className = 'custom-starter';
    const main = document.createElement('button');
    main.type = 'button';
    main.title = `点击复用：${p.title || '自定义指令'}`;
    main.innerHTML = `<span class="starter-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"></path></svg></span><span class="starter-title">${escapeHtml(p.title || '自定义指令')}</span><span class="starter-desc">自定义指令</span>`;
    main.addEventListener('click', () => sendMessage(p.text));
    const edit = document.createElement('button');
    edit.type = 'button';
    edit.className = 'starter-edit';
    edit.title = '编辑此指令';
    edit.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4L19.5 8.5a2.12 2.12 0 0 0-3-3L5 17l-1 4Z"></path><path d="M13.5 6.5l3 3"></path></svg>';
    edit.addEventListener('click', (e) => { e.stopPropagation(); openStarterPromptDialog(i); });
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'starter-del';
    del.title = '删除此指令';
    del.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg>';
    del.addEventListener('click', (e) => { e.stopPropagation(); removeStarterPrompt(i); });
    wrap.appendChild(main);
    wrap.appendChild(edit);
    wrap.appendChild(del);
    grid.insertBefore(wrap, addBtn);
  });
}

export function openStarterPromptDialog(index = -1) {
  state.editingStarterPrompt = index;
  const p = (index >= 0 ? state.customPrompts[index] : null) || {};
  $('#starterPromptTitle').value = p.title || '';
  $('#starterPromptText').value = p.text || '';
  $('#starterPromptDialog').showModal();
  $('#starterPromptTitle').focus();
}

export async function saveStarterPrompt() {
  const title = $('#starterPromptTitle').value;
  const text = $('#starterPromptText').value;
  if (!text.trim()) { toast('指令内容不能为空'); return; }
  const editing = state.editingStarterPrompt;
  try {
    const url = editing >= 0 ? `/api/starter-prompts/${editing}` : '/api/starter-prompts';
    const r = await api(url, { method: 'POST', body: { title, text } });
    state.customPrompts = r.prompts || [];
    state.editingStarterPrompt = -1;
    renderStarterPrompts();
    $('#starterPromptDialog').close();
    toast(editing >= 0 ? '已更新自定义指令' : '已保存自定义指令');
  } catch (error) {
    toast(`保存失败：${error.message}`);
  }
}

export async function removeStarterPrompt(index) {
  try {
    const r = await api(`/api/starter-prompts/${index}`, { method: 'DELETE' });
    state.customPrompts = r.prompts || [];
    renderStarterPrompts();
    toast('已删除自定义指令');
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

export const SKILL_EDIT_PRESET =
  '用户希望编辑本应用内一个已安装的 Skill。本会话已为你启用 inspect_installed_skill（以及读取/编辑/写入文件）工具。'
  + '请按以下流程执行，并【先等待用户指定要编辑哪个 Skill】：\n'
  + '1. 等待用户给出目标 Skill（支持名称或 id）。\n'
  + '2. 调用 inspect_installed_skill{skill: <名称或id>} 拿到该 Skill 的 path（SKILL.md）与 root（所在目录）。\n'
  + '3. 用 read_file 读取 SKILL.md 及其相关脚本/资源，向用户概述当前内容。\n'
  + '4. 按用户要求，用 edit_file/write_file 修改 SKILL.md、描述、脚本等；修改前可先与用户确认改动点，改完说明改了什么。\n'
  + '5. 提醒用户：改动会持久化到该 Skill 文件；切换/重开会话或重新引用（/技能名）后生效。\n'
  + '6. 若用户给的 Skill 不存在（inspect_installed_skill 返回未找到），向用户说明可用的 Skill，不要凭空编造。';

export async function startSkillEdit() {
  if (state.chatRunId || state.abortController) {
    toast('请先等待当前任务结束或停止后再编辑 Skill');
    return;
  }
  if (!state.conversationId) await createConversation();
  const cid = state.conversationId;
  try {
    const result = await api(`/api/conversations/${cid}/tools`, {
      method: 'POST',
      body: { tools: ['inspect_installed_skill', 'read_file', 'edit_file', 'write_file'] },
    });
    toast(`已启用编辑 Skill 工具${result.added?.length ? `（新增 ${result.added.length} 个）` : ''}`);
  } catch (error) {
    toast(`启用编辑工具失败：${error.message}`);
    return;
  }
  sendMessage(SKILL_EDIT_PRESET);
}

export async function sendMessage(textOverride = '') {
  await sendChatMessage(textOverride);
}

export function updateDeepReasoningButton() {
  const btn = $('#deepReasoningButton');
  if (!btn) return;
  const disabled = Boolean(state.chatRunId || state.abortController);
  btn.disabled = disabled;
  const effort = state.reasoningEffort || (state.deepReasoningEnabled ? 'medium' : 'auto');
  const auto = effort === 'auto';
  const active = auto || effort !== 'off';
  btn.classList.toggle('active', active);
  btn.dataset.reasoningEffort = effort;
  btn.setAttribute('aria-pressed', String(active));
  btn.title = auto ? '深度思考：跟随 API（自动）' : (effort !== 'off' ? '深度思考：开启' : '深度思考：关闭');
}

export async function toggleDeepReasoning() {
  if (!state.conversationId) await createConversation();
  if (state.chatRunId || state.abortController) return;
  const menu = $('#reasoningMenu');
  if (menu) {
    menu.hidden = !menu.hidden;
    if (!menu.hidden) return;
  }
  const levels = ['auto', 'off', 'low', 'medium', 'high'];
  const previous = state.reasoningEffort || (state.deepReasoningEnabled ? 'medium' : 'auto');
  const next = levels[(levels.indexOf(previous) + 1) % levels.length];
  state.reasoningEffort = next;
  state.deepReasoningEnabled = next !== 'off';
  updateDeepReasoningButton();
  try {
    const updated = await api(`/api/conversations/${state.conversationId}/settings`, {
      method: 'POST',
      body: { deep_reasoning_enabled: state.deepReasoningEnabled, reasoning_effort: next },
    });
    const index = state.conversations.findIndex((item) => item.id === state.conversationId);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
    if (next === 'auto') toast('思考强度：跟随 API（自动，本对话）');
    else toast(state.deepReasoningEnabled ? `深度思考已开启（${next}，本对话）` : '深度思考已关闭（本对话）');
  } catch (error) {
    state.reasoningEffort = previous;
    state.deepReasoningEnabled = previous !== 'off';
    updateDeepReasoningButton();
    toast(`深度思考设置保存失败：${error.message}`);
  }
}

export function applyConversationLightweight(conversation) {
  // 轻量模式只能由下方“工具 / Skill”选项开启：勾选即关闭对应能力。
  // 若会话从未开启过轻量模式（lightweight_mode=0），忽略旧版本残留的关闭项，
  // 保证默认回到普通模式（什么都不勾选 = 普通对话）。
  const enabled = Boolean(Number(conversation?.lightweight_mode || 0));
  const stored = Array.isArray(conversation?.lightweight_disabled_features)
    ? conversation.lightweight_disabled_features.filter((item) => item === 'tools' || item === 'skills' || item === 'rich_text')
    : [];
  state.lightweightDisabledFeatures = enabled ? stored : [];
  state.lightweightMode = state.lightweightDisabledFeatures.length > 0;
  state.richTextEnabled = !state.lightweightDisabledFeatures.includes('rich_text');
  updateLightweightModeControl();
}

export function updateLightweightModeControl() {
  const toolsToggle = $('#lightweightToolsToggle');
  const skillsToggle = $('#lightweightSkillsToggle');
  const richTextToggle = $('#richTextToggle');
  const attach = $('#attachButton');
  const disabled = new Set(state.lightweightDisabledFeatures || []);
  for (const [input, key] of [[toolsToggle, 'tools'], [skillsToggle, 'skills']]) {
    if (!input) continue;
    input.checked = disabled.has(key);
    input.disabled = Boolean(state.chatRunId || state.abortController);
  }
  if (richTextToggle) { richTextToggle.checked = state.richTextEnabled; richTextToggle.disabled = Boolean(state.chatRunId || state.abortController); }
  if (attach) attach.disabled = false;
  updateDeepReasoningButton();
}
export async function toggleRichText(checked) {
  if (state.chatRunId || state.abortController) return; if (!state.conversationId) await createConversation();
  const previous = [...state.lightweightDisabledFeatures], previousEnabled = state.richTextEnabled; const next = new Set(previous);
  if (checked) next.delete('rich_text'); else next.add('rich_text'); state.richTextEnabled = !!checked; state.lightweightDisabledFeatures = [...next]; state.lightweightMode = next.size > 0; updateLightweightModeControl();
  try { const updated = await api(`/api/conversations/${state.conversationId}/settings`, { method:'POST', body:{lightweight_mode:state.lightweightMode, lightweight_disabled_features:state.lightweightDisabledFeatures} }); state.lightweightDisabledFeatures = updated.lightweight_disabled_features || state.lightweightDisabledFeatures; state.richTextEnabled = !state.lightweightDisabledFeatures.includes('rich_text'); state.lightweightMode = state.lightweightDisabledFeatures.length > 0; const current = state.conversations.find((item) => item.id === state.conversationId); if (Array.isArray(current?.messages)) renderMessages(current.messages); }
  catch (error) { state.lightweightDisabledFeatures = previous; state.richTextEnabled = previousEnabled; state.lightweightMode = previous.length > 0; updateLightweightModeControl(); toast(`富文本设置保存失败：${error.message}`); }
}

export function markdownFilePreview(text) {
  return markdown(text, false);
}

export async function toggleLightweightFeature(feature, checked) {
  if (!['tools', 'skills'].includes(feature) || state.chatRunId || state.abortController) return;
  if (!state.conversationId) await createConversation();
  const previous = [...state.lightweightDisabledFeatures];
  const previousMode = state.lightweightMode;
  const next = new Set(previous);
  // 直接使用用户本次勾选意图；不要在 await 之后重新读取 DOM（新会话创建会重置勾选框）。
  if (checked) next.add(feature); else next.delete(feature);
  state.lightweightDisabledFeatures = [...next];
  state.lightweightMode = state.lightweightDisabledFeatures.length > 0;
  updateLightweightModeControl();
  try {
    const updated = await api(`/api/conversations/${state.conversationId}/settings`, {
      method: 'POST', body: {
        lightweight_mode: state.lightweightMode,
        lightweight_disabled_features: state.lightweightDisabledFeatures,
      },
    });
    state.lightweightDisabledFeatures = updated.lightweight_disabled_features || state.lightweightDisabledFeatures;
    state.lightweightMode = state.lightweightDisabledFeatures.length > 0;
    const index = state.conversations.findIndex((item) => item.id === state.conversationId);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
  } catch (error) {
    state.lightweightDisabledFeatures = previous;
    state.lightweightMode = previousMode;
    updateLightweightModeControl();
    toast(`轻量对话选项保存失败：${error.message}`);
  }
}

export async function handlePasteImage(event) {
  const items = (event.clipboardData && event.clipboardData.items) || [];
  const imageFiles = [];
  for (const item of items) {
    if (item.type && item.type.startsWith('image/') && item.kind === 'file') {
      const file = item.getAsFile();
      if (file) imageFiles.push(file);
    }
  }
  if (!imageFiles.length) return;
  event.preventDefault();
  if (!state.conversationId) await createConversation();
  await uploadFiles(imageFiles);
  toast('已粘贴图片，可发送');
}

// ---- Run 事件流渲染：类型路由表（阶段 3：巨型 if-else → type→handler 表）----
// 约定：handler 返回 false 表示“本事件不触发滚动”（原 debug_cache/reasoning_delta
// 空内容分支的 return 语义）；其余事件按原实现在分派后统一滚动到底部。
const CHAT_EVENT_HANDLERS = {
  debug_cache: handleDebugCacheEvent,
  run_started: handleRunStartedEvent,
  status: handleStatusEvent,
  skills: handleSkillsEvent,
  skill_warning: handleSkillWarningEvent,
  tools_available: handleToolsAvailableEvent,
  delta: handleDeltaEvent,
  reasoning_start: handleReasoningStartEvent,
  reasoning_delta: handleReasoningDeltaEvent,
  reasoning_end: handleReasoningEndEvent,
  reasoning: handleReasoningEvent,
  tool_start: handleToolStartEvent,
  tool_result: handleToolResultEvent,
  tool_confirm: handleToolConfirmEvent,
  choice: handleChoiceEvent,
  cancelled: handleCancelledEvent,
  run_failed: handleRunFailedEvent,
  context_full: handleContextFullEvent,
  done: handleDoneEvent,
  error: handleErrorEvent,
  usage: handleUsageEvent,
};

export function handleChatEvent(event, row, conversationId = state.conversationId, runId = state.chatRunId) {
  if (conversationId !== state.conversationId) return;
  if (state.cancelRequested || state.cancelledRunIds.has(String(event.run_id || runId || ''))) return;
  const answer = row.querySelector('.answer-content');
  const activity = row.querySelector('.run-activity');
  const setActivity = (content, html = false) => {
    if (!activity) return;
    activity.hidden = !content;
    if (html) activity.innerHTML = content;
    else activity.textContent = content || '';
  };
  const collapseReasoning = () => {
    row.querySelectorAll('.reasoning-block').forEach((block) => { block.open = false; });
  };
  const handler = CHAT_EVENT_HANDLERS[String(event.type || '')];
  if (!handler) {
    // 未知类型：可观测（console）而非静默；原实现未知事件同样直接落到末尾滚动。
    console.debug('[naiba] 未识别的运行事件类型:', String(event.type || ''));
    scrollToBottom();
    return;
  }
  if (handler(event, { row, answer, setActivity, collapseReasoning, conversationId, runId }) !== false) {
    scrollToBottom();
  }
}

function handleDebugCacheEvent(event) {
  // 缓存诊断（NAIBA_DEBUG_CACHE=1 时由后端推送）：逐条 [索引:角色:字节数:哈希]
  console.groupCollapsed(`[CACHE] ${event.label || ''}`);
  (event.lines || []).forEach((line) => console.log(line));
  console.groupEnd();
  window.__CACHE_DEBUG__ ??= [];
  window.__CACHE_DEBUG__.push({ label: event.label || '', lines: event.lines || [] });
  return false;
}

function handleRunStartedEvent(event, { row, conversationId }) {
  state.chatRunId = String(event.run_id || '');
  state.runConversationId = conversationId;
  row.dataset.runId = state.chatRunId;
  row.dataset.lightweightMode = String(Boolean(event.lightweight_mode));
}

function handleStatusEvent(event, { setActivity }) {
  clearVisionProgress();
  const statusMessage = String(event.message || '');
  setActivity(statusMessage);
  // 思考等待计时：显示 “正在思考 … · 已等待 X 秒”，收到进展事件即清除
  if (state.elapsedTimer) clearElapsedStatus();
  state.elapsedBase = statusMessage || '正在思考';
  state.elapsedSince = Date.now();
  const tick = () => {
    const seconds = Math.max(0, Math.floor((Date.now() - state.elapsedSince) / 1000));
    const el = $('#runtimeStatus');
    if (el) el.textContent = `${state.elapsedBase} · 已等待 ${seconds} 秒`;
  };
  tick();
  state.elapsedTimer = window.setInterval(tick, 1000);
}

function handleSkillsEvent(event, { setActivity }) {
  const user = (event.skills || []).filter((s) => s?.source !== 'auto');
  const auto = (event.skills || []).filter((s) => s?.source === 'auto');
  const parts = [];
  if (user.length) parts.push(`已启用 ${user.map((s) => s?.name).join('、')}`);
  if (auto.length) parts.push(`已自动匹配 ${auto.map((s) => s?.name).join('、')}`);
  setActivity(parts.join('；'));
}

function handleSkillWarningEvent(event, { setActivity }) {
  const warning = String(event.message || '本次引用的技能体积较大，已完整注入但可能影响响应速度');
  toast(warning);
  setActivity(warning);
}

function handleToolsAvailableEvent() {
  // Tool schemas are runtime state, not user-facing message content.
  // Keep tool execution/result details available without dumping the full
  // capability list into every response.
}

function handleDeltaEvent(event, { row, answer, setActivity }) {
  clearElapsedStatus();
  setActivity('');
  const content = String(event.content || '');
  if (row.dataset.lightweightMode === 'true') {
    const current = answer.dataset.raw || '';
    const next = current + content;
    answer.dataset.raw = next;
    answer.textContent = next;
    scrollToBottom();
  } else if (row.dataset.sawTool === 'true') {
    // 已出现工具：中途正文插入事件流（与思考/工具块按时间交错），不再全部堆到底部。
    const seg = getStreamingProseSegment(row, answer);
    if (seg) scheduleStreamingMarkdown(seg, (seg.dataset.raw || '') + content);
  } else {
    // 尚无工具：正文即整段回复，累积到底部（避免正文跑到思考前面的倒序）。
    const current = answer.dataset.raw || '';
    const next = current + content;
    answer.dataset.raw = next;
    scheduleStreamingMarkdown(answer, next);
  }
}

function handleReasoningStartEvent(event, { row }) {
  clearElapsedStatus();
  state.streamingReasoningBlock = null;
  row.querySelectorAll('.reasoning-block[data-active="true"]').forEach((block) => {
    block.dataset.active = 'false';
    if (!(block.querySelector('.reasoning-content')?.dataset.raw || '').trim()) block.remove();
  });
}

// ---- 流式渲染的模型请求轮次断点：每次新请求（usage 事件边界后）的第一个块左侧画「·」 ----
// row.dataset.requestsDone = 已完成的请求数（usage 事件更新）；requestsMarked = 已标记起点数。
function markRequestStart(row, block) {
  if (!row || !block) return;
  const done = Number(row.dataset.requestsDone || 0);
  const marked = Number(row.dataset.requestsMarked || 0);
  if (marked < done + 1) {
    row.dataset.requestsMarked = String(marked + 1);
    block.classList.add('request-start');
  }
}

function handleReasoningDeltaEvent(event, { row, answer }) {
  if (!String(event.content || '').trim()) return false;
  let block = row.querySelector('.reasoning-block[data-active="true"]');
  if (!block) {
    block = createStreamingReasoningBlock(answer);
    markRequestStart(row, block);
  }
  state.streamingReasoningBlock = block;
  const content = block.querySelector('.reasoning-content');
  scheduleStreamingMarkdown(content, (content.dataset.raw || '') + String(event.content || ''));
  row.dataset.reasoningStreamed = 'true';
}

function handleReasoningEndEvent() {
  // 工具 vs 正式的分类不在此处做（正文 delta 无法可靠区分：模型可能在工具前
  // 先叙说一句）。这里保持展开；接下来若 tool_start 到来，由 collapseToolReasoningBlock
  // 坍缩成单行；若一直无 tool_start（正式回复）则保持展开。
  const block = state.streamingReasoningBlock;
  if (block) {
    block.dataset.active = 'false';
    const content = block.querySelector('.reasoning-content');
    if (!(content?.dataset.raw || '').trim()) block.remove();
  }
}

function handleReasoningEvent(event, { row, answer }) {
  if (row.dataset.reasoningStreamed) return;
  // 实时显示推理内容到可折叠块
  let block = row.querySelector('.reasoning-block');
  if (!block) {
    block = document.createElement('details');
    block.className = 'reasoning-block';
    block.open = true;
    block.innerHTML = '<summary>思考过程</summary><div class="reasoning-content"></div>';
    answer.before(block);
  }
  const content = block.querySelector('.reasoning-content');
  content.innerHTML = markdown((content.dataset.raw || '') + (content.dataset.raw ? '\n\n---\n\n' : '') + event.content);
  content.dataset.raw = (content.dataset.raw || '') + (content.dataset.raw ? '\n\n---\n\n' : '') + event.content;
}

function handleToolStartEvent(event, { row, answer }) {
  clearElapsedStatus();
  // 首个工具出现：把之前累计在底部的正文移到内联块（紧跟该工具前），并切换为“有工具”模式。
  if (row.dataset.sawTool !== 'true') {
    moveBottomProseInline(row, answer);
    row.dataset.sawTool = 'true';
  }
  collapseToolReasoningBlock();
  // 每次工具调用作为一个独立兄弟节点插到 answer 之前，与思考块按时间顺序交错摆放，
  // 而不是全部塞进同一个 .tool-stack（那样会把所有工具挤在一起，破坏与思考块的交错）。
  const details = document.createElement('details');
  details.className = 'tool-run';
  details.open = true;
  const toolArguments = typeof event.arguments === 'string'
    ? event.arguments
    : JSON.stringify(event.arguments || {}, null, 2);
  details.innerHTML = `<summary>Running · ${escapeHtml(event.tool)}${event.reason ? ` · ${escapeHtml(event.reason)}` : ''}</summary><pre>${escapeHtml(toolArguments)}</pre>`;
  markRequestStart(row, details);
  answer.before(details);
  // 让新插入的工具块始终位于末尾（紧贴 answer），从而保持时间顺序。
  scrollToBottom();
}

function handleToolResultEvent(event, { row }) {
  const toolRuns = row.querySelectorAll('.tool-run');
  const last = toolRuns[toolRuns.length - 1];
  if (last) {
    const summary = last.querySelector('summary');
    if (summary) summary.textContent = `${event.success ? 'Completed' : 'Failed'} · ${event.tool}`;
    const toolArguments = typeof event.arguments === 'string'
      ? event.arguments
      : JSON.stringify(event.arguments || {}, null, 2);
    const pre = last.querySelector('pre') || document.createElement('pre');
    pre.textContent = `${toolArguments}\n\n${String(event.result || '')}`;
    if (!pre.parentNode) last.appendChild(pre);
    last.open = false;
  }
}

function handleToolConfirmEvent(event, { row, answer, runId }) {
  clearElapsedStatus();
  if (row.dataset.sawTool !== 'true') {
    moveBottomProseInline(row, answer);
    row.dataset.sawTool = 'true';
  }
  // 同样保留已输出的正式回复，避免在等待确认时被吞掉。
  const confirmId = event.confirm_id;
  const toolName = event.tool_name;
  const toolDesc = event.tool_desc;
  const toolArguments = typeof event.arguments === 'string'
    ? event.arguments
    : JSON.stringify(event.arguments || {}, null, 2);
  const confirmMarkup = `
    <div class="tool-confirm" data-confirm-id="${escapeHtml(confirmId)}">
      <div class="tool-confirm-header">
        <span class="tool-confirm-icon">⚠️</span>
        <span class="tool-confirm-title">需要确认</span>
      </div>
      <div class="tool-confirm-body">
        <div class="tool-confirm-tool">工具：${escapeHtml(toolName)}</div>
        <div class="tool-confirm-desc">${escapeHtml(toolDesc)}</div>
        ${toolArguments ? `<div class="tool-confirm-args"><pre>${escapeHtml(toolArguments)}</pre></div>` : ''}
      </div>
      <div class="tool-confirm-actions">
        <button class="tool-confirm-btn tool-confirm-reject" data-confirm-id="${escapeHtml(confirmId)}" data-run-id="${escapeHtml(event.run_id || runId)}">拒绝</button>
        <button class="tool-confirm-btn tool-confirm-approve" data-confirm-id="${escapeHtml(confirmId)}" data-run-id="${escapeHtml(event.run_id || runId)}">允许执行</button>
      </div>
    </div>`;
  answer.insertAdjacentHTML('beforebegin', confirmMarkup);
  scrollToBottom();
}

function handleChoiceEvent(event) {
  // AI回复包含可选项，显示选择按钮
  showChoiceButtons(event.choices, event.choice_groups);
}

function handleCancelledEvent(event, { row, answer, setActivity, conversationId }) {
  clearElapsedStatus();
  clearVisionProgress();
  setActivity('');
  if (event.aborted_message) {
    // 取消时后端已把累积内容持久化为"已中止"assistant 消息，直接用其渲染，保留已展示的思考与工具。
    try {
      const cancelledRow = messageElement(event.aborted_message);
      row.replaceWith(cancelledRow);
      updateContextUsage(null, event.aborted_message);
    } catch (error) {
      console.error('[naiba] cancelled 事件渲染崩溃:', error, 'message=', event.aborted_message);
    }
  } else {
    // 没有 aborted_message（例如 forced-cancel 未及时重建）：绝不能清空已展示的中途输出，
    // 只在真正无任何内容时才显示占位提示；否则会抹掉 AI 已输出的回复。
    const hasContent = Boolean((answer.dataset.raw || '').trim())
      || row.querySelector('.reasoning-block, .tool-run, .stream-prose, .tool-confirm');
    if (!hasContent) {
      answer.innerHTML = `<p>${escapeHtml(event.message || '任务已取消')}</p>`;
    }
    setActivity(event.message || '任务已取消');
  }
  // 首轮上下文折叠卡：终态事件前后端已把 first_turn 落盘，即时拉取展示（首轮取消同样有上下文可看）。
  if (conversationId) void refreshFirstTurnCard(conversationId);
}

function handleRunFailedEvent(event, { answer, setActivity }) {
  clearElapsedStatus();
  clearVisionProgress();
  setActivity('');
  // 工具协议解析失败：只展示可读错误，不显示原始 XML/JSON 或命令参数。
  answer.innerHTML = `<p>执行失败：${escapeHtml(event.error || '任务执行失败')}</p>`;
  $('#runtimeStatus').textContent = '执行失败';
}

function handleContextFullEvent() {
  // 上下文已达上限：后端已阻止本次请求，立即锁定输入并提示新建对话。
  state.contextAtCeiling = true;
  updateContextComposerLock(Boolean(state.chatBusy));
}

function handleUsageEvent(event, { row, answer }) {
  // 用量框常驻流式消息尾端：首次请求完成时挂载，之后每完成一次请求原位更新
  // （命中率为最后一次请求口径，与终态 metadata.usage 同构）。
  row.dataset.requestsDone = String(Math.max(
    Number(row.dataset.requestsDone || 0),
    Number(event.usage?.requests || 0),
  ));
  let box = answer.nextElementSibling;
  if (!box || !(box.classList && box.classList.contains('usage-stream'))) {
    box = document.createElement('div');
    box.className = 'usage-stream';
    answer.insertAdjacentElement('afterend', box);
  }
  box.innerHTML = usageMarkup(event.usage || {});
}

function handleDoneEvent(event, { row, answer, collapseReasoning, conversationId }) {
  clearElapsedStatus();
  clearVisionProgress();
  collapseReasoning();
  if (event.message) {
    try {
      const completedRow = messageElement(event.message);
      row.replaceWith(completedRow);
      updateContextUsage(null, event.message);
      const metadata = event.message.metadata || {};
      if ((Array.isArray(metadata.choice_groups) && metadata.choice_groups.length)
        || (Array.isArray(metadata.choices) && metadata.choices.length)) {
        showChoiceButtons(metadata.choices, metadata.choice_groups);
      }
    } catch (error) {
      console.error('[naiba] done 事件渲染崩溃:', error, 'message=', event.message);
    }
  } else {
    answer.innerHTML = '<p>计划执行完成</p>';
  }
  $('#runtimeStatus').textContent = '就绪';
  // 首轮上下文折叠卡：后端在终态事件落盘之前已抢先写入 first_turn（见 run/chat.py），
  // done 后立即拉取展示，不必等下次 openConversation / 同步轮询。
  if (conversationId) void refreshFirstTurnCard(conversationId);
}

function handleErrorEvent(event, { row, answer, collapseReasoning, conversationId }) {
  clearElapsedStatus();
  clearVisionProgress();
  collapseReasoning();
  if (event.partial_message) {
    // 失败时后端已把累积内容持久化为 partial assistant 消息，直接用其渲染，
    // 保留已展示的思考/正文/工具，避免 HTTP 500 后内容被覆盖丢失。
    try {
      const partialRow = messageElement(event.partial_message);
      row.replaceWith(partialRow);
      updateContextUsage(null, event.partial_message);
    } catch (error) {
      console.error('[naiba] error 事件渲染崩溃:', error, 'message=', event.partial_message);
    }
  } else {
    // 没有 partial_message：绝不能清空已展示的中途输出，
    // 只在真正无任何内容时才显示错误占位；否则会抹掉 AI 已输出的回复。
    const hasContent = Boolean((answer.dataset.raw || '').trim())
      || row.querySelector('.reasoning-block, .tool-run, .stream-prose, .tool-confirm');
    if (hasContent) {
      const errNode = document.createElement('p');
      errNode.className = 'run-error';
      errNode.textContent = `执行失败：${event.message || '任务执行失败'}`;
      answer.appendChild(errNode);
    } else {
      answer.innerHTML = `<p>执行失败：${escapeHtml(event.message || '任务执行失败')}</p>`;
    }
  }
  $('#runtimeStatus').textContent = '执行失败';
  // 失败轮次同样可能是首轮：first_turn 已由收尾路径落盘，即时拉取展示。
  if (conversationId) void refreshFirstTurnCard(conversationId);
}

export function showChoiceButtons(choices, choiceGroups = []) {
  hideChoiceButtons();
  const composerWrap = $('.composer-wrap');
  const composer = $('#composerForm');
  const legacyChoices = Array.isArray(choices)
    ? choices.map((choice) => String(choice).trim()).filter(Boolean)
    : [];
  const sourceGroups = Array.isArray(choiceGroups) && choiceGroups.length
    ? choiceGroups
    : [{ prompt: '', choices: legacyChoices }];
  const groups = sourceGroups.map((group) => ({
    prompt: String(group?.prompt || '').trim(),
    choices: Array.isArray(group?.choices)
      ? group.choices.map((choice) => String(choice).trim()).filter(Boolean)
      : [],
  })).filter((group) => group.choices.length);
  if (!composerWrap || !composer || !groups.length) return;

  const container = document.createElement('div');
  container.className = 'choice-buttons';
  container.id = 'choiceButtons';
  container.setAttribute('role', 'group');
  container.setAttribute('aria-label', '可选回复');
  composerWrap.insertBefore(container, composer);
  const ruleBar = $('#conversationRuleBar');
  if (ruleBar) ruleBar.hidden = true;

  const selected = [];
  let groupIndex = 0;

  const formatAnswer = (group, choice, index) => choice;

  const renderGroup = () => {
    const group = groups[groupIndex];
    container.replaceChildren();
    container.setAttribute('aria-label', group.prompt || `第 ${groupIndex + 1} 组选项`);

    const header = document.createElement('div');
    header.className = 'choice-header';
    if (groupIndex > 0) {
      const back = document.createElement('button');
      back.type = 'button';
      back.className = 'choice-back';
      back.textContent = '←';
      back.title = '返回上一项';
      back.setAttribute('aria-label', '返回上一项');
      back.disabled = Boolean(state.abortController);
      back.addEventListener('click', () => {
        selected.splice(groupIndex - 1);
        groupIndex -= 1;
        renderGroup();
      });
      header.appendChild(back);
    }

    const prompt = document.createElement('strong');
    prompt.className = 'choice-prompt';
    prompt.textContent = group.prompt || '请选择';
    header.appendChild(prompt);

    if (groups.length > 1) {
      const progress = document.createElement('span');
      progress.className = 'choice-progress';
      progress.textContent = `${groupIndex + 1}/${groups.length}`;
      header.appendChild(progress);
    }
    container.appendChild(header);

    if (selected.length) {
      const summary = document.createElement('div');
      summary.className = 'choice-selection-summary';
      summary.textContent = `已选：${selected.join('；')}`;
      container.appendChild(summary);
    }

    const options = document.createElement('div');
    options.className = 'choice-options';
    group.choices.forEach((choice) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'choice-btn';
      btn.textContent = choice;
      btn.disabled = Boolean(state.abortController);
      btn.addEventListener('click', () => {
        selected[groupIndex] = choice;
        if (groupIndex < groups.length - 1) {
          groupIndex += 1;
          renderGroup();
          scrollToBottom();
          return;
        }
        const answer = groups
          .map((answerGroup, index) => formatAnswer(answerGroup, selected[index], index))
          .join('\n');
        hideChoiceButtons();
        fillComposer(answer);
      });
      options.appendChild(btn);
    });
    container.appendChild(options);
  };

  renderGroup();
  scrollToBottom();
}

export function hideChoiceButtons() {
  const existing = $('#choiceButtons');
  if (existing) existing.remove();
  renderConversationRuleBar();
}

export function fillComposer(text) {
  // 把按钮拼好的内容放进输入框由用户确认，不自动发送。
  const input = $('#messageInput');
  if (!input) return;
  input.value = String(text || '');
  resizeTextarea();
  renderInputMirror();
  updateSkillPopup();
  input.focus();
}

export async function approveTool(confirmId, runId = state.chatRunId) {
  try {
    const confirmEl = document.querySelector(`[data-confirm-id="${confirmId}"]`);
    if (confirmEl) {
      confirmEl.querySelector('.tool-confirm-actions').innerHTML = '<div class="tool-confirm-status">正在执行...</div>';
    }
    const response = await api('/api/tool/confirm', {
      method: 'POST', body: { run_id: runId, confirm_id: confirmId },
    });
    if (confirmEl) {
      confirmEl.querySelector('.tool-confirm-status').textContent = response.success ? '已执行' : `执行失败：${response.result}`;
    }
  } catch (error) {
    toast(`确认失败：${error.message}`);
  }
}

export async function rejectTool(confirmId, runId = state.chatRunId) {
  try {
    const confirmEl = document.querySelector(`[data-confirm-id="${confirmId}"]`);
    if (confirmEl) {
      confirmEl.querySelector('.tool-confirm-actions').innerHTML = '<div class="tool-confirm-status">已拒绝</div>';
    }
    const response = await api('/api/tool/reject', {
      method: 'POST', body: { run_id: runId, confirm_id: confirmId },
    });
    if (confirmEl) confirmEl.querySelector('.tool-confirm-status').textContent = response.result || '已拒绝';
  } catch (error) {
    toast(`拒绝失败：${error.message}`);
  }
}

// 确认卡按钮事件委托：模块（ESM）作用域函数不能经内联 onclick 访问（全局查找 ReferenceError），
// 统一在模块内委托分派（import 绑定只读红线：不做跨文件赋值）。
document.addEventListener('click', (event) => {
  const button = event.target.closest('.tool-confirm-btn');
  if (!button || !button.dataset.confirmId) return;
  event.preventDefault();
  const confirmId = button.dataset.confirmId;
  const runId = button.dataset.runId || state.chatRunId;
  if (button.classList.contains('tool-confirm-reject')) {
    rejectTool(confirmId, runId);
  } else if (button.classList.contains('tool-confirm-approve')) {
    approveTool(confirmId, runId);
  }
});

// 用量"请求明细"展开/收起（与确认卡同模式：data-* + 模块内委托）。
document.addEventListener('click', (event) => {
  const button = event.target.closest('[data-usage-toggle]');
  if (!button) return;
  const scope = button.closest('.usage-stream, .message-body');
  const box = scope && scope.querySelector('.usage-requests');
  if (!box) return;
  box.hidden = !box.hidden;
  button.setAttribute('aria-expanded', String(!box.hidden));
  const arrow = button.querySelector('.usage-toggle-arrow');
  if (arrow) arrow.textContent = box.hidden ? '▸' : '▾';
});

export function setBusy(busy) {
  state.chatBusy = busy;
  const mc = $('#messages');
  if (mc) mc.classList.toggle('conversation-running', busy);
  const sendBtn = $('#sendButton');
  sendBtn.disabled = Boolean(state.cancelRequested);
  sendBtn.classList.toggle('is-stop', busy);
  sendBtn.innerHTML = state.cancelRequested
    ? '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"></circle><circle cx="12" cy="12" r="3"></circle></svg>'
    : (busy
      ? '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2"></rect></svg>'
      : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5M5 12l7-7 7 7"></path></svg>');
  sendBtn.title = state.cancelRequested ? '正在停止' : (busy ? '停止当前任务' : '发送');
  $$('#choiceButtons button').forEach((button) => { button.disabled = busy; });
  sendBtn.setAttribute('aria-label', state.cancelRequested ? '正在停止' : (busy ? '停止当前任务' : '发送'));
  const messageInput = $('#messageInput');
  messageInput.disabled = false;
  messageInput.placeholder = busy ? '回复进行中…' : '输入消息';
  updateContextComposerLock(busy);
  updateLightweightModeControl();
  updateUnloadModelButton();
  if (!busy && $('#runtimeStatus').textContent !== '执行失败') $('#runtimeStatus').textContent = '就绪';
}

export async function cancelCurrentRun() {
  if (state.cancelRequested) return;
  const runId = String(state.chatRunId || '');
  const conversationId = String(state.runConversationId || state.conversationId || '');
  if (!runId && !conversationId) return;
  state.cancelRequested = true;
  state.cancelConversationId = conversationId;
  if (runId) state.cancelledRunIds.add(runId);
  state.checkRunEligible = false;
  clearRunReconnectTimers();
  detachRunConnection();
  state.runReconnectAt = 0;
  state.runAttempt = 0;
  state.runRecovering = false;
  setConnectionState('connected');
  setBusy(true);
  $('#runtimeStatus').textContent = '正在停止';

  let terminalConfirmed = false;
  try {
    let result = null;
    for (const delay of [0, 100, 250, 500, 1000]) {
      if (delay) await new Promise((resolve) => window.setTimeout(resolve, delay));
      try {
        result = await api('/api/chat/cancel', {
          method: 'POST', body: { run_id: runId, conversation_id: conversationId },
        });
        break;
      } catch (error) {
        // The initial /api/chat request and this fallback request are served by
        // different threads.  Before run_started there is a tiny window where
        // the conversation Run has not been committed yet.
        if (runId || error.status !== 404 || delay === 1000) throw error;
      }
    }
    const resolvedRunId = String(result?.run?.id || runId || '');
    if (resolvedRunId) state.cancelledRunIds.add(resolvedRunId);
    for (let attempt = 0; attempt < 100; attempt += 1) {
      const active = await api(`/api/runs?conversation_id=${encodeURIComponent(conversationId)}&active_only=1`);
      if (!(active.runs || []).length) {
        terminalConfirmed = true;
        break;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 250));
    }
    if (!terminalConfirmed) throw new Error('服务端尚未确认任务已停止');
    await loadTasks();
    if (state.conversationId === conversationId) await openConversation(conversationId);
  } catch (error) {
    console.debug('[naiba] cancel chat failed:', error.message);
    toast(`停止任务失败：${error.message}`);
  } finally {
    if (terminalConfirmed && state.cancelConversationId === conversationId) {
      state.chatRunId = '';
      state.runConversationId = '';
      state.runSequence = 0;
      state.runRow = null;
      state.cancelRequested = false;
      state.cancelConversationId = '';
      state.checkRunEligible = false;
      setBusy(false);
    }
  }
}

// 显式刷新页面（内嵌 pywebview 无法 F5 时的退路，浏览器同样可用）。
// URL 中的 token 与对话/工作区状态由后端持久化，reload 后可恢复。
export function reloadPage() {
  if (state.runWatchdogTimer) stopRunWatchdog();
  if (state.elapsedTimer) clearElapsedStatus();
  window.location.reload();
}

// ============================================================
// Skill 快速引用（/ 索引 + 蓝色高亮 + 顶栏点击插入 + 发送解析）
// ============================================================
