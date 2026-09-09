// ============================================================
// 04-messages.js —— 拆分自 public/app.js 第 1212-1501 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, api, draggedFileCache, emptyStateElement, escapeHtml, notifyComposerChanged, state, toast } from "./01-core.js";
import { markdown } from "./02-markdown.js";
import { activityMarkup, closeImageLightbox, fileChangesSummaryMarkup, fileUrl, mediaKind, mediaMarkup, mediaTruncatedNotice, reasoningMarkup, remainingAttachments, skillMarkup, sourcesMarkup, toolMarkup, updateContextUsage, uploadedFileMarkup, usageMarkup } from "./03-media.js";
import { openConversation, syncCurrentConversation } from "./08-conversations.js";
import { renderPendingFiles } from "./10-upload.js";
import { hideChoiceButtons, sendMessage, showChoiceButtons } from "./12-chat-input.js";
import { hideSkillPopup, renderInputMirror, renderUserContent, resizeTextarea, updateSkillPopup } from "./13-skill-refs.js";
// 当前会话所用 Agent 的自定义头像 URL（没有则空串 → 回退到默认的「AI」圆标）。
// 与 currentAgentFixedSkillIds 同口径：会话绑定的 Agent 优先，失效时回退默认 Agent。
export function currentAgentAvatarUrl() {
  const agents = state.bootstrap?.agents || [];
  const conversation = state.conversations.find((item) => item.id === state.conversationId);
  let agent = agents.find((item) => item.id === String(conversation?.agent_id || ''));
  if (!agent) agent = agents.find((item) => item.id === String(state.bootstrap?.default_agent_id || ''));
  const file = String(agent?.avatar || '');
  return file ? `/api/agents/avatar/${encodeURIComponent(file)}` : '';
}

// 内置默认种子模板（设置页留空时回退用它）；占位符：{handoff_path} / {task_count} / {task_list}
export const DEFAULT_CONTEXT_RESET_SEED = [
  '上一段会话已交接，交接文档：{handoff_path}',
  '请先读取该交接文档再继续。',
  '[后台任务] 当前仍有 {task_count} 个任务在运行：',
  '{task_list}',
].join('\n');

// 按分割线标记渲染「新会话」种子消息：没有后台任务时，含占位符的整行自动去掉。
export function contextResetSeedText(info = {}) {
  const template = String(state.bootstrap?.settings?.context_reset_seed_template || '').trim()
    || DEFAULT_CONTEXT_RESET_SEED;
  const tasks = Array.isArray(info.tasks) ? info.tasks : [];
  const taskList = tasks.map((task) => {
    const kind = String(task.kind || '');
    const status = String(task.status || '');
    const suffix = kind || status ? `（${kind}${kind && status ? '，' : ''}${status}）` : '';
    return `- ${String(task.id || '')}${suffix}${task.title ? `：${task.title}` : ''}`;
  }).join('\n');
  return template.split('\n')
    .filter((line) => !((line.includes('{task_count}') || line.includes('{task_list}')) && !tasks.length))
    .map((line) => line
      .replaceAll('{handoff_path}', String(info.handoff_path || ''))
      .replaceAll('{task_count}', String(tasks.length))
      .replaceAll('{task_list}', taskList))
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

// 把种子消息填进输入框——**不自动发送**，由用户确认/编辑后点发送。
export function fillContextResetSeed(info = {}) {
  const text = contextResetSeedText(info);
  const input = $('#messageInput');
  if (!input || !text) return false;
  input.value = text;
  notifyComposerChanged(input);
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  return true;
}

// 「新会话」分割条：标在**某条消息**的 metadata 上（`session_start`），渲染在该消息正下方。
// 语义 = 此线以上的消息不再进入模型上下文，线以下的消息仍在上下文里；聊天记录一条不删。
// 兼容遗留形态：早期版本用独立的 role=session 标记行，这里照旧渲染成同款分隔条。
export function sessionDividerElement(message) {
  const legacyRow = message.role === 'session';
  const info = (message.metadata || {}).session_start || {};
  const at = Number(info.at || message.created_at || 0);
  const time = at ? new Date(at).toLocaleString('zh-CN', { hour12: false }) : '';
  const source = String(info.source || 'manual') === 'tool' ? '模型重置' : '手动';
  const handoff = String(info.handoff_path || '');
  const seedButton = message.id && (handoff || String(info.source || '') === 'tool')
    ? `<button type="button" class="session-divider-seed" data-fill-reset-seed="${escapeHtml(message.id)}" title="把「新会话」种子消息填进输入框（可编辑后再发送）">填入种子消息</button>`
    : '';
  const row = document.createElement('article');
  row.className = 'message-row session-divider';
  row.dataset.messageId = message.id || '';
  row.dataset.sessionDivider = message.id || '';
  // 注意：这里是 DOM 属性赋值（不是 innerHTML），不能 escapeHtml——转义后的 &quot; 会被
  // dataset 原样读出，JSON.parse 直接失败（实测：种子消息里路径变空）。
  row.dataset.resetSeedInfo = JSON.stringify(info);
  row.innerHTML = `
    <div class="session-divider-bar" title="此线以上的消息不再进入模型上下文；下方消息仍保留在上下文中（聊天记录全部保留）">
      <span class="session-divider-line" aria-hidden="true"></span>
      <span class="session-divider-label">新会话${legacyRow ? '开始' : ''} · ${escapeHtml(source)}${time ? ` · ${escapeHtml(time)}` : ''}</span>
      <span class="session-divider-line" aria-hidden="true"></span>
      ${seedButton}
      ${message.id ? '<button type="button" class="session-divider-cancel" data-cancel-session-start title="撤销这条分割线：此线以上的消息重新进入模型上下文">撤销</button>' : ''}
    </div>
    <div class="session-divider-hint">此线以上不再进入模型上下文${handoff ? ` · 交接文档：${escapeHtml(handoff)}` : ''}</div>`;
  return row;
}

// 该消息下方是否要跟一条分割线（遗留标记行由 messageElement 直接渲染，不在此列）。
export function sessionDividerAfter(message) {
  if (!message || message.role === 'session') return null;
  return (message.metadata || {}).session_start ? sessionDividerElement(message) : null;
}

// 就地替换一条消息，并按需在其下方补上分割线（终态事件渲染路径复用）。
export function replaceWithMessage(target, message, temporary = false) {
  const element = messageElement(message, temporary);
  target.replaceWith(element);
  const divider = sessionDividerAfter(message);
  if (divider) element.insertAdjacentElement('afterend', divider);
  return element;
}

export function messageElement(message, temporary = false) {
  if (message.role === 'session') return sessionDividerElement(message);
  const row = document.createElement('article');
  row.className = `message-row ${message.role}`;
  row.dataset.messageId = message.id || '';
  const metadata = message.metadata || {};
  row.__messageMetadata = metadata;
  if (Array.isArray(metadata.attachments)) {
    metadata.attachments.forEach((attachment) => {
      const source = attachment.source || attachment.path;
      if (source && mediaKind(source, attachment.name) === 'image') preloadDraggedFile(source, attachment.name);
    });
  }
  if (message.role === 'user') {
    const actions = message.id ? '<div class="message-actions"><button data-branch-message title="从这条消息分支到新会话继续">分支</button></div>' : '';
    row.innerHTML = `<div class="message-body">${renderUserContent(metadata.display_content || message.content)}${uploadedFileMarkup(metadata.attachments)}${actions}</div>`;
  } else {
    const abortedBadge = metadata.aborted
      ? '<span class="aborted-badge">已中止</span>'
      : metadata.partial ? '<span class="aborted-badge">未完成</span>' : '';
    const activity = Array.isArray(metadata.activity) ? metadata.activity : [];
    const activityHtml = activity.length ? activityMarkup(activity) : '';
    const activityHasProse = activity.some((item) => item && item.type === 'prose');
    const reasoningToolHtml = activityHtml || (reasoningMarkup(metadata.reasoning) + toolMarkup(metadata.tool_runs));
    // 当 activity 已内嵌正文（prose 条目）时，正文按时间交错展示，不再在末尾重复渲染；
    // 末尾的 answer-content 仅保留用于复制/检索（隐藏），避免与时间线重复。
    const hideBottomContent = activityHasProse && !temporary;
    // 媒体就地内嵌在工具调用处（toolRunMarkup 自带）；末尾网格只渲染"没有就地归属"的
    // 附件——新消息的附件都在 run.media 里（集合命中→不重复渲染），旧会话没有 run.media
    // （集合为空→末尾网格照旧），两条渲染路径互不打架。
    const bottomAttachments = remainingAttachments(metadata);
    // 自定义头像：用该 Agent 的会话把默认「AI」圆标换成上传的图片（已中心裁切成正方形）。
    const avatarUrl = currentAgentAvatarUrl();
    const avatarHtml = avatarUrl
      ? `<img class="message-avatar message-avatar-img" src="${escapeHtml(avatarUrl)}" alt="">`
      : '<div class="message-avatar">AI</div>';
    // 「新会话」分割线入口：紧挨「复制」右侧（只有已落库的完整回复才有 id，流式临时气泡不给）。
    const sessionButton = (!temporary && message.id)
      ? `<button data-session-start-after="${escapeHtml(message.id)}" title="在这条回复之后划一条分割线：此线以上的消息不再进入模型上下文（下方消息仍在上下文中，聊天记录全部保留）">新会话</button>`
      : '';
    row.innerHTML = `
      ${avatarHtml}
      <div class="message-card">
        <div class="message-body">
          ${skillMarkup(metadata.skills)}
          ${hideBottomContent ? abortedBadge : ''}
          ${reasoningToolHtml}
          ${temporary ? '<div class="run-activity activity">正在准备</div>' : ''}
          <div class="answer-content" data-raw="" ${hideBottomContent ? 'style="display:none"' : ''}>${temporary ? '' : abortedBadge + markdown(message.content)}</div>
          ${temporary ? '' : sourcesMarkup(metadata.sources)}
          ${mediaMarkup(bottomAttachments)}
          ${bottomAttachments.length ? mediaTruncatedNotice(metadata.attachments_truncated) : ''}
          ${temporary ? '' : fileChangesSummaryMarkup(metadata.files)}
          ${temporary ? '' : usageMarkup({ ...(metadata.usage || {}), performance: metadata.performance || metadata.usage?.performance }, message.created_at)}
          ${temporary ? '' : `<div class="message-actions"><button data-copy-message>复制</button>${sessionButton}</div>`}
        </div>
      </div>`;
  }
  return row;
}

export function preloadDraggedFile(source, name = '') {
  const url = new URL(fileUrl(source), location.href).href;
  if (draggedFileCache.has(url)) return;
  fetch(url).then((response) => response.ok ? response.blob() : Promise.reject(new Error('image fetch failed')))
    .then((blob) => draggedFileCache.set(url, new File([blob], name || 'image' + (blob.type ? '.' + blob.type.split('/')[1] : ''), { type: blob.type })))
    .catch(() => {});
}

export function startEditMessage(row) {
  if (!row) return;
  const body = row.querySelector('.message-body');
  if (!body || body.querySelector('textarea[data-edit-input]')) return;
  // 提取纯文本内容（不含附件标记）。带 /ref 引用的消息优先回填原始 display_content（含引用的原文），
  // 否则退到 DOM 文本/rawContent。
  const displayContent = row.__messageMetadata?.display_content;
  const textContent = body.childNodes[0]?.textContent ?? body.textContent;
  const currentText = (displayContent !== undefined && displayContent !== '')
    ? displayContent
    : (row.dataset.rawContent || textContent.trim());
  const attachments = row.__messageMetadata?.attachments || [];
  row.dataset.rawContent = currentText;
  body.innerHTML = `
    <textarea class="edit-input" data-edit-input rows="9">${escapeHtml(currentText)}</textarea>
    <div class="edit-attachments">${uploadedFileMarkup(attachments)}</div>
    <div class="edit-actions">
      <button class="primary-button" data-edit-confirm>重新发送</button>
      <button class="control-button" data-edit-cancel>取消</button>
    </div>`;
  const textarea = body.querySelector('[data-edit-input]');
  textarea.focus();
  textarea.setSelectionRange(textarea.value.length, textarea.value.length);
  body.querySelector('[data-edit-cancel]').addEventListener('click', () => {
    // 取消：重新渲染当前会话
    if (state.conversationId) openConversation(state.conversationId);
  });
  body.querySelector('[data-edit-confirm]').addEventListener('click', () => {
    confirmEditMessage(row, textarea.value);
  });
  textarea.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) confirmEditMessage(row, textarea.value);
    if (e.key === 'Escape' && state.conversationId) openConversation(state.conversationId);
  });
}

export async function confirmEditMessage(row, newText) {
  const text = newText.trim();
  if (!text) {
    toast('内容不能为空');
    return;
  }
  const messageId = row.dataset.messageId;
  if (!messageId || !state.conversationId) return;
  try {
    const result = await api('/api/messages/edit', {
      method: 'POST',
      body: { conversation_id: state.conversationId, message_id: messageId },
    });
    toast('已从该消息重开，编辑点之前的上下文将复用缓存');
    // 恢复原消息的附件，供重发使用
    state.pendingFiles = (result.attachments || []).map((f) => ({ name: f.name, path: f.path, size: f.size }));
    renderPendingFiles();
    // 截断后重新渲染会话（被编辑的消息已从历史消失）
    await openConversation(state.conversationId);
    // 填入新内容并重发
    const input = $('#messageInput');
    input.value = text;
    resizeTextarea();
    renderInputMirror();
    updateSkillPopup();
    notifyComposerChanged(input);
    await sendMessage();
  } catch (error) {
    toast(`编辑失败：${error.message}`);
    if (state.conversationId) openConversation(state.conversationId);
  }
}

// 从某条 user 消息分支：新开一个会话，复制分支点之前的历史，并把分支消息预填进输入框。
// 非破坏性（原会话保留）；运行中不显示分支按钮（见 CSS .conversation-running），此处兜底拦截。
export async function branchMessage(row) {
  if (state.chatRunId || state.abortController) {
    toast('请先等待当前任务结束或停止后再分支');
    return;
  }
  const messageId = row?.dataset.messageId;
  const sourceId = state.conversationId;
  if (!messageId || !sourceId) {
    toast('分支失败：消息或会话不存在');
    return;
  }
  try {
    const result = await api(`/api/conversations/${sourceId}/branch`, {
      method: 'POST',
      body: { message_id: messageId },
    });
    const newConversation = result.conversation || {};
    const branch = result.branch_message || {};
    // 让新会话进入侧栏列表
    const index = state.conversations.findIndex((c) => c.id === newConversation.id);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...newConversation };
    else state.conversations.unshift(newConversation);
    // 切到新会话（复用 openConversation 的完整装载逻辑）
    await openConversation(newConversation.id);
    // 预填分支消息内容（原样，含 /ref），并恢复其附件为待上传（走现有输入逻辑）
    hideSkillPopup();
    const input = $('#messageInput');
    input.value = branch.display_content || branch.content || '';
    resizeTextarea();
    renderInputMirror();
    state.pendingFiles = (branch.attachments || []).map((f) => ({ name: f.name, path: f.path, size: f.size, thumb_path: f.thumb_path }));
    renderPendingFiles();
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    toast('已从该消息分支到新会话');
  } catch (error) {
    toast(`分支失败：${error.message}`);
  }
}

// 在某条 AI 回复之后落一条「新会话」分割线：此线以上的消息不再进入模型上下文。
export async function startNewSession(afterMessageId) {
  if (state.abortController || state.chatRunId) {
    toast('请先等待当前回答结束或停止后再划分割线');
    return;
  }
  const conversationId = state.conversationId;
  if (!conversationId || !afterMessageId) {
    toast('请先打开一个对话');
    return;
  }
  if (!window.confirm('在这条回复之后划一条分割线？\n\n此线以上的消息不再进入模型上下文；线以下的消息仍保留在上下文中。聊天记录全部保留，可随时撤销。')) return;
  try {
    await api(`/api/conversations/${conversationId}/session_start`, {
      method: 'POST',
      body: { after_message_id: afterMessageId, source: 'manual' },
    });
    await syncCurrentConversation();
    toast('已划出分割线：下一条消息起，此线以上的内容不再进入模型上下文');
  } catch (error) {
    toast(`划分割线失败：${error.message}`);
  }
}

// 撤销分割线：此线以上的消息重新进入模型上下文。
export async function cancelSessionStart(messageId) {
  if (!messageId) return;
  if (!window.confirm('撤销这条分割线？\n\n此线以上的消息会重新进入模型请求。')) return;
  try {
    await api(`/api/session_start/${encodeURIComponent(messageId)}`, { method: 'DELETE' });
    await syncCurrentConversation();
    toast('已撤销分割线');
  } catch (error) {
    toast(`撤销失败：${error.message}`);
  }
}

export let stickToBottom = true;

// ESM 下 import 绑定只读：跨文件写入经 setter（读点保持直接引用不变）。
export function setStickToBottom(value) { stickToBottom = value; }

export function isNearBottom(threshold = 80) {
  const messages = $('#messages');
  if (!messages) return true;
  return (messages.scrollHeight - messages.scrollTop - messages.clientHeight) < threshold;
}

// 默认滚动：只在用户仍停留在底部（跟随）时才自动滚到最新内容；
// 用户滚轮上滑阅读历史时，后续任何 delta/工具事件都不再把页面强行拉回底部。
export function scrollToBottom() {
  if (!stickToBottom) return;
  const messages = $('#messages');
  if (messages) messages.scrollTop = messages.scrollHeight;
}

// 强制滚到底部：用于确实需要展示最新内容的地方（渲染后一次性定位）。
export function forceScrollToBottom() {
  const messages = $('#messages');
  if (messages) messages.scrollTop = messages.scrollHeight;
}

export function scheduleStreamingMarkdown(element, raw) {
  if (!element) return;
  element.dataset.raw = raw;
  if (element.dataset.renderScheduled === '1') return;
  element.dataset.renderScheduled = '1';
  window.setTimeout(() => {
    element.dataset.renderScheduled = '0';
    element.innerHTML = markdown(element.dataset.raw || '');
    scrollToBottom();
  }, 40);
}

// 把“中途正文”作为独立兄弟块插到 answer 之前，按时间顺序与思考块/工具块交错显示。
// 只有当 answer 的前一个兄弟元素已经是流式正文块时才复用；一旦中间插入了工具/思考块，
// 之后的新正文会生成新的独立块，从而保持“思考→正文→工具→思考→正文…”的顺序，
// 而不是把所有正文统一累积到末尾的 answer-content。
export function getStreamingProseSegment(row, answer) {
  if (!answer) return null;
  const prev = answer.previousElementSibling;
  if (prev && prev.classList && prev.classList.contains('stream-prose')) {
    return prev;
  }
  const seg = document.createElement('div');
  seg.className = 'stream-prose';
  answer.before(seg);
  return seg;
}

// 首个工具出现时，把之前累计在底部（answer-content）的正文移到内联的正文块，
// 让它紧跟在该工具之前，与思考块/后续工具按时间交错，而不是停在末尾。
export function moveBottomProseInline(row, answer) {
  if (!answer) return;
  const bottomRaw = answer.dataset.raw || '';
  if (!bottomRaw.trim()) return;
  const seg = getStreamingProseSegment(row, answer);
  if (!seg) return;
  seg.dataset.raw = bottomRaw;
  seg.innerHTML = markdown(bottomRaw);
  answer.dataset.raw = '';
  answer.replaceChildren();
}

export function firstTurnCardMarkup(firstTurn) {
  const systemText = String((firstTurn && (firstTurn.system || firstTurn.prompt)) || '');
  if (!systemText) return '';
  const tools = Array.isArray(firstTurn.tools) ? firstTurn.tools : [];
  const skills = Array.isArray(firstTurn.skills) ? firstTurn.skills : [];
  const options = firstTurn.options || {};
  const toolChips = tools.map((tool) => `<span class="ft-chip ft-tool-chip" title="${escapeHtml(tool.description || '')}">${escapeHtml(tool.name || '')}</span>`).join('');
  const skillChips = skills.map((skill) => `<span class="ft-chip">${escapeHtml(skill.name || skill.id || '')}</span>`).join('');
  const optionText = [
    options.temperature != null ? `temperature ${options.temperature}` : '',
    options.max_tokens != null ? `max_tokens ${options.max_tokens}` : '',
    options.context_size != null ? `context ${options.context_size}` : '',
    options.reasoning_effort ? `reasoning ${options.reasoning_effort}` : '',
  ].filter(Boolean).join(' · ');
  return `<details class="first-turn-card">
    <summary>首次请求上下文 · ${escapeHtml(firstTurn.agent_name || 'Agent')} · ${escapeHtml(firstTurn.model_key || '')} · 工具 ${tools.length} 个</summary>
    <div class="ft-body">
      <div class="ft-meta">${skillChips ? `技能：${skillChips}` : '技能：无'}</div>
      <div class="ft-tools">${toolChips || '<span class="ft-note">（未启用工具）</span>'}</div>
      ${optionText ? `<div class="ft-options">生成参数：${escapeHtml(optionText)}</div>` : ''}
      <details class="ft-section">
        <summary>系统提示词（发送给模型的 system 原文）</summary>
        <pre class="ft-prompt">${escapeHtml(systemText)}</pre>
      </details>
      ${tools.length ? `<details class="ft-section">
        <summary>工具定义（${tools.length} 个，JSON）</summary>
        <pre class="ft-prompt">${escapeHtml(JSON.stringify(tools, null, 2))}</pre>
      </details>` : ''}
    </div>
  </details>`;
}

// 「首次请求上下文」折叠卡的就地刷新（终态事件后调用）：数据源与 openConversation 一致
// （GET /api/conversations/{id}/first_turn）。只增删/替换折叠卡节点、不整页重渲染，
// 避免打断已完成消息行的 DOM；renderMessages 也复用之，保证两条渲染路径同序
// （empty → 折叠卡 → 消息）。
export async function refreshFirstTurnCard(conversationId = state.conversationId) {
  if (!conversationId || conversationId !== state.conversationId) return;
  let firstTurn = null;
  try {
    const data = await api(`/api/conversations/${conversationId}/first_turn`);
    const hasSystem = data && typeof data === 'object' && Boolean(data.system || data.prompt);
    firstTurn = hasSystem ? data : null;
  } catch (_) {
    firstTurn = null; // 老会话无此数据 / 接口瞬时失败：静默（与 openConversation 同策略）
  }
  if (conversationId !== state.conversationId) return; // 拉取期间已切换会话：丢弃
  state.firstTurnInfo = firstTurn;
  upsertFirstTurnCard();
}

function upsertFirstTurnCard() {
  const container = $('#messages');
  if (!container) return;
  const existing = container.querySelector('.first-turn-card');
  const markup = state.firstTurnInfo ? firstTurnCardMarkup(state.firstTurnInfo) : '';
  if (!markup) {
    if (existing) existing.remove();
    return;
  }
  const template = document.createElement('template');
  template.innerHTML = markup.trim();
  const card = template.content.firstElementChild;
  if (!card) {
    if (existing) existing.remove();
    return;
  }
  if (existing) existing.replaceWith(card);
  else {
    const empty = $('#emptyState');
    if (empty && empty.parentNode === container) empty.insertAdjacentElement('afterend', card);
    else container.prepend(card);
  }
}

/* ---------- 消息列表懒加载（窗口恒以「轮」为边界） ---------- */

// 打开会话默认渲染最近 N 轮；向上滚动时每次再往前渲染 N 轮。
// 窗口必须以「轮」为边界：一条 AI 回复上的「新会话」分割线属于该轮，
// 不能出现"分割线在窗口内、它的锚点消息在窗口外"这种拆散（用户特别提醒过）。
const LAZY_TURNS_INITIAL = 10;
const LAZY_TURNS_STEP = 10;
const LAZY_TOP_TRIGGER = 240;
// 渲染/程序化滚动会连带触发 scroll 事件：这段时间内不把 scroll 当成"用户滚到顶"，
// 否则打开会话（内容刚填进去、scrollTop 还在 0 附近）就会立刻多渲染一段。
let lazySuppressUntil = 0;

// 轮起点 = 每条 user 消息的下标（首条不是 user 时把 0 也算一个起点）。
function turnStartIndexes(messages) {
  const starts = [];
  (messages || []).forEach((message, index) => {
    if (message?.role === 'user') starts.push(index);
  });
  if (!starts.length || starts[0] !== 0) starts.unshift(0);
  return starts;
}

function clampTurnStart(messages, wanted) {
  let result = 0;
  for (const start of turnStartIndexes(messages)) {
    if (start <= wanted) result = start;
    else break;
  }
  return result;
}

function initialRenderStart(messages) {
  const starts = turnStartIndexes(messages);
  return starts[Math.max(0, starts.length - LAZY_TURNS_INITIAL)];
}

// 一段消息的 DOM 片段（消息 + 紧跟其后的「新会话」分割线，保证两者同进同出）。
function messageRangeFragment(messages, start, end) {
  const fragment = document.createDocumentFragment();
  for (let index = start; index < end; index += 1) {
    const message = messages[index];
    if (!message) continue;
    fragment.append(messageElement(message));
    const divider = sessionDividerAfter(message);
    if (divider) fragment.append(divider);
  }
  return fragment;
}

// 程序化定位/补偿必须瞬时生效：容器是 scroll-behavior: smooth，直接写 scrollTop 会动画化，
// 既测不准（scrollTop 读回旧值），还会在动画期间连发 scroll 事件干扰懒加载判定。
function withInstantScroll(container, mutate) {
  const previous = container.style.scrollBehavior;
  container.style.scrollBehavior = 'auto';
  try {
    mutate();
  } finally {
    container.style.scrollBehavior = previous;
  }
}

// 向上扩展渲染窗口（预渲染视界外的一段历史），并保持视口内容不跳动。
export function extendRenderedWindow() {
  const container = $('#messages');
  const messages = state.messages || [];
  const start = Number(state.renderStart) || 0;
  if (!container || start <= 0) return false;
  const starts = turnStartIndexes(messages);
  let position = starts.indexOf(start);
  if (position <= 0) position = starts.length;
  const nextStart = starts[Math.max(0, position - LAZY_TURNS_STEP)];
  if (nextStart >= start) return false;
  const beforeHeight = container.scrollHeight;
  const anchor = container.querySelector('.message-row[data-message-id]');
  const fragment = messageRangeFragment(messages, nextStart, start);
  if (anchor) anchor.before(fragment);
  else container.append(fragment);
  state.renderStart = nextStart;
  // 补进来的高度加回 scrollTop：用户正在看的那条消息仍停在原来的位置（瞬时，不动画）。
  withInstantScroll(container, () => {
    container.scrollTop += container.scrollHeight - beforeHeight;
  });
  scheduleTurnRail();
  return true;
}

// 把某条消息所在轮次渲染出来（刻度轨点击未渲染的轮次时用）。
function ensureMessageRendered(messageIndex) {
  let guard = 0;
  while ((Number(state.renderStart) || 0) > messageIndex && guard < 200) {
    if (!extendRenderedWindow()) break;
    guard += 1;
  }
  return (Number(state.renderStart) || 0) <= messageIndex;
}

export function renderMessages(messages) {
  const container = $('#messages');
  const empty = emptyStateElement;
  closeImageLightbox();
  const list = Array.isArray(messages) ? messages : [];
  // 切换会话 → 窗口重置为「最近 N 轮」；同一会话刷新（轮询/保存后）→ 保留当前窗口与滚动位置，
  // 否则用户正在往上翻历史时一次轮询就会把他拽回底部。
  const switched = state.messagesConversationId !== String(state.conversationId || '');
  const keepScroll = !switched && !stickToBottom;
  const previousScrollTop = container.scrollTop;
  const wantedStart = switched ? initialRenderStart(list) : clampTurnStart(list, Number(state.renderStart) || 0);
  state.messages = list;
  state.messagesConversationId = String(state.conversationId || '');
  state.renderStart = list.length ? Math.min(wantedStart, list.length - 1) : 0;
  // 渲染/程序化滚动产生的 scroll 事件不算"用户滚到顶"：这段时间内不触发预渲染。
  lazySuppressUntil = Date.now() + 400;
  // 诊断日志：定位"消息消失"是数据为空还是渲染崩溃
  console.log('[naiba] renderMessages 调用, 消息数=', list.length,
    '渲染起点=', state.renderStart,
    'conversationId=', state.conversationId,
    'roles=', list.map((m) => m.role).join(','));
  try {
    container.replaceChildren();
    // 始终保留 empty 在容器中，仅切换 hidden；否则它会被移出 DOM，
    // 导致后续 sendMessage 中 $('#emptyState') 为 null 而崩溃
    empty.hidden = list.length > 0;
    container.append(empty);
    // 首轮上下文折叠卡：固定在最顶部（第一条消息上方），展示第一轮发送给模型的
    // 系统提示词与工具集（默认折叠）。
    upsertFirstTurnCard();
    if (list.length) {
      // 懒加载：只渲染 [renderStart, 末尾) 这一段（窗口恒以「轮」为边界，分割线不会与锚点分离）
      container.append(messageRangeFragment(list, state.renderStart, list.length));
      if (keepScroll) {
        withInstantScroll(container, () => { container.scrollTop = previousScrollTop; });
      } else {
        // 先瞬时定位到底部（smooth 会让几百条消息"慢慢滑"），再在下一帧布局稳定后确认；
        // 用户若已上滑（stickToBottom=false）则不再抢滚动。
        withInstantScroll(container, () => { container.scrollTop = container.scrollHeight; });
        requestAnimationFrame(() => scrollToBottom());
      }
    }
    const choiceMessage = pendingChoiceMessage(list);
    const choices = choiceMessage?.metadata?.choices || [];
    const choiceGroups = choiceMessage?.metadata?.choice_groups || [];
    if ((Array.isArray(choiceGroups) && choiceGroups.length) || (Array.isArray(choices) && choices.length)) {
      showChoiceButtons(choices, choiceGroups);
    }
    else hideChoiceButtons();
    updateContextUsage(list);
  } catch (error) {
    console.error('[naiba] renderMessages 渲染崩溃:', error, '消息数=', list.length);
  }
}

export function pendingChoiceMessage(messages) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.role === 'user') return null;
    const choices = message?.metadata?.choices;
    const groups = message?.metadata?.choice_groups;
    if (message?.role === 'assistant'
      && ((Array.isArray(groups) && groups.length) || (Array.isArray(choices) && choices.length))) {
      return message;
    }
    if (message?.role === 'assistant') return null;
  }
  return null;
}

/* ---------- 对话刻度轨（右侧）：每个用户轮次一条横条 ---------- */

const TURN_RAIL_MAX = 30;          // 同时最多显示 30 条（以视口中心为基准的滑动窗口）
const TURN_TIP_USER_CHARS = 160;   // 概要里用户消息最多保留的字符数（再多交给 CSS 省略号）
const TURN_TIP_REPLY_CHARS = 320;

let turnRailTurns = [];            // [{ anchor, user, reply }]
let turnRailActive = -1;
let turnRailWindow = { start: -1, end: -1 };
let turnRailFrame = 0;
let turnRailBound = false;

// 消息数据的文本预览（懒加载下未渲染的轮次没有 DOM，刻度轨概要只能从数据取）。
function messagePreviewText(message, limit) {
  const raw = String((message?.metadata || {}).display_content ?? message?.content ?? '');
  const text = raw.replace(/\s+/g, ' ').trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

// 一个「用户轮次」= 一条用户消息 + 它之后（下一条用户消息之前）的助手回复。
// **从 state.messages 收集**（不是从 DOM）：懒加载只渲染窗口内的消息，刻度轨仍要覆盖全部轮次；
// 已渲染的轮次顺带记下锚点元素，供偏移计算与点击跳转使用。
function collectTurns() {
  const messages = state.messages || [];
  const anchors = new Map();
  document.querySelectorAll('#messages .message-row[data-message-id]').forEach((row) => {
    if (row.dataset.messageId) anchors.set(row.dataset.messageId, row);
  });
  const turns = [];
  messages.forEach((message, messageIndex) => {
    if (!message) return;
    if (message.role === 'user') {
      turns.push({
        messageId: String(message.id || ''),
        messageIndex,
        anchor: anchors.get(String(message.id || '')) || null,
        user: messagePreviewText(message, TURN_TIP_USER_CHARS),
        reply: '',
      });
      return;
    }
    if (!turns.length || message.role !== 'assistant') return;
    const reply = messagePreviewText(message, TURN_TIP_REPLY_CHARS);
    if (reply) turns[turns.length - 1].reply = reply;  // 一轮多条助手消息时取最后一条有正文的
  });
  return turns;
}

function turnRailOffsets(container) {
  const base = container.getBoundingClientRect().top - container.scrollTop;
  const offsets = turnRailTurns.map((turn) => (turn.anchor
    ? Math.round(turn.anchor.getBoundingClientRect().top - base)
    : null));
  // 未渲染的轮次没有锚点：按相邻已渲染轮次向上/向下均摊估算。
  // 只用于判定"视口中心在哪一轮"，不参与任何布局。
  const firstKnown = offsets.findIndex((value) => value !== null);
  if (firstKnown < 0) return offsets.map((_, index) => index * 120);
  for (let index = firstKnown - 1; index >= 0; index -= 1) {
    offsets[index] = offsets[index + 1] - 120;
  }
  for (let index = firstKnown + 1; index < offsets.length; index += 1) {
    if (offsets[index] === null) offsets[index] = offsets[index - 1] + 120;
  }
  return offsets;
}

// 视口中心落在哪一轮的垂直范围内，就高亮哪一条。
function turnRailActiveIndex(container, offsets) {
  if (!turnRailTurns.length) return -1;
  const center = container.scrollTop + container.clientHeight / 2;
  let active = 0;
  for (let index = 0; index < offsets.length; index += 1) {
    if (offsets[index] <= center) active = index;
    else break;
  }
  return active;
}

function renderTurnRail() {
  const rail = $('#turnRail');
  const container = $('#messages');
  if (!rail || !container) return;
  turnRailTurns = collectTurns();
  if (turnRailTurns.length < 2) {
    rail.hidden = true;
    turnRailWindow = { start: -1, end: -1 };
    turnRailActive = -1;
    hideTurnTip();
    return;
  }
  const offsets = turnRailOffsets(container);
  const active = turnRailActiveIndex(container, offsets);
  const total = turnRailTurns.length;
  const span = Math.min(TURN_RAIL_MAX, total);
  const start = Math.max(0, Math.min(active - Math.floor(span / 2), total - span));
  const end = start + span;
  rail.hidden = false;
  if (start === turnRailWindow.start && end === turnRailWindow.end) {
    // 窗口没变：只挪高亮，不重写 DOM（教训 §九.37：每帧重写是迟滞主因）
    if (active !== turnRailActive) {
      turnRailActive = active;
      rail.querySelectorAll('.turn-tick').forEach((tick) => {
        tick.classList.toggle('active', Number(tick.dataset.turnIndex) === active);
      });
    }
    return;
  }
  turnRailWindow = { start, end };
  turnRailActive = active;
  rail.replaceChildren();
  for (let index = start; index < end; index += 1) {
    // 固定尺寸的透明块 = 判定区；可见的线是块里的内层元素（线变长变粗不影响块尺寸）
    const tick = document.createElement('button');
    tick.type = 'button';
    tick.className = 'turn-tick';
    tick.dataset.turnIndex = String(index);
    // 懒加载后"第 N 轮"与 DOM 行不再一一对应，带上该轮用户消息 id 便于定位/断言。
    tick.dataset.turnMessageId = String(turnRailTurns[index]?.messageId || '');
    tick.setAttribute('aria-label', `第 ${index + 1} 轮对话`);
    const line = document.createElement('span');
    line.className = 'turn-tick-line';
    tick.append(line);
    if (index === active) tick.classList.add('active');
    rail.append(tick);
  }
}

function scheduleTurnRail() {
  if (turnRailFrame) return;
  turnRailFrame = requestAnimationFrame(() => {
    turnRailFrame = 0;
    try {
      renderTurnRail();
    } catch (error) {
      // 刻度轨是辅助显示，坏掉不能影响消息渲染——但必须留痕，不静默吞掉。
      console.error('[naiba] 对话刻度轨渲染失败:', error);
    }
  });
}

function ensureTurnTip() {
  let tip = $('#turnTip');
  if (tip) return tip;
  tip = document.createElement('div');
  tip.id = 'turnTip';
  tip.className = 'turn-tip';
  tip.setAttribute('role', 'tooltip');
  tip.hidden = true;
  document.body.append(tip);
  return tip;
}

function showTurnTip(tick) {
  const index = Number(tick.dataset.turnIndex);
  const turn = turnRailTurns[index];
  if (!turn) return;
  const tip = ensureTurnTip();
  tip.innerHTML = `
    <div class="turn-tip-index">第 ${index + 1} 轮</div>
    <div class="turn-tip-user">${escapeHtml(turn.user || '（无文字，仅附件）')}</div>
    <div class="turn-tip-reply">${escapeHtml(turn.reply || '（暂无回复）')}</div>`;
  tip.hidden = false;
  const rect = tick.getBoundingClientRect();
  const left = Math.max(8, rect.left - tip.offsetWidth - 10);
  const top = Math.max(8, Math.min(
    rect.top + rect.height / 2 - tip.offsetHeight / 2,
    window.innerHeight - tip.offsetHeight - 8,
  ));
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function hideTurnTip() {
  const tip = $('#turnTip');
  if (tip) tip.hidden = true;
}

function scrollToTurn(index) {
  let turn = turnRailTurns[index];
  const container = $('#messages');
  if (!turn || !container) return;
  // 懒加载：点到的轮次可能还没渲染 → 先把窗口扩到它，再重新收集锚点。
  if (!turn.anchor) {
    if (!ensureMessageRendered(turn.messageIndex)) return;
    turnRailTurns = collectTurns();
    turn = turnRailTurns[index];
    if (!turn?.anchor) return;
  }
  // 把该轮的用户消息滚到视口垂直中心：这样"视口中心所在轮次"正好是点中的那一条，
  // 跳转后高亮不会跑到隔壁（否则跳转即高亮漂移，用户会以为点错了）。
  const base = container.getBoundingClientRect().top - container.scrollTop;
  const rowRect = turn.anchor.getBoundingClientRect();
  const top = rowRect.top - base - Math.max(0, (container.clientHeight - rowRect.height) / 2);
  container.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
}

export function initTurnRail() {
  const rail = $('#turnRail');
  const container = $('#messages');
  if (!rail || !container || turnRailBound) return;
  turnRailBound = true;
  container.addEventListener('scroll', scheduleTurnRail, { passive: true });
  // 懒加载：滚到接近顶部就往前预渲染一段（窗口按「轮」扩展，分割线不会与锚点分离）。
  container.addEventListener('scroll', () => {
    if (Date.now() < lazySuppressUntil) return;      // 渲染/程序化滚动，不算用户操作
    if (stickToBottom) return;                        // 仍在底部附近：没在翻历史
    if ((Number(state.renderStart) || 0) > 0 && container.scrollTop <= LAZY_TOP_TRIGGER) {
      extendRenderedWindow();
    }
  }, { passive: true });
  window.addEventListener('resize', scheduleTurnRail);
  window.addEventListener('scroll', hideTurnTip, true);
  rail.addEventListener('click', (event) => {
    const tick = event.target.closest('.turn-tick');
    if (tick) scrollToTurn(Number(tick.dataset.turnIndex));
  });
  rail.addEventListener('mouseover', (event) => {
    const tick = event.target.closest('.turn-tick');
    if (tick) showTurnTip(tick);
  });
  // 用 mouseleave（整条轨道）而不是每根线的 mouseout：块之间切换时不会闪。
  rail.addEventListener('mouseleave', hideTurnTip);
  rail.addEventListener('focusin', (event) => {
    const tick = event.target.closest('.turn-tick');
    if (tick) showTurnTip(tick);
  });
  rail.addEventListener('focusout', hideTurnTip);
  // 消息区结构变化（整轮渲染 / 流式追加 / 编辑重渲染）时重建刻度
  new MutationObserver(scheduleTurnRail).observe(container, { childList: true });
  scheduleTurnRail();
}


