// ============================================================
// 04-messages.js —— 拆分自 public/app.js 第 1212-1501 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, api, draggedFileCache, emptyStateElement, escapeHtml, state, toast } from "./01-core.js";
import { markdown } from "./02-markdown.js";
import { activityMarkup, attachmentThumbUrl, closeImageLightbox, fileChangesSummaryMarkup, fileUrl, mediaMarkup, reasoningMarkup, skillMarkup, sourcesMarkup, toolMarkup, updateContextUsage, usageMarkup } from "./03-media.js";
import { openConversation } from "./08-conversations.js";
import { renderPendingFiles } from "./10-upload.js";
import { hideChoiceButtons, sendMessage, showChoiceButtons } from "./12-chat-input.js";
import { hideSkillPopup, renderInputMirror, renderUserContent, resizeTextarea, updateSkillPopup } from "./13-skill-refs.js";
export function messageElement(message, temporary = false) {
  const row = document.createElement('article');
  row.className = `message-row ${message.role}`;
  row.dataset.messageId = message.id || '';
  const metadata = message.metadata || {};
  row.__messageMetadata = metadata;
  if (Array.isArray(metadata.attachments)) {
    metadata.attachments.forEach((attachment) => {
      const source = attachment.source || attachment.path;
      if (source && /\.(png|jpe?g|gif|webp)$/i.test(source)) preloadDraggedFile(source, attachment.name);
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
    const reasoningToolHtml = activityHtml || (reasoningMarkup(metadata.reasoning, true) + toolMarkup(metadata.tool_runs));
    // 当 activity 已内嵌正文（prose 条目）时，正文按时间交错展示，不再在末尾重复渲染；
    // 末尾的 answer-content 仅保留用于复制/检索（隐藏），避免与时间线重复。
    const hideBottomContent = activityHasProse && !temporary;
    row.innerHTML = `
      <div class="message-avatar">AI</div>
      <div class="message-card">
        <div class="message-body">
          ${skillMarkup(metadata.skills)}
          ${hideBottomContent ? abortedBadge : ''}
          ${reasoningToolHtml}
          ${temporary ? '<div class="run-activity activity">正在准备</div>' : ''}
          <div class="answer-content" data-raw="" ${hideBottomContent ? 'style="display:none"' : ''}>${temporary ? '' : abortedBadge + markdown(message.content)}</div>
          ${temporary ? '' : sourcesMarkup(metadata.sources)}
          ${mediaMarkup(metadata.attachments)}
          ${temporary ? '' : fileChangesSummaryMarkup(metadata.files)}
          ${temporary ? '' : usageMarkup({ ...(metadata.usage || {}), performance: metadata.performance || metadata.usage?.performance }, message.created_at)}
          ${temporary ? '' : `<div class="message-actions"><button data-copy-message>复制</button></div>`}
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

export function uploadedFileMarkup(files = []) {
  if (!files.length) return '';
  const html = files.map((file) => {
    const source = file.source || file.path || '';
    const isImage = /\.(png|jpe?g|webp|gif)$/i.test(source);
    if (isImage) {
      const thumbUrl = attachmentThumbUrl(file);
      const largeUrl = fileUrl(source);
      return `<figure class="attachment attachment-image"><img class="thumbnail" src="${escapeHtml(thumbUrl)}" alt="${escapeHtml(file.name || 'image')}" loading="lazy" draggable="true" data-large-url="${escapeHtml(largeUrl)}"><figcaption>${escapeHtml(file.name || '')}</figcaption></figure>`;
    }
    return `<span class="file-chip">${escapeHtml(file.name)}</span>`;
  }).join('');
  return `<div class="media-grid">${html}</div>`;
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

export function renderMessages(messages) {
  const container = $('#messages');
  const empty = emptyStateElement;
  closeImageLightbox();
  // 诊断日志：定位"消息消失"是数据为空还是渲染崩溃
  console.log('[naiba] renderMessages 调用, 消息数=', messages.length,
    'conversationId=', state.conversationId,
    'roles=', messages.map((m) => m.role).join(','));
  try {
    container.replaceChildren();
    // 始终保留 empty 在容器中，仅切换 hidden；否则它会被移出 DOM，
    // 导致后续 sendMessage 中 $('#emptyState') 为 null 而崩溃
    const visibleMessages = messages;
    empty.hidden = visibleMessages.length > 0;
    container.append(empty);
    if (visibleMessages.length) {
      visibleMessages.forEach((message) => container.append(messageElement(message)));
      scrollToBottom();
    }
    const choiceMessage = pendingChoiceMessage(visibleMessages);
    const choices = choiceMessage?.metadata?.choices || [];
    const choiceGroups = choiceMessage?.metadata?.choice_groups || [];
    if ((Array.isArray(choiceGroups) && choiceGroups.length) || (Array.isArray(choices) && choices.length)) {
      showChoiceButtons(choices, choiceGroups);
    }
    else hideChoiceButtons();
    updateContextUsage(messages);
  } catch (error) {
    console.error('[naiba] renderMessages 渲染崩溃:', error, '消息数=', messages.length);
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

