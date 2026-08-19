const urlToken = new URLSearchParams(location.search).get('token') || '';
if (urlToken) {
  localStorage.setItem('naibaChatToken', urlToken);
  history.replaceState(null, '', location.pathname);
}

const state = {
  token: urlToken || localStorage.getItem('naibaChatToken') || localStorage.getItem('lanSkillToken') || '',
  bootstrap: null,
  conversations: [],
  conversationId: '',
  selectedSkills: JSON.parse(localStorage.getItem('naibaChatSkillIds') || localStorage.getItem('lanSkillIds') || '[]'),
  autoSkills: (localStorage.getItem('naibaChatAutoSkills') ?? localStorage.getItem('lanAutoSkills')) === 'true',
  pendingFiles: [],
  abortController: null,
  chatRunId: '',
  runConversationId: '',
  runSequence: 0,
  runEvents: {},
  taskSubmitting: false,
  conversationSettingsId: '',
  providerEditing: false,
  providerIsNew: false,
  syncTimer: null,
  syncInFlight: false,
  conversationSnapshot: '',
  agentFormSkillIds: [],
  tasks: [],
  taskTimer: null,
  visionTimer: null,
  visionStartedAt: 0,
  webSearchEnabled: false,
  deepReasoningEnabled: false,
  contextUsage: null,
  providerModelCapabilities: {},
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const emptyStateElement = $('#emptyState');

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (options.body && typeof options.body !== 'string') {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { ...options, headers });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function toast(message) {
  const element = $('#toast');
  element.textContent = message;
  element.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove('show'), 2200);
}

async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (_) {
      // WebView and LAN HTTP pages may not grant the Clipboard API permission.
    }
  }

  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.left = '-9999px';
  document.body.append(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  textarea.remove();
  if (!copied) throw new Error('浏览器未允许访问剪贴板');
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function markdown(text) {
  const codeBlocks = [];
  let safe = escapeHtml(text).replace(/```([^\n]*)\n([\s\S]*?)```/g, (_, language, code) => {
    const index = codeBlocks.length;
    const lang = escapeHtml(language.trim());
    codeBlocks.push(
      `<div class="code-block"><div class="code-block-bar">` +
      `<span class="code-lang">${lang}</span>` +
      `<button type="button" class="code-copy" data-copy-code>复制</button>` +
      `</div><pre><code data-language="${lang}">${code}</code></pre></div>`
    );
    return `\n@@CODE_${index}@@\n`;
  });
  safe = safe
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  const blocks = safe.split(/\n{2,}/).map((block) => {
    const trimmed = block.trim();
    if (!trimmed) return '';
    const codeMatch = trimmed.match(/^@@CODE_(\d+)@@$/);
    if (codeMatch) return codeBlocks[Number(codeMatch[1])];
    if (/^[-*] /.test(trimmed)) {
      const items = trimmed.split('\n').map((line) => `<li>${line.replace(/^[-*] /, '')}</li>`).join('');
      return `<ul>${items}</ul>`;
    }
    return `<p>${trimmed.replaceAll('\n', '<br>')}</p>`;
  });
  return blocks.join('');
}

function fileUrl(source) {
  return `/api/file?token=${encodeURIComponent(state.token)}&path=${encodeURIComponent(source)}`;
}

function mediaMarkup(attachments = []) {
  if (!attachments.length) return '';
  const items = attachments.map((attachment) => {
    const source = attachment.source || attachment.path;
    const lower = String(source).toLowerCase().split('?')[0];
    const url = fileUrl(source);
    if (/\.(png|jpe?g|webp|gif)$/.test(lower)) {
      return `<a href="${url}" target="_blank"><img src="${url}" alt="${escapeHtml(attachment.name || '生成图片')}" loading="lazy"></a>`;
    }
    if (/\.(mp4|webm)$/.test(lower)) return `<video src="${url}" controls playsinline></video>`;
    if (/\.(wav|mp3|m4a)$/.test(lower)) return `<audio src="${url}" controls></audio>`;
    return `<a class="file-chip" href="${url}" target="_blank">${escapeHtml(attachment.name || '下载文件')}</a>`;
  }).join('');
  return `<div class="media-grid">${items}</div>`;
}

function toolMarkup(runs = []) {
  if (!runs.length) return '';
  return `<div class="tool-stack">${runs.map((run) => `
    <details class="tool-run">
      <summary>${run.success ? '已执行' : '执行失败'} · ${escapeHtml(run.tool)}${run.reason ? ` · ${escapeHtml(run.reason)}` : ''}</summary>
      <pre>${escapeHtml(JSON.stringify(run.arguments || {}, null, 2))}\n\n${escapeHtml(run.result || '')}</pre>
    </details>`).join('')}</div>`;
}

function reasoningMarkup(reasoning) {
  const list = Array.isArray(reasoning) ? reasoning.filter(Boolean) : (reasoning ? [reasoning] : []);
  if (!list.length) return '';
  const text = list.join('\n\n---\n\n');
  return `<details class="reasoning-block">
    <summary>思考过程（${list.length} 段）</summary>
    <div class="reasoning-content">${markdown(text)}</div>
  </details>`;
}

function usageMarkup(usage) {
  if (!usage || typeof usage !== 'object') return '';
  const input = Number(usage.input_tokens || 0);
  const output = Number(usage.output_tokens || 0);
  const cached = Number(usage.cached_tokens || 0);
  const total = Number(usage.total_tokens || input + output);
  if (!input && !output) return '';
  const rate = input ? Number(usage.cache_hit_rate ?? (cached / input * 100)).toFixed(1) : '0.0';
  const requests = Number(usage.requests || 1);
  return `<div class="usage-line" title="本轮 ${requests} 次模型请求">本轮 ${total.toLocaleString()} tokens · 输入 ${input.toLocaleString()} · 输出 ${output.toLocaleString()} · 缓存 ${cached.toLocaleString()}/${input.toLocaleString()}（${rate}%）</div>`;
}

function updateContextUsage(messages = null, message = null) {
  let target = message;
  if (!target && Array.isArray(messages)) {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index]?.role === 'assistant' && messages[index]?.metadata?.usage) {
        target = messages[index];
        break;
      }
    }
  }
  state.contextUsage = target?.metadata?.usage || null;
  renderContextUsage();
}

function renderContextUsage() {
  const button = $('#contextUsageButton');
  const ring = $('#contextUsageRing');
  const summary = $('#contextUsageSummary');
  const turn = $('#lastTurnUsageSummary');
  if (!button || !ring || !summary || !turn) return;
  const usage = state.contextUsage;
  if (!usage) {
    ring.style.setProperty('--context-percent', '0');
    ring.classList.remove('warning', 'danger');
    button.title = '上下文用量：暂无数据';
    summary.textContent = '暂无模型用量数据';
    turn.textContent = '完成一次回复后显示本轮消耗';
    return;
  }
  const input = Number(usage.input_tokens || 0);
  const output = Number(usage.output_tokens || 0);
  const total = Number(usage.total_tokens || input + output);
  const context = Number(usage.context_tokens || 0);
  const profiles = state.bootstrap?.model_profiles || state.bootstrap?.providers || [];
  const profile = profiles.find((item) => item.model_key === usage.model_key)
    || selectedProvider();
  const profileLimit = Number(profile?.context_window || 0);
  const trustedStoredLimit = usage.context_limit_source
    ? Number(usage.context_limit || 0)
    : 0;
  const limit = profileLimit || trustedStoredLimit;
  const percent = limit > 0 ? Math.min(100, Math.max(0, context / limit * 100)) : 0;
  ring.style.setProperty('--context-percent', percent.toFixed(1));
  ring.classList.toggle('warning', percent >= 70 && percent < 90);
  ring.classList.toggle('danger', percent >= 90);
  const contextText = limit
    ? `${context.toLocaleString()} / ${limit.toLocaleString()}（${percent.toFixed(1)}%）`
    : context ? `${context.toLocaleString()} / 上限未知` : '上下文上限未知';
  button.title = `上下文用量：${contextText}`;
  summary.textContent = `上下文 ${contextText}`;
  turn.textContent = `最近一轮：输入 ${input.toLocaleString()} · 输出 ${output.toLocaleString()} · 总计 ${total.toLocaleString()} tokens`;
}

// Legacy provider context_size: migrated to context_window and kept only for
// compatibility with older configuration readers.
// providerContextSize remains only as a hidden legacy selector marker;
// providerContextWindow is the active provider-scoped control.

function toggleContextUsagePopover(event) {
  event.stopPropagation();
  const popover = $('#contextUsagePopover');
  const button = $('#contextUsageButton');
  const open = popover.hidden;
  popover.hidden = !open;
  button.setAttribute('aria-expanded', String(open));
}

function closeContextUsagePopover() {
  const popover = $('#contextUsagePopover');
  const button = $('#contextUsageButton');
  if (!popover || popover.hidden) return;
  popover.hidden = true;
  button?.setAttribute('aria-expanded', 'false');
}

function skillMarkup(skills = []) {
  if (!Array.isArray(skills) || !skills.length) return '';
  const names = skills.map((skill) => escapeHtml(skill?.name || skill)).filter(Boolean);
  return names.length ? `<div class="skill-usage">已启用 Skill：${names.join('、')}</div>` : '';
}

function sourcesMarkup(sources = []) {
  if (!Array.isArray(sources) || !sources.length) return '';
  const items = sources.map((source) => {
    const url = String(source?.url || '');
    if (!/^https?:\/\//i.test(url)) return '';
    const title = escapeHtml(source?.title || url);
    const snippet = escapeHtml(source?.snippet || '');
    const published = escapeHtml(source?.published_at || '');
    return `<li><a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${title}</a>${published ? `<time>${published}</time>` : ''}${snippet ? `<p>${snippet}</p>` : ''}</li>`;
  }).filter(Boolean).join('');
  return items ? `<details class="message-sources"><summary>联网来源（${sources.length}）</summary><ol>${items}</ol></details>` : '';
}

function toolAvailabilityMarkup(tools = []) {
  if (!Array.isArray(tools) || !tools.length) return '';
  const items = tools.map((tool) => {
    const name = typeof tool === 'string' ? tool : tool?.name;
    if (!name) return '';
    const description = typeof tool === 'string' ? '' : String(tool?.description || '');
    return `<li><code>${escapeHtml(name)}</code>${description ? ` <span>${escapeHtml(description)}</span>` : ''}</li>`;
  }).filter(Boolean).join('');
  return items ? `<details class="tool-availability"><summary>Available tools (${tools.length})</summary><ul>${items}</ul></details>` : '';
}

function messageElement(message, temporary = false) {
  const row = document.createElement('article');
  row.className = `message-row ${message.role}`;
  row.dataset.messageId = message.id || '';
  const metadata = message.metadata || {};
  if (message.role === 'user') {
    row.innerHTML = `<div class="message-body">${markdown(message.content)}${uploadedFileMarkup(metadata.attachments)}${message.id ? `<div class="message-actions"><button data-edit-message title="编辑并从此处重新开始">编辑</button></div>` : ''}</div>`;
  } else {
    row.innerHTML = `
      <div class="message-avatar">AI</div>
      <div class="message-body">
        ${skillMarkup(metadata.skills)}
        ${reasoningMarkup(metadata.reasoning)}
        ${toolAvailabilityMarkup(metadata.allowed_tools)}
        ${toolMarkup(metadata.tool_runs)}
        ${temporary ? '<div class="run-activity activity">正在准备</div>' : ''}
        <div class="answer-content" data-raw="">${temporary ? '' : markdown(message.content)}</div>
        ${temporary ? '' : sourcesMarkup(metadata.sources)}
        ${mediaMarkup(metadata.attachments)}
        ${temporary ? '' : usageMarkup(metadata.usage)}
        ${temporary ? '' : `<div class="message-actions"><button data-copy-message>复制</button></div>`}
      </div>`;
  }
  return row;
}

function startEditMessage(row) {
  if (!row || state.chatRunId) {
    if (state.chatRunId) toast('请先停止当前对话再编辑');
    return;
  }
  const body = row.querySelector('.message-body');
  if (!body || body.querySelector('textarea[data-edit-input]')) return;
  // 提取纯文本内容（不含附件标记）
  const textContent = body.childNodes[0]?.textContent ?? body.textContent;
  const currentText = row.dataset.rawContent || textContent.trim();
  row.dataset.rawContent = currentText;
  body.innerHTML = `
    <textarea class="edit-input" data-edit-input rows="3">${escapeHtml(currentText)}</textarea>
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

async function confirmEditMessage(row, newText) {
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
    // 恢复原消息的附件，供重发使用
    state.pendingFiles = (result.attachments || []).map((f) => ({ name: f.name, path: f.path, size: f.size }));
    renderPendingFiles();
    // 截断后重新渲染会话（被编辑的消息已从历史消失）
    await openConversation(state.conversationId);
    // 填入新内容并重发
    const input = $('#messageInput');
    input.value = text;
    resizeTextarea();
    await sendMessage();
  } catch (error) {
    toast(`编辑失败：${error.message}`);
    if (state.conversationId) openConversation(state.conversationId);
  }
}

function uploadedFileMarkup(files = []) {
  if (!files.length) return '';
  const html = files.map((file) => {
    const source = file.source || file.path || '';
    const isImage = /\.(png|jpe?g|gif|webp)$/i.test(source);
    if (isImage) {
      const src = fileUrl(source);
      return `<figure class="attachment attachment-image"><a href="${src}" target="_blank" rel="noreferrer"><img src="${src}" alt="${escapeHtml(file.name || 'image')}" loading="lazy"></a><figcaption>${escapeHtml(file.name || '')}</figcaption></figure>`;
    }
    return `<span class="file-chip">${escapeHtml(file.name)}</span>`;
  }).join('');
  return `<div class="media-grid">${html}</div>`;
}

function scrollToBottom() {
  const messages = $('#messages');
  messages.scrollTop = messages.scrollHeight;
}

function renderMessages(messages) {
  const container = $('#messages');
  const empty = emptyStateElement;
  // 诊断日志：定位"消息消失"是数据为空还是渲染崩溃
  console.log('[naiba] renderMessages 调用, 消息数=', messages.length,
    'conversationId=', state.conversationId,
    'roles=', messages.map((m) => m.role).join(','));
  try {
    container.replaceChildren();
    // 始终保留 empty 在容器中，仅切换 hidden；否则它会被移出 DOM，
    // 导致后续 sendMessage 中 $('#emptyState') 为 null 而崩溃
    empty.hidden = messages.length > 0;
    container.append(empty);
    if (messages.length) {
      messages.forEach((message) => container.append(messageElement(message)));
      scrollToBottom();
    }
    const choiceMessage = pendingChoiceMessage(messages);
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

function pendingChoiceMessage(messages) {
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

async function authenticate(token) {
  const response = await fetch('/api/auth', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  });
  if (!response.ok) throw new Error('访问口令不正确');
  state.token = token;
  localStorage.setItem('naibaChatToken', token);
}

async function initialize() {
  if (!state.token) {
    $('#authDialog').showModal();
    return;
  }
  try {
    state.bootstrap = await api('/api/bootstrap');
  } catch (error) {
    state.token = '';
    localStorage.removeItem('naibaChatToken');
    localStorage.removeItem('lanSkillToken');
    $('#authDialog').showModal();
    return;
  }
  const migration = state.bootstrap.data_location?.migration;
  if (migration?.migrated) {
    const restored = [
      migration.config ? 'API 配置' : '',
      migration.data ? '对话数据' : '',
    ].filter(Boolean).join('和');
    toast(`已从旧目录恢复${restored || '数据'}`);
  }
  $('#serverDot').className = 'connected';
  $('#serverLabel').textContent = '服务已连接';
  $('#lanAddress').textContent = state.bootstrap.lan_url;
  $('#connectionAddress').textContent = state.bootstrap.lan_url;
  $('#autoSkills').checked = state.autoSkills;
  populateModels();
  renderAgents();
  renderAgentManager();
  // 恢复模式 Tab 状态
  $$('.mode-tab').forEach(tab => tab.classList.toggle('active', tab.dataset.mode === state.mode));
  populateRuntimeSettings();
  populateAgentSettings();
  populateVisionSettings();
  populateSearchSettings();
  renderSkills();
  renderProviders();
  renderMcp();
  renderUpdateStatus(state.bootstrap.update || {});
  await loadConversations();
  await loadTasks();
  startTaskSync();
  startConversationSync();
  startMcpPoll();
}

const activeTaskStatuses = new Set(['queued', 'running', 'waiting', 'cancelling']);

async function loadTasks() {
  try {
    const result = await api('/api/tasks');
    const previous = new Map(state.tasks.map((task) => [task.id, task.status]));
    state.tasks = result.tasks || [];
    renderRunTasks();
    for (const task of state.tasks) {
      if (previous.has(task.id) && previous.get(task.id) !== task.status && ['completed', 'failed', 'cancelled'].includes(task.status)) {
        if (task.status === 'completed') toast(`${task.agent_name} 的任务已完成`);
        if (task.conversation_id === state.conversationId) syncCurrentConversation();
      }
    }
  } catch (error) {
    console.debug('[naiba] 任务同步失败:', error.message);
  }
}

function startTaskSync() {
  if (state.taskTimer) return;
  state.taskTimer = window.setInterval(loadTasks, 1500);
}

function taskStatusLabel(status) {
  return ({ queued: '排队中', running: '运行中', waiting: '等待确认', cancelling: '取消中', completed: '已完成', failed: '失败', cancelled: '已取消' })[status] || status;
}

function currentPermissionMode() {
  const conversation = state.conversations.find((item) => item.id === state.conversationId);
  const mode = conversation?.permission_mode || 'confirm';
  return ['confirm', 'auto', 'full'].includes(mode) ? mode : 'confirm';
}

function renderPermissionModeSwitch() {
  const mode = currentPermissionMode();
  $$('#permissionModeSwitch [data-permission-mode]').forEach((button) => {
    const active = button.dataset.permissionMode === mode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-checked', String(active));
    button.disabled = !state.conversationId;
  });
}

async function switchPermissionMode(mode) {
  if (!state.conversationId || !['confirm', 'auto', 'full'].includes(mode) || mode === currentPermissionMode()) return;
  if (mode === 'full' && !confirm('完全访问会允许此对话的 Agent 无需逐次确认即可操作本机文件、命令、网络和 MCP。确认启用？')) {
    renderPermissionModeSwitch();
    return;
  }
  const conversationId = state.conversationId;
  try {
    const updated = await api(`/api/conversations/${conversationId}/settings`, {
      method: 'POST',
      body: { permission_mode: mode },
    });
    if (state.conversationId !== conversationId) return;
    const index = state.conversations.findIndex((item) => item.id === conversationId);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
    renderPermissionModeSwitch();
    const names = { confirm: '请求批准', auto: '替我审批', full: '完全访问' };
    toast(`当前对话已切换为“${names[mode]}”`);
  } catch (error) {
    renderPermissionModeSwitch();
    toast(`切换审批模式失败：${error.message}`);
  }
}

// ---- 计划（Plan 模式） ----

function planStatusLabel(status) {
  return ({ prepare: '准备中', ready: '待确认', building: '执行中', finished: '已完成', failed: '执行失败', cancelled: '已取消' })[status] || status;
}

async function loadPlans() {
  const requestSeq = ++state.planLoadSeq;
  const conversationId = state.conversationId;
  if (!state.conversationId) {
    state.plans = [];
    renderPlanBar();
    return;
  }
  try {
    const result = await api(`/api/plans?conversation_id=${encodeURIComponent(conversationId)}`);
    // Timers and run completion may overlap; ignore responses for an older state.
    if (requestSeq !== state.planLoadSeq || conversationId !== state.conversationId) return;
    state.plans = result.plans || [];
  } catch (error) {
    if (requestSeq !== state.planLoadSeq || conversationId !== state.conversationId) return;
    console.debug('[naiba] 计划同步失败:', error.message);
  }
  renderPlanBar();
  fillPlanCards();
}

function activePlan() {
  // Older plans are history and must not restore actions after the newest plan ends.
  const plan = state.plans[0];
  return plan && ['prepare', 'ready', 'building', 'failed', 'cancelled'].includes(plan.status)
    ? plan
    : null;
}

function renderPlanBar() {
  const bar = $('#planBar');
  if (!bar) return;
  const plan = activePlan();
  if (!plan || !state.conversationId || currentInteractionMode() !== 'plan') {
    bar.hidden = true;
    bar.innerHTML = '';
    return;
  }
  bar.hidden = false;
  const steps = Array.isArray(plan.steps) ? plan.steps : [];
  const done = steps.filter((step) => step.status === 'done').length;
  const planId = escapeHtml(plan.id);
  let text = '';
  let actions = '';
  if (plan.status === 'prepare') {
    text = '计划准备中：正在澄清需求或生成方案，请直接回复我的问题';
    actions = `<button type="button" data-plan-action="cancel" data-plan-id="${planId}">取消计划</button>`;
  } else if (plan.status === 'ready') {
    text = `方案已就绪：《${escapeHtml(plan.title || '实施计划')}》（${steps.length} 步），请审阅`;
    actions = `<button type="button" data-plan-action="keep-planning" data-plan-id="${planId}">Keep planning</button>`
      + `<button type="button" data-plan-action="edit" data-plan-id="${planId}">编辑计划</button>`
      + `<button type="button" class="plan-primary" data-plan-action="execute" data-plan-id="${planId}">Approve</button>`;
  } else if (plan.status === 'building') {
    const current = steps.find((step) => step.status === 'running');
    text = `正在执行《${escapeHtml(plan.title || '实施计划')}》 ${done}/${steps.length} 步`
      + (current ? `：${escapeHtml(current.title)}` : '')
      + (plan.detail?.message ? ` · ${escapeHtml(plan.detail.message)}` : '');
    if (plan.detail?.confirm_id) {
      const confirmId = escapeHtml(plan.detail.confirm_id);
      actions = `<button type="button" data-plan-action="reject" data-confirm-id="${confirmId}">拒绝</button>`
        + `<button type="button" class="plan-primary" data-plan-action="confirm" data-confirm-id="${confirmId}">确认执行</button>`;
    }
    actions += `<button type="button" data-plan-action="cancel" data-plan-id="${planId}">取消</button>`;
  } else if (plan.status === 'failed') {
    text = `《${escapeHtml(plan.title || '实施计划')}》执行失败（${done}/${steps.length} 步已完成）：${escapeHtml(plan.error || '未知错误')}`;
    actions = `<button type="button" data-plan-action="edit" data-plan-id="${planId}">编辑计划</button>`
      + `<button type="button" class="plan-primary" data-plan-action="execute" data-plan-id="${planId}">继续执行</button>`
      + `<button type="button" data-plan-action="cancel" data-plan-id="${planId}">取消</button>`;
  } else if (plan.status === 'cancelled') {
    text = `计划已取消：《${escapeHtml(plan.title || '实施计划')}》（${done}/${steps.length} 步已完成）`;
    actions = `<button type="button" data-plan-action="edit" data-plan-id="${planId}">编辑计划</button>`
      + `<button type="button" class="plan-primary" data-plan-action="execute" data-plan-id="${planId}">继续执行</button>`;
  }
  bar.innerHTML = `<span class="plan-bar-status plan-status-${escapeHtml(plan.status)}">${planStatusLabel(plan.status)}</span>`
    + `<span class="plan-bar-text">${text}</span>`
    + `<span class="plan-bar-actions">${actions}</span>`;
}

function fillPlanCards() {
  $$('[data-plan-card]').forEach((slot) => {
    const plan = state.plans.find((item) => item.id === slot.dataset.planCard);
    slot.innerHTML = plan ? planCardMarkup(plan) : '';
  });
}

function planCardMarkup(plan) {
  const steps = Array.isArray(plan.steps) ? plan.steps : [];
  const done = steps.filter((step) => step.status === 'done').length;
  const stepIcons = { pending: '○', running: '◌', done: '✓', failed: '✗' };
  const stepsHtml = steps.length
    ? `<ol class="plan-steps">${steps.map((step) => `
      <li class="plan-step plan-step-${escapeHtml(step.status)}">
        <span class="plan-step-icon">${stepIcons[step.status] || '○'}</span>
        <span class="plan-step-title">${escapeHtml(step.title)}</span>
        ${step.summary ? `<span class="plan-step-summary">${escapeHtml(step.summary)}</span>` : ''}
      </li>`).join('')}</ol>`
    : '';
  const contentHtml = plan.content
    ? `<details class="plan-content"><summary>方案详情</summary><div class="plan-content-body">${markdown(plan.content)}</div></details>`
    : '';
  const archive = plan.archive_path
    ? `<a class="plan-archive" href="${fileUrl(plan.archive_path)}" target="_blank" rel="noreferrer">归档</a>`
    : '';
  return `<div class="plan-card-inner">
    <div class="plan-card-head">
      <span class="plan-card-badge plan-status-${escapeHtml(plan.status)}">${planStatusLabel(plan.status)}</span>
      <b class="plan-card-title">${escapeHtml(plan.title || '实施计划')}</b>
      ${steps.length ? `<span class="plan-card-progress">${done}/${steps.length}</span>` : ''}
      ${archive}
    </div>
    ${stepsHtml}
    ${contentHtml}
    ${plan.error ? `<div class="plan-card-error">${escapeHtml(plan.error)}</div>` : ''}
  </div>`;
}

async function executePlan(planId) {
  try {
    const run = await api(`/api/plans/${planId}/execute`, {
      method: 'POST',
      body: { web_search_enabled: state.webSearchEnabled },
    });
    const plan = state.plans.find((item) => item.id === planId);
    if (plan) {
      plan.status = 'building';
      plan.detail = { ...(plan.detail || {}), message: '准备执行计划', run_id: run?.id || '' };
      renderPlanBar();
      fillPlanCards();
    }
    if (run?.id && run.conversation_id === state.conversationId) {
      await resumeRun(run);
    }
    const index = state.conversations.findIndex((item) => item.id === state.conversationId);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], interaction_mode: 'craft' };
    state.interactionMode = 'craft';
    localStorage.setItem('naibaChatInteractionMode', 'craft');
    renderModeSwitch();
    toast('计划已开始执行');
  } catch (error) {
    toast(`执行失败：${error.message}`);
  }
  await loadPlans();
}

async function keepPlanning(planId) {
  try {
    await api(`/api/plans/${planId}/keep-planning`, { method: 'POST', body: {} });
    await loadPlans();
    $('#messageInput').focus();
    toast('已保留计划，继续规划');
  } catch (error) {
    toast(`继续规划失败：${error.message}`);
  }
}

async function cancelPlan(planId) {
  const plan = state.plans.find((item) => item.id === planId);
  if (plan?.status === 'prepare' && state.chatRunId) {
    await cancelCurrentRun();
  }
  try {
    await api(`/api/plans/${planId}/cancel`, { method: 'POST', body: {} });
  } catch (error) {
    toast(`取消失败：${error.message}`);
  }
  await loadPlans();
}

async function resolvePlanConfirmation(confirmId, approved) {
  const runId = String(activePlan()?.detail?.run_id || state.chatRunId || '');
  if (!runId) {
    toast('找不到该确认所属的 Run');
    return;
  }
  try {
    await api(approved ? '/api/tool/confirm' : '/api/tool/reject', {
      method: 'POST', body: { run_id: runId, confirm_id: confirmId },
    });
  } catch (error) {
    toast(`处理确认失败：${error.message}`);
  }
  await loadPlans();
}

function openPlanEditor(planId) {
  const plan = state.plans.find((item) => item.id === planId);
  if (!plan) return;
  state.planEditingId = planId;
  $('#planEditTitle').value = plan.title || '';
  $('#planEditContent').value = plan.content || '';
  $('#planEditError').textContent = '';
  $('#planEditDialog').showModal();
}

async function savePlanEdit(event) {
  event.preventDefault();
  const planId = state.planEditingId;
  if (!planId) return;
  const button = $('#savePlanEdit');
  button.disabled = true;
  try {
    await api(`/api/plans/${planId}`, {
      method: 'PUT',
      body: { title: $('#planEditTitle').value, content: $('#planEditContent').value },
    });
    $('#planEditDialog').close();
    toast('计划已保存');
    await loadPlans();
  } catch (error) {
    $('#planEditError').textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function renderUpdateStatus(status) {
  const current = status.current_version || '开发版';
  const latest = status.latest_version || '尚未检查';
  $('#currentVersion').textContent = status.current_commit ? `${current} · ${status.current_commit.slice(0, 7)}` : current;
  $('#latestVersion').textContent = status.latest_commit ? `${latest} · ${status.latest_commit.slice(0, 7)}` : latest;
  const notes = Array.isArray(status.release_notes)
    ? status.release_notes.filter((note) => String(note || '').trim())
    : (status.release_notes ? [String(status.release_notes)] : []);
  const notesPanel = $('#updateNotes');
  const notesList = $('#updateNotesList');
  notesList.replaceChildren(...notes.map((note) => {
    const item = document.createElement('li');
    item.textContent = note;
    return item;
  }));
  // 仅根据是否存在更新内容显示/隐藏详情；不重置用户已展开/收起状态
  notesPanel.hidden = notes.length === 0;
  const messages = {
    idle: '启动后会自动从 GitHub 检查并安装更新。',
    checking: '正在检查更新…',
    current: '当前已经是最新版本。',
    available: '发现新版本，可以立即安装。',
    downloading: '正在下载并校验更新，请勿关闭程序。',
    restarting: '更新已准备好，程序即将重启。',
    error: status.error || '检查更新失败。',
  };
  $('#updateMessage').textContent = !status.supported
    ? '当前运行目录不支持自动更新，请确认它来自受支持的 Git 仓库。'
    : (messages[status.phase] || messages.idle);
  if (status.mode === 'source' && status.source_dirty && status.update_available) {
    $('#updateMessage').textContent = '发现新提交，但工作区有未提交修改；请先提交或清理后再更新。';
  }
  $('#installUpdate').hidden = !status.update_available || ['downloading', 'restarting'].includes(status.phase);
  $('#checkUpdate').disabled = ['checking', 'downloading', 'restarting'].includes(status.phase);
}

async function checkUpdate() {
  const button = $('#checkUpdate');
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  button.disabled = true;
  renderUpdateStatus({ ...(state.bootstrap.update || {}), phase: 'checking' });
  try {
    let status = await api('/api/update/check', { method: 'POST', body: {}, signal: controller.signal });
    state.bootstrap.update = status;
    renderUpdateStatus(status);
    const startedAt = Date.now();
    while (status.phase === 'checking' && Date.now() - startedAt < 30000) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      status = await api('/api/update');
      state.bootstrap.update = status;
      renderUpdateStatus(status);
    }
    if (status.phase === 'checking') {
      renderUpdateStatus({ ...status, phase: 'error', error: '检查更新超时，请稍后重试。' });
    }
  } catch (error) {
    try {
      const status = await api('/api/update');
      state.bootstrap.update = status;
      renderUpdateStatus(status.phase === 'checking'
        ? { ...status, phase: 'error', error: '检查更新超时，请稍后重试。' }
        : status);
    } catch (_) {
      renderUpdateStatus({ ...(state.bootstrap.update || {}), phase: 'error', error: error.message });
    }
  } finally {
    clearTimeout(timeout);
    button.disabled = false;
  }
}

async function installUpdate() {
  const button = $('#installUpdate');
  button.disabled = true;
  try {
    const status = await api('/api/update/install', { method: 'POST', body: {} });
    state.bootstrap.update = status;
    renderUpdateStatus(status);
    toast('正在下载更新，完成后会自动重启');
  } catch (error) {
    toast(`更新失败：${error.message}`);
    button.disabled = false;
  }
}

function populateModels() {
  const select = $('#modelSelect');
  const previous = select.value;
  const profiles = state.bootstrap.model_profiles || state.bootstrap.providers || [];
  const defaultKey = String(state.bootstrap.default_model_key || '');
  select.innerHTML = '';

  if (!profiles.length) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = '请先在设置中添加模型';
    select.append(opt);
  } else {
    const online = profiles.filter((p) => (p.kind || 'online') === 'online');
    const local = profiles.filter((p) => p.kind === 'local');
    const group = (label, list) => {
      if (!list.length) return;
      const og = document.createElement('optgroup');
      og.label = label;
      list.forEach((p) => {
        const option = document.createElement('option');
        option.value = p.model_key;
        option.textContent = `${p.name} · ${p.model}`;
        og.append(option);
      });
      select.append(og);
    };
    group('在线 API', online);
    group('本地模型', local);
  }

  if ([...select.options].some((o) => o.value === previous)) {
    select.value = previous;
  } else if (defaultKey && [...select.options].some((o) => o.value === defaultKey)) {
    select.value = defaultKey;
  } else if (select.options.length) {
    select.selectedIndex = 0;
  }
  updateUnloadModelButton();
}

function selectedProvider() {
  const value = $('#modelSelect')?.value || '';
  if (!value) return null;
  const profiles = state.bootstrap.model_profiles || state.bootstrap.providers || [];
  return profiles.find((p) => p.model_key === value) || null;
}

function localProviderKind(provider) {
  if (!provider) return '';
  const requestFormat = String(provider.request_format || '').toLowerCase();
  return ['ollama', 'lm_studio'].includes(requestFormat) ? requestFormat : '';
}

function updateUnloadModelButton() {
  const busy = Boolean(state.chatRunId || state.taskSubmitting);
  const topButton = $('#unloadModel');
  const topKind = localProviderKind(selectedProvider());
  if (topButton) {
    topButton.hidden = !topKind;
    topButton.disabled = busy;
    topButton.title = topKind ? `卸载${topKind === 'ollama' ? ' Ollama' : ' LM Studio'} 当前模型` : '当前供应商不支持手动卸载';
  }

  const settingsButton = $('#unloadProviderModel');
  if (settingsButton) {
    const providerId = $('#providerId')?.value || '';
    const provider = (state.bootstrap?.providers || []).find((item) => item.id === providerId);
    const kind = localProviderKind(provider);
    settingsButton.hidden = !kind;
    settingsButton.disabled = busy;
    settingsButton.title = kind ? `卸载${kind === 'ollama' ? ' Ollama' : ' LM Studio'} 当前模型` : '';
  }
}

async function unloadProviderModel(provider) {
  const kind = localProviderKind(provider);
  if (!provider || !kind) {
    toast('当前供应商不是支持卸载的本地模型');
    return;
  }
  if (state.chatRunId || state.taskSubmitting) {
    toast('请先等待当前对话结束');
    return;
  }
  if (!confirm(`卸载${kind === 'ollama' ? ' Ollama' : ' LM Studio'} 模型“${provider.model}”？`)) return;
  $('#unloadModel').disabled = true;
  $('#unloadProviderModel').disabled = true;
  try {
    const result = await api('/api/models/unload', {
      method: 'POST',
      body: { model_key: provider.model_key },
    });
    toast(`${result.provider} 模型已卸载，显存和内存将被回收`);
  } catch (error) {
    toast(`卸载失败：${error.message}`);
  } finally {
    updateUnloadModelButton();
  }
}

async function unloadCurrentModel() {
  await unloadProviderModel(selectedProvider());
}

async function unloadConfiguredProviderModel() {
  const providerId = $('#providerId').value;
  const provider = (state.bootstrap.model_profiles || state.bootstrap.providers || []).find((item) => item.id === providerId);
  await unloadProviderModel(provider);
}

async function saveModelSelection() {
  const value = $('#modelSelect').value;
  const result = await api('/api/settings', { method: 'POST', body: { model_key: value } });
  Object.assign(state.bootstrap.settings, result.settings);
  state.bootstrap.default_model_key = result.default_model_key || value;
  if (state.conversationId) {
    try {
      await api(`/api/conversations/${state.conversationId}/settings`, {
        method: 'POST',
        body: { model_key: value },
      });
    } catch (error) {
      console.debug('[naiba] 保存对话模型失败:', error.message);
    }
  }
  populateModels();
  toast('模型已切换');
}

// 根据对话已保存的 model_key 恢复模型选择；未绑定或已删除时回退到全局默认
function applyConversationModel(conversation) {
  const select = $('#modelSelect');
  if (!select) return;
  const target = String(conversation?.model_key || '');
  if (target && [...select.options].some((o) => o.value === target)) {
    select.value = target;
    updateUnloadModelButton();
    return;
  }
  const fallback = String(state.bootstrap.default_model_key || '');
  if (fallback && [...select.options].some((o) => o.value === fallback)) {
    select.value = fallback;
  } else if (select.options.length) {
    select.selectedIndex = 0;
  }
  updateUnloadModelButton();
}

function renderAgents() {
  const select = $('#agentSelect');
  if (!select) return;
  const agents = state.bootstrap?.agents || [];
  select.innerHTML = '';
  agents.forEach((agent) => {
    const option = document.createElement('option');
    option.value = agent.id;
    option.textContent = agent.name;
    select.append(option);
  });
  if (!select.options.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = '暂无 Agent';
    select.append(option);
  }
  applyConversationAgent(state.conversations.find((item) => item.id === state.conversationId));
}

// 根据对话已保存的 agent_id 恢复 Agent 选择；未绑定或已删除时回退到默认 Agent
function applyConversationAgent(conversation) {
  const select = $('#agentSelect');
  if (!select) return;
  const agents = state.bootstrap?.agents || [];
  const agentId = String(conversation?.agent_id || '');
  if (agentId && agents.some((agent) => agent.id === agentId) && [...select.options].some((o) => o.value === agentId)) {
    select.value = agentId;
  } else {
    const fallback = String(state.bootstrap?.default_agent_id || '');
    if ([...select.options].some((o) => o.value === fallback)) {
      select.value = fallback;
    } else if (select.options.length) {
      select.selectedIndex = 0;
    }
  }
  renderSkills($('#skillSearch')?.value || '');
}

async function saveAgentSelection() {
  const value = $('#agentSelect').value;
  if (!state.conversationId) {
    toast('请先打开或新建一个对话');
    return;
  }
  try {
    const updated = await api(`/api/conversations/${state.conversationId}/settings`, {
      method: 'POST',
      body: { agent_id: value },
    });
    const index = state.conversations.findIndex((item) => item.id === state.conversationId);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
    renderConversations();
    renderSkills($('#skillSearch')?.value || '');
    toast('Agent 已切换');
  } catch (error) {
    toast(`切换失败：${error.message}`);
    applyConversationAgent(state.conversations.find((item) => item.id === state.conversationId));
  }
}

async function loadConversations() {
  const result = await api('/api/conversations');
  state.conversations = result.conversations;
  renderConversations();
  if (!state.conversationId && state.conversations.length) {
    await openConversation(state.conversations[0].id);
  } else if (!state.conversations.length) {
    renderMessages([]);
    renderPermissionModeSwitch();
  }
}

function renderConversations() {
  $('#conversationList').innerHTML = state.conversations.map((conversation) => `
    <div class="conversation-item ${conversation.id === state.conversationId ? 'active' : ''}" data-conversation-id="${conversation.id}">
      <button class="conversation-settings" title="对话设置" aria-label="${escapeHtml(conversation.title)} 的设置">⚙</button>
      <button class="conversation-open" title="${escapeHtml(conversation.title)}">${escapeHtml(conversation.title)}</button>
      <button class="delete-conversation" title="删除对话" aria-label="删除对话">删除</button>
    </div>`).join('');
}

async function createConversation() {
  detachRunSubscription();
  hideChoiceButtons();
  const conversation = await api('/api/conversations', {
    method: 'POST',
    body: {
      interaction_mode: 'craft',
      permission_mode: 'confirm',
      web_search_enabled: false,
      deep_reasoning_enabled: false,
      agent_id: $('#agentSelect')?.value || state.bootstrap?.default_agent_id || '',
    },
  });
  state.conversationId = conversation.id;
  state.conversations.unshift(conversation);
  renderConversations();
  applyConversationModel(conversation);
  applyConversationAgent(conversation);
  state.webSearchEnabled = Boolean(Number(conversation.web_search_enabled || 0));
  updateWebSearchButton();
  state.deepReasoningEnabled = Boolean(Number(conversation.deep_reasoning_enabled || 0));
  updateDeepReasoningButton();
  renderMessages([]);
  renderPermissionModeSwitch();
  closeSidebar();
  $('#messageInput').focus();
}

async function openConversation(id) {
  if (id !== state.conversationId) {
    detachRunSubscription();
    hideChoiceButtons();
  }
  const conversation = await api(`/api/conversations/${id}`);
  state.conversationId = id;
  const index = state.conversations.findIndex((item) => item.id === id);
  if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...conversation };
  state.conversationSnapshot = conversationSnapshot(conversation);
  console.log('[naiba] openConversation', id.slice(0, 8), '服务器返回消息数=', (conversation.messages || []).length);
  renderConversations();
  applyConversationModel(conversation);
  applyConversationAgent(conversation);
  renderMessages(conversation.messages || []);
  renderPermissionModeSwitch();
  // 联网搜索开关由对话数据库字段恢复，不依赖当前浏览器。
  state.webSearchEnabled = Boolean(Number(conversation.web_search_enabled || 0));
  updateWebSearchButton();
  state.deepReasoningEnabled = Boolean(Number(conversation.deep_reasoning_enabled || 0));
  updateDeepReasoningButton();
  await resumeConversationRun(id);
  closeSidebar();
}

function conversationSnapshot(conversation) {
  const messages = Array.isArray(conversation?.messages) ? conversation.messages : [];
  const last = messages.at(-1);
  return [conversation?.updated_at || '', messages.length, last?.id || '', last?.role || ''].join('|');
}

async function syncCurrentConversation() {
  if (state.syncInFlight || !state.conversationId || state.abortController) return;
  if (document.visibilityState === 'hidden') return;
  state.syncInFlight = true;
  const id = state.conversationId;
  try {
    const conversation = await api(`/api/conversations/${id}`);
    if (state.conversationId !== id || state.abortController) return;
    const snapshot = conversationSnapshot(conversation);
    if (snapshot === state.conversationSnapshot) return;
    const index = state.conversations.findIndex((item) => item.id === id);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...conversation };
    state.conversationSnapshot = snapshot;
    renderConversations();
    renderMessages(conversation.messages || []);
    renderPermissionModeSwitch();
    state.webSearchEnabled = Boolean(Number(conversation.web_search_enabled || 0));
    updateWebSearchButton();
    state.deepReasoningEnabled = Boolean(Number(conversation.deep_reasoning_enabled || 0));
    updateDeepReasoningButton();
  } catch (error) {
    console.debug('[naiba] 对话同步失败:', error.message);
  } finally {
    state.syncInFlight = false;
  }
}

function startConversationSync() {
  if (state.syncTimer) return;
  state.syncTimer = window.setInterval(syncCurrentConversation, 1800);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') syncCurrentConversation();
  });
}

function taskModeLabel(task) {
  return '普通';
}

function taskElapsed(task) {
  const start = Number(task.started_at || task.created_at || 0);
  const end = Number(task.finished_at || Date.now());
  if (!start || end < start) return '';
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

function renderRunTasks() {
  const active = state.tasks.filter((task) => activeTaskStatuses.has(task.status));
  $('#taskCount').textContent = String(active.length);
  $('#openTasks').classList.toggle('has-active', active.length > 0);
  const current = active.filter((task) => task.conversation_id === state.conversationId);
  const bar = $('#activeTaskBar');
  bar.hidden = current.length === 0;
  if (current.length) {
    bar.innerHTML = `当前对话有 ${current.length} 个 Run 正在执行。<button type="button" data-open-tasks>查看</button>`;
  }
  const list = $('#taskList');
  if (!state.tasks.length) {
    list.innerHTML = '<div class="task-empty">暂无异步任务</div>';
    return;
  }
  list.innerHTML = state.tasks.map((task) => {
    const conversation = state.conversations.find((item) => item.id === task.conversation_id);
    const detail = task.error || task.detail?.message || '';
    return `<div class="task-item" data-task-id="${escapeHtml(task.id)}">
      <div class="task-title">${escapeHtml(task.message)}</div>
      <div class="task-meta">${escapeHtml(taskModeLabel(task))} · ${escapeHtml(task.agent_name)} · ${escapeHtml(conversation?.title || '原对话')} · ${escapeHtml(taskElapsed(task))}</div>
      <div class="task-detail">${escapeHtml(detail)}</div>
      <div class="task-actions"><span class="task-status ${escapeHtml(task.status)}">${taskStatusLabel(task.status)}</span></div>
    </div>`;
  }).join('');
}

function openConversationSettings(id) {
  const conversation = state.conversations.find((item) => item.id === id);
  if (!conversation) return;
  state.conversationSettingsId = id;
  $('#conversationSettingsTitle').textContent = conversation.title || '当前对话';
  $('#conversationTitle').value = conversation.title_customized ? (conversation.title || '') : '';
  $('#conversationSystemPrompt').value = conversation.system_prompt || '';
  $('#conversationStreamEnabled').checked = Number(conversation.stream_enabled ?? 1) !== 0;
  $('#conversationSettingsDialog').showModal();
}

async function saveConversationSettings(event) {
  event.preventDefault();
  const id = state.conversationSettingsId;
  if (!id) return;
  const saveButton = $('#saveConversationSettings');
  saveButton.disabled = true;
  try {
    const updated = await api(`/api/conversations/${id}/settings`, {
      method: 'POST',
      body: {
        title: $('#conversationTitle').value,
        system_prompt: $('#conversationSystemPrompt').value,
        stream_enabled: $('#conversationStreamEnabled').checked,
      },
    });
    const index = state.conversations.findIndex((item) => item.id === id);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
    $('#conversationSettingsDialog').close();
    renderConversations();
    toast('对话设置已保存');
  } catch (error) {
    toast(`保存失败：${error.message}`);
  } finally {
    saveButton.disabled = false;
  }
}

async function deleteConversation(id) {
  if (id === state.conversationId && state.chatRunId) {
    toast('请先停止当前回复再删除对话');
    return;
  }
  const conversation = state.conversations.find((item) => item.id === id);
  if (!confirm(`删除对话"${conversation?.title || '新对话'}"？`)) return;
  await api(`/api/conversations/${id}`, { method: 'DELETE' });
  state.conversations = state.conversations.filter((item) => item.id !== id);
  if (state.conversationId === id) {
    state.conversationId = '';
  }
  renderConversations();
  if (state.conversations.length) await openConversation(state.conversations[0].id);
  else {
    renderMessages([]);
  }
}

// 当前对话绑定的 Agent 的固定 Skill id 列表；未绑定或已删除时回退到默认 Agent
function currentAgentFixedSkillIds() {
  const agents = state.bootstrap?.agents || [];
  const conversation = state.conversations.find((item) => item.id === state.conversationId);
  let agentId = String(conversation?.agent_id || '');
  let agent = agents.find((item) => item.id === agentId);
  if (!agent) {
    agentId = String(state.bootstrap?.default_agent_id || '');
    agent = agents.find((item) => item.id === agentId);
  }
  return (agent?.skill_ids || []).map(String);
}

// 有效启用的 Skill = Agent 固定 Skill + 手动勾选 Skill（去重）
function effectiveSkillIds() {
  return [...new Set([...currentAgentFixedSkillIds(), ...state.selectedSkills])];
}

function renderSkills(filter = '') {
  if (!state.bootstrap) return;
  const query = filter.trim().toLowerCase();
  const fixed = new Set(currentAgentFixedSkillIds());
  const effective = new Set(effectiveSkillIds());
  const skills = state.bootstrap.skills.filter((skill) =>
    !query || `${skill.name} ${skill.description}`.toLowerCase().includes(query));
  $('#skillList').innerHTML = skills.map((skill) => `
    <label class="skill-item${fixed.has(skill.id) ? ' fixed' : ''}">
      <input type="checkbox" value="${skill.id}" ${effective.has(skill.id) ? 'checked' : ''} ${fixed.has(skill.id) ? 'disabled' : ''}>
      <span><b>${escapeHtml(skill.name)}</b><p>${escapeHtml(skill.description)}</p></span>
      ${fixed.has(skill.id) ? '<em>Agent 固定</em>' : (skill.script_count ? `<em>${skill.script_count} 脚本</em>` : '')}
    </label>`).join('');
  updateSkillSummary();
}

function updateSkillSummary() {
  const fixed = currentAgentFixedSkillIds().length;
  const effective = effectiveSkillIds().length;
  $('#skillCount').textContent = state.autoSkills ? '自动' : String(effective);
  $('#skillsSummary').textContent = `${state.bootstrap.skills.length} 个可用，${effective} 个启用${fixed ? `（含 ${fixed} 个 Agent 固定）` : ''}`;
}

function renderProviders() {
  const providers = state.bootstrap.model_profiles || state.bootstrap.providers || [];
  const select = $('#providerSelect');
  select.innerHTML = providers.length
    ? providers.map((provider) => {
        const tag = provider.kind === 'local' ? '[本地] ' : '[在线] ';
        return `<option value="${provider.id}">${escapeHtml(tag + provider.name)}</option>`;
      }).join('')
    : '<option value="">尚未添加供应商</option>';
  const currentId = $('#providerId').value;
  const current = providers.find((provider) => provider.id === currentId)
    || providers.find((provider) => provider.id === state.bootstrap.settings.provider_id)
    || providers[0];
  showProviderForm(current || {}, { editing: false });
}

function showProviderForm(provider = {}, { editing = false, isNew = false } = {}) {
  $('#providerId').value = provider.id || '';
  if (isNew) {
    $('#providerSelect').insertAdjacentHTML('beforeend', '<option value="__new__">正在添加新供应商</option>');
    $('#providerSelect').value = '__new__';
  } else {
    $('#providerSelect').value = provider.id || '';
  }
  $('#providerName').value = provider.name || '';
  $('#providerBaseUrl').value = provider.base_url || '';
  const inferredKind = provider.kind || (['ollama', 'lm_studio', 'llama_cpp'].includes(provider.request_format) ? 'local' : 'online');
  $('#providerKind').value = inferredKind === 'local' ? '1' : '0';
  $('#providerContextWindow').value = provider.context_window || provider.context_size || '';
  $('#providerMaxOutputTokens').value = provider.max_output_tokens || '';
  $('#providerTemperature').value = provider.temperature ?? '';
  $('#providerReasoningEffort').value = provider.reasoning_effort || 'auto';
  setProviderModelOptions([], provider.model || '');
  $('#providerFormat').value = provider.request_format || 'openai_chat';
  $('#providerApiKey').value = '';
  $('#providerApiKey').type = 'password';
  $('#toggleProviderKey').textContent = '显示';
  $('#toggleProviderKey').title = '显示 API Key';
  $('#providerKeyStatus').textContent = provider.has_api_key ? '已配置' : '未配置';
  $('#providerError').textContent = '';
  setProviderEditMode(editing, isNew);
  syncProviderKindOptions();
  updateProviderFormatGuide();
  updateProviderContextField();
  updateUnloadModelButton();
}

function syncProviderKindOptions() {
  const local = $('#providerKind').value === '1';
  const allowed = local ? ['lm_studio', 'ollama', 'llama_cpp'] : ['openai_chat', 'codex_responses', 'gemini', 'claude'];
  const format = $('#providerFormat');
  [...format.options].forEach((option) => { option.hidden = !allowed.includes(option.value); });
  if (!allowed.includes(format.value)) format.value = allowed[0];
  const hint = $('#providerKindHint');
  if (hint) hint.textContent = local ? '本地 API 使用 Ollama 或 LM Studio 服务。' : '在线 API 使用远程模型服务。';
}

function updateProviderFormatGuide() {
  const guide = $('#providerFormatGuide');
  const format = $('#providerFormat').value;
  const guides = {
    ollama: '先启动 Ollama。API URL 通常填写 http://127.0.0.1:11434/v1；API Key 可留空；模型名称可通过 ollama list 查看，然后点击“检查模型”。',
    lm_studio: '先在 LM Studio 的 Developer / Local Server 页面启动服务并加载模型。API URL 通常填写 http://127.0.0.1:1234/v1；API Key 可留空，然后点击“检查模型”。',
    llama_cpp: '先启动 llama.cpp server。API URL 通常填写 http://127.0.0.1:8080/v1；API Key 可留空，然后点击“检查模型”。',
  };
  guide.textContent = guides[format] || '';
  guide.hidden = !guides[format];
}

function updateProviderContextField() {
  const field = $('#providerContextField');
  const input = $('#providerContextWindow');
  const active = Boolean($('#providerId').value) || state.providerIsNew;
  field.hidden = !active;
  input.disabled = !state.providerEditing;
  $('#providerMaxOutputField').hidden = !active;
  $('#providerTemperatureField').hidden = !active;
  ['#providerMaxOutputTokens', '#providerTemperature'].forEach((selector) => {
    $(selector).disabled = !state.providerEditing;
  });
}

function setProviderEditMode(editing, isNew = false) {
  state.providerEditing = editing;
  state.providerIsNew = isNew;
  const active = Boolean($('#providerId').value) || isNew;
  $$('.provider-field').forEach((element) => { element.hidden = !active; });
  $('#providerEmpty').hidden = active;
  [
    '#providerName', '#providerBaseUrl', '#providerApiKey', '#providerFormat',
    '#providerKind', '#providerModel', '#providerModelCustom', '#providerContextWindow',
    '#providerMaxOutputTokens', '#providerTemperature', '#providerReasoningEffort',
  ].forEach((selector) => { $(selector).disabled = !editing; });
  $('#providerSelect').disabled = editing;
  $('#addProvider').disabled = editing;
  $('#deleteProvider').disabled = !$('#providerId').value || editing;
  $('#loadProviderModels').disabled = !editing;
  $('#testProvider').hidden = !active;
  $('#editProvider').hidden = !active || editing;
  $('#cancelProvider').hidden = !editing;
  $('#saveProvider').hidden = !editing;
  updateProviderContextField();
  updateUnloadModelButton();
}

function setProviderModelOptions(models = [], current = '') {
  const select = $('#providerModel');
  const unique = [];
  const seen = new Set();
  models.forEach((model) => {
    const id = String(model.id || '').trim();
    if (!id || seen.has(id)) return;
    seen.add(id);
    unique.push({ id, name: String(model.name || id) });
    state.providerModelCapabilities[id] = {
      context_window: model.context_window,
      max_output_tokens: model.max_output_tokens,
    };
  });
  if (current && !seen.has(current)) unique.unshift({ id: current, name: current });
  const prompt = unique.length > 1 && !current
    ? `<option value="">请选择模型（${unique.length} 个可用）</option>`
    : '';
  select.innerHTML = unique.length
    ? prompt + unique.map((model) => `<option value="${escapeHtml(model.id)}">${escapeHtml(model.name)}</option>`).join('')
    : '<option value="">填写连接信息后自动检查</option>';
  select.insertAdjacentHTML('beforeend', '<option value="__custom__">手动输入模型名称…</option>');
  select.value = current || (unique.length === 1 ? unique[0].id : '');
  $('#providerModelCustom').hidden = true;
  $('#providerModelCustom').required = false;
}

function applyProviderModelCapabilities() {
  const model = $('#providerModel').value;
  const capability = state.providerModelCapabilities[model];
  if (!capability) return;
  if (!$('#providerContextWindow').value && capability.context_window) {
    $('#providerContextWindow').value = capability.context_window;
  }
  if (!$('#providerMaxOutputTokens').value && capability.max_output_tokens) {
    $('#providerMaxOutputTokens').value = capability.max_output_tokens;
  }
}

function toggleCustomModel() {
  const custom = $('#providerModel').value === '__custom__';
  $('#providerModelCustom').hidden = !custom;
  $('#providerModelCustom').required = custom;
  if (custom) $('#providerModelCustom').focus();
}

function providerFormValue() {
  const selectedModel = $('#providerModel').value;
  const kind = $('#providerKind').value === '1' ? 'local' : 'online';
  const numberOrUndefined = (selector) => {
    const raw = $(selector).value.trim();
    return raw ? Number(raw) : undefined;
  };
  return {
    id: $('#providerId').value,
    name: $('#providerName').value.trim(),
    base_url: $('#providerBaseUrl').value.trim(),
    model: selectedModel === '__custom__' ? $('#providerModelCustom').value.trim() : selectedModel,
    api_key: $('#providerApiKey').value.trim(),
    kind,
    local_backend: kind === 'local' ? $('#providerFormat').value : undefined,
    request_format: $('#providerFormat').value,
    context_window: numberOrUndefined('#providerContextWindow'),
    max_output_tokens: numberOrUndefined('#providerMaxOutputTokens'),
    temperature: numberOrUndefined('#providerTemperature'),
    reasoning_effort: $('#providerReasoningEffort').value,
  };
}

async function loadProviderModels({ automatic = false } = {}) {
  if (!state.providerEditing) return;
  const values = providerFormValue();
  const localFormat = ['lm_studio', 'ollama', 'llama_cpp'].includes(values.request_format);
  if (!values.base_url || (!values.api_key && !values.id && !localFormat)) {
    if (!automatic) $('#providerError').textContent = '请先填写 API URL 和 API Key';
    return;
  }
  const button = $('#loadProviderModels');
  button.disabled = true;
  button.textContent = '检查中…';
  if (!automatic) $('#providerError').textContent = '正在获取可用模型…';
  try {
    const result = await api('/api/providers/models', { method: 'POST', body: values });
    if (!result.models?.length) throw new Error('接口没有返回可用模型，请选择"手动输入模型名称"');
    const current = $('#providerModel').value;
    setProviderModelOptions(result.models, current && current !== '__custom__' ? current : '');
    applyProviderModelCapabilities();
    $('#providerError').textContent = '模型目录可访问；请继续点击“测试连接”验证实际推理。';
    toast(`已找到 ${result.models.length} 个模型`);
  } catch (error) {
    $('#providerError').textContent = `模型检查失败：${error.message}`;
    if (!$('#providerModel').value) setProviderModelOptions([], '');
  } finally {
    button.disabled = false;
    button.textContent = '检查模型';
  }
}

let providerModelCheckTimer;
function scheduleProviderModelCheck() {
  clearTimeout(providerModelCheckTimer);
  providerModelCheckTimer = setTimeout(() => loadProviderModels({ automatic: true }), 350);
}

async function saveProvider(event) {
  event.preventDefault();
  try {
    const values = providerFormValue();
    if (values.model && (!values.context_window || !values.max_output_tokens)) {
      try {
        const result = await api('/api/providers/models', { method: 'POST', body: values });
        const matched = (result.models || []).find((item) => String(item.id || '') === values.model);
        if (matched?.context_window && !values.context_window) {
          values.context_window = Number(matched.context_window);
          $('#providerContextWindow').value = values.context_window;
        }
        if (matched?.max_output_tokens && !values.max_output_tokens) {
          values.max_output_tokens = Number(matched.max_output_tokens);
          $('#providerMaxOutputTokens').value = values.max_output_tokens;
        }
      } catch (_) {
        // Capability metadata is optional; the provider may supply defaults.
      }
    }
    const saved = await api('/api/providers', { method: 'POST', body: values });
    ['providers', 'model_profiles'].forEach((key) => {
      const list = state.bootstrap[key] || (state.bootstrap[key] = []);
      const index = list.findIndex((item) => item.id === saved.id);
      if (index >= 0) list[index] = saved;
      else list.push(saved);
    });
    $('#providerId').value = saved.id;
    renderProviders();
    populateModels();
    const visionSelect = $('#visionProvider');
    if (visionSelect) delete visionSelect.dataset.populated;
    populateVisionSettings();
    toast('API 供应商已保存');
  } catch (error) {
    $('#providerError').textContent = error.message;
  }
}

function addProvider() {
  if (state.providerEditing) return;
  showProviderForm({}, { editing: true, isNew: true });
  $('#providerName').focus();
}

function editProvider() {
  if (!$('#providerId').value) return;
  setProviderEditMode(true, false);
  $('#providerName').focus();
}

function cancelProviderEdit() {
  clearTimeout(providerModelCheckTimer);
  renderProviders();
}

async function testProvider() {
  $('#providerError').textContent = '正在测试连接…';
  try {
    const result = await api('/api/providers/test', { method: 'POST', body: providerFormValue() });
    $('#providerError').textContent = `推理连接成功：${result.response}`;
  } catch (error) {
    $('#providerError').textContent = `模型目录可能可访问，但推理服务不可用：${error.message}`;
  }
}

async function toggleProviderKey() {
  const input = $('#providerApiKey');
  const button = $('#toggleProviderKey');
  if (input.type === 'text') {
    input.type = 'password';
    button.textContent = '显示';
    button.title = '显示 API Key';
    return;
  }
  try {
    if (!input.value && $('#providerId').value) {
      const result = await api(`/api/providers/${$('#providerId').value}/secret`);
      input.value = result.api_key || '';
    }
    input.type = 'text';
    button.textContent = '隐藏';
    button.title = '隐藏 API Key';
  } catch (error) {
    $('#providerError').textContent = error.message;
  }
}

function populateRuntimeSettings() {
  const settings = state.bootstrap.settings;
  $('#commandTimeout').value = settings.command_timeout;
  $('#workspaceDir').value = settings.workspace_dir === 'workspace' ? '' : (settings.workspace_dir || '');
  $('#resolvedWorkspaceDir').textContent = state.bootstrap.resolved_workspace_dir || '-';
  renderWorkspaceControl();
}

function renderWorkspaceControl() {
  const resolved = String(state.bootstrap?.resolved_workspace_dir || '').trim();
  const raw = String(state.bootstrap?.settings?.workspace_dir || 'workspace').trim() || 'workspace';
  const label = raw === 'workspace' ? 'workspace' : (raw.split(/[\\/]/).filter(Boolean).pop() || raw);
  const button = $('#workspaceLabel');
  if (button) button.textContent = label;
  const detail = $('#workspaceDialogResolved');
  if (detail) detail.textContent = resolved || '保存后显示解析路径';
  const input = $('#workspaceDialogInput');
  if (input && document.activeElement !== input) input.value = raw === 'workspace' ? '' : raw;
}

function populateVisionSettings() {
  const settings = state.bootstrap.settings || {};
  const vision = settings.vision || {};
  const select = $('#visionProvider');
  if (select) {
    const providers = state.bootstrap.model_profiles || state.bootstrap.providers || [];
    const previous = select.value;
    select.replaceChildren(new Option('OVH 免费视觉链（默认）', ''));
    for (const kind of ['online', 'local']) {
      const list = providers.filter((p) => (p.kind || 'online') === kind);
      if (!list.length) continue;
      const group = document.createElement('optgroup');
      group.label = kind === 'local' ? '本地 API / 模型' : '在线 API';
      for (const provider of list) {
        const option = new Option(`${provider.name || provider.id} · ${provider.model || ''}`, provider.model_key || provider.id);
        group.append(option);
      }
      select.append(group);
    }
    const target = vision.provider_model_key || previous || '';
    if ([...select.options].some((option) => option.value === target)) select.value = target;
    const deleteButton = $('#deleteVisionProvider');
    if (deleteButton) deleteButton.disabled = !select.value;
  }
  const auto = $('#visionAutoRoute'); if (auto) auto.checked = vision.auto_route !== false;
  const timeout = $('#visionTimeout'); if (timeout) timeout.value = vision.timeout_ms || 120000;
  const maxImages = $('#visionMaxImages'); if (maxImages) maxImages.value = vision.max_images || 4;
}

function populateSearchSettings() {
  const settings = state.bootstrap.settings || {};
  const search = settings.search || {};
  const profiles = searchProfiles(search);
  const select = $('#searchProfileSelect');
  if (!select) return;
  select.replaceChildren(...profiles.map((profile) => new Option(profile.name || profile.endpoint || '未命名搜索 API', profile.id)));
  if (!profiles.length) select.append(new Option('尚未添加搜索 API', ''));
  const target = profiles.some((profile) => profile.id === search.provider_id)
    ? search.provider_id
    : (profiles[0]?.id || '');
  select.value = target;
  renderSearchProfileFields(profiles.find((profile) => profile.id === target) || {});
  $('#deleteSearchProfile').disabled = !target;
}

function searchProfiles(search = state.bootstrap?.settings?.search || {}) {
  if (Array.isArray(search.profiles) && search.profiles.length) {
    return search.profiles.map((profile) => ({ ...profile }));
  }
  if (search.endpoint) {
    return [{
      id: 'legacy-search',
      name: '搜索 API',
      endpoint: search.endpoint,
      api_key: search.api_key || '',
      max_results: search.max_results || 5,
    }];
  }
  return [];
}

function renderSearchProfileFields(profile) {
  $('#searchProfileName').value = profile.name || '';
  $('#searchEndpoint').value = profile.endpoint || '';
  $('#searchApiKey').value = profile.api_key || '';
  $('#searchMaxResults').value = profile.max_results || 5;
}

function searchProfileFormValue(id = '') {
  return {
    id: id || `search_${Date.now().toString(36)}`,
    name: $('#searchProfileName')?.value.trim() || '搜索 API',
    endpoint: $('#searchEndpoint')?.value.trim() || '',
    api_key: $('#searchApiKey')?.value.trim() || '',
    max_results: Number($('#searchMaxResults')?.value || 5),
  };
}

async function saveVisionSettings(options = {}) {
  const payload = {
    vision: {
      provider_model_key: $('#visionProvider')?.value || '',
      auto_route: $('#visionAutoRoute')?.checked !== false,
      timeout_ms: Number($('#visionTimeout')?.value || 120000),
      max_images: Number($('#visionMaxImages')?.value || 4),
    },
  };
  try {
    const result = await api('/api/settings', { method: 'POST', body: payload });
    Object.assign(state.bootstrap.settings, result.settings);
    const deleteButton = $('#deleteVisionProvider');
    if (deleteButton) deleteButton.disabled = !payload.vision.provider_model_key;
    if (!options.quiet) toast('视觉设置已保存');
  } catch (error) {
    toast(`视觉设置保存失败：${error.message}`);
  }
}

function selectedVisionProvider() {
  const key = $('#visionProvider')?.value || '';
  return (state.bootstrap.model_profiles || state.bootstrap.providers || [])
    .find((provider) => (provider.model_key || provider.id) === key) || null;
}

function openVisionProviderForm() {
  switchSettingsTab('models');
  addProvider();
}

async function deleteVisionProvider() {
  const provider = selectedVisionProvider();
  if (!provider) return;
  if (!confirm(`删除 API 供应商“${provider.name || provider.id}”？这会同时移除模型配置。`)) return;
  try {
    await api(`/api/providers/${encodeURIComponent(provider.id)}`, { method: 'DELETE' });
    const data = await api('/api/bootstrap');
    state.bootstrap = { ...state.bootstrap, ...data };
    populateModels();
    renderProviders();
    populateVisionSettings();
    toast('API 供应商已删除');
  } catch (error) {
    toast(`删除 API 失败：${error.message}`);
  }
}

async function persistSearchProfiles(profiles, providerId, quiet = false) {
  const payload = { search: { provider_id: providerId || '', profiles } };
  const result = await api('/api/settings', { method: 'POST', body: payload });
  Object.assign(state.bootstrap.settings, result.settings);
  populateSearchSettings();
  if (!quiet) toast('搜索 API 已保存');
}

async function saveSearchSettings(options = {}) {
  const search = state.bootstrap.settings.search || {};
  const profiles = searchProfiles(search);
  let id = $('#searchProfileSelect')?.value || '';
  const profile = searchProfileFormValue(id);
  id = profile.id;
  const index = profiles.findIndex((item) => item.id === id);
  if (index >= 0) profiles[index] = profile;
  else profiles.push(profile);
  await persistSearchProfiles(profiles, id, options.quiet === true);
}

function addSearchProfile() {
  const search = state.bootstrap.settings.search || {};
  const profiles = searchProfiles(search);
  const profile = { id: `search_${Date.now().toString(36)}`, name: '新搜索 API', endpoint: '', api_key: '', max_results: 5 };
  profiles.push(profile);
  state.bootstrap.settings.search = { provider_id: profile.id, profiles };
  populateSearchSettings();
  $('#searchProfileName').select();
}

async function deleteSearchProfile() {
  const id = $('#searchProfileSelect')?.value || '';
  if (!id) return;
  const search = state.bootstrap.settings.search || {};
  const profiles = searchProfiles(search);
  const current = profiles.find((profile) => profile.id === id);
  if (!confirm(`删除搜索 API“${current?.name || ''}”？`)) return;
  const remaining = profiles.filter((profile) => profile.id !== id);
  await persistSearchProfiles(remaining, remaining[0]?.id || '');
}

async function testVisionConnection() {
  const el = $('#visionTestResult');
  if (el) el.textContent = '测试中…';
  try {
    const result = await api('/api/vision/test', {
      method: 'POST',
      body: { provider_model_key: $('#visionProvider')?.value || '' },
    });
    if (el) el.textContent = result.ok
      ? `可用（延迟 ${result.latency_ms ?? '?'}ms，${result.backend || result.model || ''}）`
      : `不可用：${result.reason || ''}`;
  } catch (error) {
    if (el) el.textContent = `测试失败：${error.message}`;
  }
}

async function testSearchConnection() {
  const el = $('#searchTestResult');
  if (el) el.textContent = '测试中…';
  try {
    const result = await api('/api/search/test', {
      method: 'POST',
      body: searchProfileFormValue($('#searchProfileSelect')?.value || ''),
    });
    if (el) el.textContent = result.ok ? `可用（provider=${result.provider || ''}）` : `不可用：${result.reason || ''}`;
  } catch (error) {
    if (el) el.textContent = `测试失败：${error.message}`;
  }
}

function populateAgentSettings() {
  const settings = state.bootstrap.settings;
  const enabled = new Set(settings.agent_tools || []);
  $$('[data-agent-tool]').forEach((input) => {
    input.checked = input.value.split(',').every((tool) => enabled.has(tool));
  });
}

async function saveAgentSettings() {
  const payload = {
    agent_tools: $$('[data-agent-tool]:checked').flatMap((input) => input.value.split(',')),
  };
  const result = await api('/api/settings', { method: 'POST', body: payload });
  Object.assign(state.bootstrap.settings, result.settings);
  toast('工具范围已保存');
}

// ---- Agent 管理 ----

async function refreshAgentsFromServer() {
  const data = await api('/api/agents');
  state.bootstrap.agents = data.agents || [];
  state.bootstrap.default_agent_id = data.default_agent_id || 'general';
}

function renderAgentManager() {
  const list = $('#agentList');
  if (!list) return;
  const agents = state.bootstrap?.agents || [];
  const defaultId = String(state.bootstrap?.default_agent_id || '');
  list.innerHTML = agents.map((agent) => `
    <div class="agent-item ${agent.id === defaultId ? 'default' : ''}">
      <div class="agent-item-info">
        <b>${escapeHtml(agent.name)}${agent.id === defaultId ? '<em>默认</em>' : ''}</b>
        <small>${agent.skill_ids?.length ? `${agent.skill_ids.length} 个固定 Skill` : '无固定 Skill'}</small>
        ${agent.system_prompt ? `<p>${escapeHtml(agent.system_prompt)}</p>` : ''}
      </div>
      <div class="agent-item-actions">
        <button class="control-button" data-agent-edit="${escapeHtml(agent.id)}" type="button">编辑</button>
        <button class="danger-button" data-agent-delete="${escapeHtml(agent.id)}" type="button">删除</button>
      </div>
    </div>`).join('') || '<p class="activity">尚未添加 Agent，点击下方按钮新增。</p>';
}

function renderAgentSkillPicker() {
  const list = $('#agentSkillList');
  if (!list) return;
  const skills = state.bootstrap?.skills || [];
  list.innerHTML = skills.map((skill) => `
    <label class="skill-item">
      <input type="checkbox" value="${skill.id}" ${state.agentFormSkillIds.includes(skill.id) ? 'checked' : ''}>
      <span><b>${escapeHtml(skill.name)}</b><p>${escapeHtml(skill.description)}</p></span>
    </label>`).join('') || '<p class="activity">暂无可用 Skill</p>';
}

function showAgentForm(agent = null) {
  $('#agentFormId').value = agent?.id || '';
  $('#agentId').value = agent?.id || '';
  $('#agentId').disabled = Boolean(agent);
  $('#agentName').value = agent?.name || '';
  $('#agentSystemPromptEdit').value = agent?.system_prompt || '';
  state.agentFormSkillIds = agent?.skill_ids ? [...agent.skill_ids] : [];
  renderAgentSkillPicker();
  $('#agentError').textContent = '';
  $('#addAgent').hidden = true;
  $('#agentForm').hidden = false;
  $('#agentName').focus();
}

function hideAgentForm() {
  $('#agentForm').hidden = true;
  $('#addAgent').hidden = false;
  $('#agentError').textContent = '';
}

async function saveAgentForm() {
  const payload = {
    id: $('#agentId').value.trim(),
    name: $('#agentName').value.trim(),
    system_prompt: $('#agentSystemPromptEdit').value,
    skill_ids: state.agentFormSkillIds,
  };
  try {
    await api('/api/agents', { method: 'POST', body: payload });
    await refreshAgentsFromServer();
    hideAgentForm();
    renderAgents();
    renderAgentManager();
    applyConversationAgent(state.conversations.find((item) => item.id === state.conversationId));
    toast('Agent 已保存');
  } catch (error) {
    $('#agentError').textContent = error.message;
  }
}

async function deleteAgent(agentId) {
  const agent = (state.bootstrap?.agents || []).find((item) => item.id === agentId);
  if (!confirm(`删除 Agent「${agent?.name || agentId}」？引用它的对话将回退到默认 Agent。`)) return;
  try {
    await api(`/api/agents/${encodeURIComponent(agentId)}`, { method: 'DELETE' });
    await refreshAgentsFromServer();
    renderAgents();
    renderAgentManager();
    applyConversationAgent(state.conversations.find((item) => item.id === state.conversationId));
    toast('Agent 已删除');
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

async function saveRuntimeSettings() {
  const payload = {
    command_timeout: Number($('#commandTimeout').value),
    workspace_dir: $('#workspaceDir').value.trim(),
  };
  const result = await api('/api/settings', { method: 'POST', body: payload });
  Object.assign(state.bootstrap.settings, result.settings);
  state.bootstrap.resolved_workspace_dir = result.resolved_workspace_dir || state.bootstrap.resolved_workspace_dir;
  $('#resolvedWorkspaceDir').textContent = state.bootstrap.resolved_workspace_dir || '-';
  renderWorkspaceControl();
  toast('运行参数已保存');
}

async function saveWorkspaceSettings() {
  const value = $('#workspaceDialogInput')?.value.trim() || '';
  try {
    const result = await api('/api/settings', { method: 'POST', body: { workspace_dir: value } });
    Object.assign(state.bootstrap.settings, result.settings);
    state.bootstrap.resolved_workspace_dir = result.resolved_workspace_dir || state.bootstrap.resolved_workspace_dir;
    $('#workspaceDir').value = value;
    renderWorkspaceControl();
    $('#workspaceDialog').close();
    toast('工作区已保存');
  } catch (error) {
    toast(`工作区保存失败：${error.message}`);
  }
}

async function saveAccessToken() {
  const value = $('#accessTokenInput').value.trim();
  if (!value) {
    toast('请输入新口令');
    return;
  }
  if (value.length < 4) {
    toast('口令至少 4 位');
    return;
  }
  const result = await api('/api/settings', { method: 'POST', body: { access_token: value } });
  Object.assign(state.bootstrap.settings, result.settings);
  // 更新本会话使用的口令，避免保存后立即失效
  state.token = value;
  localStorage.setItem('naibaChatToken', value);
  $('#accessTokenInput').value = '';
  $('#accessTokenInput').placeholder = '口令已更新（输入可再次修改）';
  toast('口令已更新，其他设备需用新口令登录');
}

function mcpServerState(server) {
  if (server.status === 'error' || server.error) return { text: '错误', color: '#e45e55' };
  if (server.activity === 'calling' || (server.active_calls && server.active_calls > 0)) return { text: '使用中', color: '#3ecf8e' };
  if (server.status === 'connecting' || server.status === 'reconnecting') return { text: '连接中', color: '#e0a13a' };
  if (server.connected) return { text: '已就绪', color: '#7d867d' };
  return { text: '待机', color: '#7d867d' };
}

function renderMcp() {
  const servers = state.bootstrap.mcp_servers || [];
  // Top-bar status light: priority error > in-use > connection-change > idle
  const mcpButton = $('#mcpStatus');
  const dot = mcpButton.querySelector('i');
  const label = mcpButton.querySelector('span');
  const anyError = servers.some((s) => s.status === 'error' || s.error);
  const anyCalling = servers.some((s) => s.activity === 'calling' || (s.active_calls && s.active_calls > 0));
  const anyConnecting = servers.some((s) => s.status === 'connecting' || s.status === 'reconnecting');
  let topText;
  if (anyError) {
    mcpButton.classList.remove('connected', 'calling', 'connecting'); mcpButton.classList.add('error');
    if (dot) dot.style.background = '#e45e55'; topText = 'MCP · 错误';
  } else if (anyCalling) {
    mcpButton.classList.remove('error', 'connecting'); mcpButton.classList.add('calling');
    if (dot) dot.style.background = '#3ecf8e'; topText = 'MCP · 使用中';
  } else if (anyConnecting) {
    mcpButton.classList.remove('error', 'calling'); mcpButton.classList.add('connecting');
    if (dot) dot.style.background = '#e0a13a'; topText = 'MCP · 连接中';
  } else {
    mcpButton.classList.remove('error', 'calling', 'connecting');
    if (servers.length && servers.every((s) => s.connected)) {
      mcpButton.classList.add('connected');
      if (dot) dot.style.background = '#3ecf8e'; topText = 'MCP · 已就绪';
    } else {
      if (dot) dot.style.background = '#7d867d'; topText = servers.length ? 'MCP · 待机' : 'MCP';
    }
  }
  if (label) label.textContent = topText;

  $('#mcpList').innerHTML = servers.map((server) => {
    const st = mcpServerState(server);
    const detail = server.connected
      ? `${server.tools?.length ?? 0} 个工具`
      : (server.status === 'idle' ? '仅在本轮激活的 Skill 需要 MCP 时连接' : escapeHtml(server.error || st.text));
    return `<div class="connection-item">
      <span><b>${escapeHtml(server.id)} · ${st.text}</b><small>${detail}</small></span>
      <span class="status-mark" style="background:${st.color}"></span>
    </div>`;
  }).join('') || '<p class="activity">没有注册 MCP 服务</p>';
  $$('#mcpList .connection-item').forEach((item, index) => {
    const server = servers[index];
    if (!server) return;
    const actions = document.createElement('span');
    actions.className = 'mcp-actions';
    const test = document.createElement('button');
    test.className = 'control-button mcp-test';
    test.type = 'button';
    test.textContent = '测试';
    test.addEventListener('click', () => mcpAction(server.id, 'test'));
    const reconnect = document.createElement('button');
    reconnect.className = 'control-button mcp-reconnect';
    reconnect.type = 'button';
    reconnect.textContent = '重连';
    reconnect.addEventListener('click', () => mcpAction(server.id, 'reconnect'));
    actions.append(test, reconnect);
    item.append(actions);
  });
}

async function mcpAction(serverId, action) {
  try {
    const result = await api('/api/mcp/' + action, { method: 'POST', body: { server_id: serverId } });
    const server = state.bootstrap.mcp_servers.find((item) => item.id === serverId);
    if (server) Object.assign(server, result);
    renderMcp();
    toast(action === 'test' ? 'MCP 测试完成' : 'MCP 已重新连接');
  } catch (error) {
    toast('MCP 操作失败：' + error.message);
  }
}

// 轻量轮询：仅刷新状态相关字段（status/connected/active_calls/activity/last_used_at），
// 保留 bootstrap 中已有的 tools 与 error 信息，使"使用中/已就绪"状态实时反映。
async function pollMcpStatus() {
  try {
    const data = await api('/api/mcp/status/light');
    const servers = data.servers || [];
    const prev = state.bootstrap.mcp_servers || [];
    const byId = {};
    for (const s of prev) byId[s.id] = s;
    for (const s of servers) {
      const cur = byId[s.id];
      if (!cur) continue;
      cur.status = s.status;
      cur.connected = s.connected;
      cur.active_calls = s.active_calls;
      cur.activity = s.activity;
      cur.last_used_at = s.last_used_at;
    }
    renderMcp();
  } catch (_error) {
    /* 轮询失败不阻断界面 */
  }
}

function startMcpPoll() {
  if (state.mcpPollTimer) return;
  state.mcpPollTimer = window.setInterval(pollMcpStatus, 2000);
}

async function uploadFiles(files) {
  for (const file of files) {
    const chip = { name: file.name, uploading: true };
    state.pendingFiles.push(chip);
    renderPendingFiles();
    try {
      const data = await readAsDataUrl(file);
      const uploaded = await api('/api/uploads', { method: 'POST', body: { name: file.name, data } });
      Object.assign(chip, uploaded, { uploading: false });
    } catch (error) {
      state.pendingFiles = state.pendingFiles.filter((item) => item !== chip);
      toast(`上传失败：${error.message}`);
    }
    renderPendingFiles();
  }
}

function readAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function renderPendingFiles() {
  $('#pendingFiles').innerHTML = state.pendingFiles.map((file, index) => `
    <span class="file-chip">${file.uploading ? '上传中 · ' : ''}${escapeHtml(file.name)}<button data-remove-file="${index}" title="移除">×</button></span>`).join('');
}

function detachRunSubscription() {
  state.abortController?.abort();
  clearVisionProgress();
  state.abortController = null;
  state.chatRunId = '';
  state.runConversationId = '';
  state.runSequence = 0;
  setBusy(false);
}

function clearVisionProgress() {
  if (state.visionTimer) window.clearInterval(state.visionTimer);
  state.visionTimer = null;
  state.visionStartedAt = 0;
}

function renderVisionProgress(answer, event) {
  clearVisionProgress();
  const startedAt = Number(event.started_at || Date.now());
  state.visionStartedAt = startedAt;
  const backend = String(event.backend || '视觉模型');
  const update = () => {
    const elapsed = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    const label = answer.querySelector('.vision-progress-elapsed');
    if (label) label.textContent = `已等待 ${elapsed} 秒`;
    $('#runtimeStatus').textContent = `${backend} · 已等待 ${elapsed} 秒`;
  };
  answer.innerHTML = `<div class="vision-progress"><span class="vision-spinner" aria-hidden="true"></span><span class="vision-progress-label">正在调用 ${escapeHtml(backend)}</span><span class="vision-progress-elapsed">已等待 0 秒</span></div>`;
  update();
  state.visionTimer = window.setInterval(update, 1000);
}

function createRunRow(run) {
  const row = messageElement({ role: 'assistant', content: '' }, true);
  row.dataset.runId = String(run.id || '');
  row.dataset.runKind = String(run.kind || 'chat');
  $('#messages').append(row);
  scrollToBottom();
  return row;
}

async function consumeRunStream(response, row, conversationId, runId, controller) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';
    for (const line of lines) {
      if (!line.trim()) continue;
      const event = JSON.parse(line);
      if (event.type === 'heartbeat') continue;
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
    const event = JSON.parse(buffer);
    if (event.type !== 'heartbeat') handleChatEvent(event, row, conversationId, runId);
  }
}

async function finishRunSubscription(conversationId, controller) {
  if (state.abortController === controller) {
    state.abortController = null;
    state.chatRunId = '';
    state.runConversationId = '';
    state.runSequence = 0;
    setBusy(false);
  }
  await loadTasks();
  if (state.conversationId === conversationId && !state.abortController) {
    await loadConversations();
    if (state.conversationId === conversationId) await openConversation(conversationId);
  }
}

async function resumeRun(run) {
  const conversationId = String(run?.conversation_id || '');
  const runId = String(run?.id || '');
  if (!runId || conversationId !== state.conversationId) return;
  detachRunSubscription();
  const row = createRunRow(run);
  const controller = new AbortController();
  state.abortController = controller;
  state.chatRunId = runId;
  state.runConversationId = conversationId;
  const cached = state.runEvents[runId] || [];
  state.runSequence = Number(cached.at(-1)?.sequence || 0);
  setBusy(true);
  cached.forEach((event) => handleChatEvent(event, row, conversationId, runId));
  void (async () => {
    try {
      const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/events?after=${state.runSequence}`, {
        headers: { Authorization: `Bearer ${state.token}` },
        signal: controller.signal,
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.error || `HTTP ${response.status}`);
      }
      await consumeRunStream(response, row, conversationId, runId, controller);
    } catch (error) {
      if (error.name !== 'AbortError' && state.conversationId === conversationId) {
        row.querySelector('.answer-content').innerHTML = `<p>恢复 Run 失败：${escapeHtml(error.message)}</p>`;
      }
    } finally {
      if (state.abortController === controller) await finishRunSubscription(conversationId, controller);
    }
  })();
}

async function resumeConversationRun(conversationId) {
  if (!conversationId || conversationId !== state.conversationId || state.abortController) return;
  try {
    const result = await api(`/api/runs?conversation_id=${encodeURIComponent(conversationId)}&active_only=1`);
    if (conversationId !== state.conversationId || state.abortController) return;
    const run = (result.runs || [])[0];
    if (run) await resumeRun(run);
    else setBusy(false);
  } catch (error) {
    console.debug('[naiba] Run 恢复失败:', error.message);
  }
}

async function sendChatMessage(textOverride = '') {
  const input = $('#messageInput');
  const text = (textOverride || input.value).trim();
  if (!text || state.chatRunId || state.abortController || state.taskSubmitting) return;
  if (state.pendingFiles.some((file) => file.uploading)) {
    toast('请等待文件上传完成');
    return;
  }
  hideChoiceButtons();
  if (!state.conversationId) await createConversation();
  const conversationId = state.conversationId;
  const attachments = state.pendingFiles.map(({ name, path, size }) => ({ name, path, size }));
  state.pendingFiles = [];
  renderPendingFiles();
  input.value = '';
  resizeTextarea();
  if ($('#emptyState')) $('#emptyState').hidden = true;
  $('#messages').append(messageElement({ role: 'user', content: text, metadata: { attachments } }));
  const row = createRunRow({ id: '', kind: 'chat' });
  const controller = new AbortController();
  state.abortController = controller;
  state.runConversationId = conversationId;
  setBusy(true);
  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${state.token}` },
      body: JSON.stringify({
        conversation_id: conversationId,
        message: text,
        attachments,
        model_key: $('#modelSelect').value,
        auto_skills: state.autoSkills,
        skill_ids: state.selectedSkills,
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
    await consumeRunStream(response, row, conversationId, state.chatRunId, controller);
  } catch (error) {
    if (error.name !== 'AbortError' && state.conversationId === conversationId) {
      row.querySelector('.answer-content').innerHTML = `<p>请求失败：${escapeHtml(error.message)}</p>`;
    }
  } finally {
    if (state.abortController === controller) await finishRunSubscription(conversationId, controller);
  }
}

async function sendMessage(textOverride = '') {
  await sendChatMessage(textOverride);
}

function updateDeepReasoningButton() {
  const btn = $('#deepReasoningButton');
  if (!btn) return;
  btn.classList.toggle('active', state.deepReasoningEnabled);
  btn.setAttribute('aria-pressed', String(state.deepReasoningEnabled));
  btn.title = state.deepReasoningEnabled ? '深度思考：开启' : '深度思考：关闭';
}

async function toggleDeepReasoning() {
  if (!state.conversationId) await createConversation();
  if (state.chatRunId || state.abortController) return;
  const previous = state.deepReasoningEnabled;
  state.deepReasoningEnabled = !previous;
  updateDeepReasoningButton();
  try {
    const updated = await api(`/api/conversations/${state.conversationId}/settings`, {
      method: 'POST',
      body: { deep_reasoning_enabled: state.deepReasoningEnabled },
    });
    const index = state.conversations.findIndex((item) => item.id === state.conversationId);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
    toast(state.deepReasoningEnabled ? '深度思考已开启（本对话）' : '深度思考已关闭（本对话）');
  } catch (error) {
    state.deepReasoningEnabled = previous;
    updateDeepReasoningButton();
    toast(`深度思考设置保存失败：${error.message}`);
  }
}

function updateWebSearchButton() {
  const btn = $('#webSearchButton');
  if (!btn) return;
  btn.classList.toggle('active', state.webSearchEnabled);
  btn.setAttribute('aria-pressed', String(state.webSearchEnabled));
  btn.title = state.webSearchEnabled ? '联网搜索：开启' : '联网搜索：关闭';
}

async function toggleWebSearch() {
  if (!state.conversationId) await createConversation();
  const previous = state.webSearchEnabled;
  state.webSearchEnabled = !previous;
  updateWebSearchButton();
  try {
    const updated = await api(`/api/conversations/${state.conversationId}/settings`, {
      method: 'POST',
      body: { web_search_enabled: state.webSearchEnabled },
    });
    const index = state.conversations.findIndex((item) => item.id === state.conversationId);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
    toast(state.webSearchEnabled ? '联网搜索已开启（本对话）' : '联网搜索已关闭（本对话）');
  } catch (error) {
    state.webSearchEnabled = previous;
    updateWebSearchButton();
    toast(`联网搜索设置保存失败：${error.message}`);
  }
}

async function handlePasteImage(event) {
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

function handleChatEvent(event, row, conversationId = state.conversationId, runId = state.chatRunId) {
  if (conversationId !== state.conversationId) return;
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
  if (event.type === 'run_started') {
    state.chatRunId = String(event.run_id || '');
    state.runConversationId = conversationId;
    row.dataset.runId = state.chatRunId;
  } else if (event.type === 'vision_start') {
    renderVisionProgress(activity || answer, event);
  } else if (event.type === 'vision_done') {
    clearVisionProgress();
    setActivity(event.message || '视觉识别完成，正在交给主模型处理');
    $('#runtimeStatus').textContent = event.message || '视觉识别完成，正在交给主模型处理';
  } else if (event.type === 'vision_error') {
    clearVisionProgress();
    setActivity(event.message || '视觉识别失败，已降级处理');
    $('#runtimeStatus').textContent = '视觉识别失败，已降级处理';
  } else if (event.type === 'status') {
    clearVisionProgress();
    const statusMessage = String(event.message || '').startsWith('已自动识图')
      ? `视觉识别完成，正在交给主模型处理（${event.message}）`
      : event.message;
    setActivity(statusMessage);
    $('#runtimeStatus').textContent = statusMessage;
  } else if (event.type === 'skills') {
    setActivity(`已启用 ${event.skills.map((skill) => skill.name).join('、')}`);
  } else if (event.type === 'tools_available') {
    const existing = row.querySelector('.tool-availability');
    if (existing) existing.remove();
    const wrapper = document.createElement('div');
    wrapper.innerHTML = toolAvailabilityMarkup(event.tools || []);
    if (wrapper.firstElementChild) answer.before(wrapper.firstElementChild);
  } else if (event.type === 'delta') {
    setActivity('');
    const current = answer.dataset.raw || '';
    const next = current + String(event.content || '');
    answer.dataset.raw = next;
    answer.innerHTML = markdown(next);
  } else if (event.type === 'reasoning_start') {
    row.querySelectorAll('.reasoning-block[data-active="true"]').forEach((block) => { block.dataset.active = 'false'; });
    const block = document.createElement('details');
    block.className = 'reasoning-block';
    block.open = true;
    block.dataset.streaming = 'true';
    block.dataset.active = 'true';
    block.innerHTML = '<summary>Thinking</summary><div class="reasoning-content"></div>';
    answer.before(block);
  } else if (event.type === 'reasoning_delta') {
    let block = row.querySelector('.reasoning-block[data-active="true"]');
    if (!block) {
      block = document.createElement('details');
      block.className = 'reasoning-block';
      block.open = true;
      block.dataset.streaming = 'true';
      block.dataset.active = 'true';
      block.innerHTML = '<summary>Thinking</summary><div class="reasoning-content"></div>';
      answer.before(block);
    }
    const content = block.querySelector('.reasoning-content');
    content.dataset.raw = (content.dataset.raw || '') + String(event.content || '');
    content.innerHTML = markdown(content.dataset.raw);
    row.dataset.reasoningStreamed = 'true';
  } else if (event.type === 'reasoning_end') {
    row.querySelectorAll('.reasoning-block[data-active="true"]').forEach((block) => {
      block.open = false;
      block.dataset.active = 'false';
    });
  } else if (event.type === 'reasoning' && !row.dataset.reasoningStreamed) {
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
  } else if (event.type === 'tool_start') {
    const stack = row.querySelector('.tool-stack') || document.createElement('div');
    stack.className = 'tool-stack';
    const details = document.createElement('details');
    details.className = 'tool-run';
    details.open = true;
    const toolArguments = typeof event.arguments === 'string'
      ? event.arguments
      : JSON.stringify(event.arguments || {}, null, 2);
    details.innerHTML = `<summary>Running · ${escapeHtml(event.tool)}${event.reason ? ` · ${escapeHtml(event.reason)}` : ''}</summary><pre>${escapeHtml(toolArguments)}</pre>`;
    stack.appendChild(details);
    if (!stack.parentNode) answer.before(stack);
  } else if (event.type === 'tool_start_legacy') {
    const stack = row.querySelector('.tool-stack') || document.createElement('div');
    stack.className = 'tool-stack';
    stack.insertAdjacentHTML('beforeend', `<div class="tool-run">正在执行 · ${escapeHtml(event.tool)}${event.reason ? ` · ${escapeHtml(event.reason)}` : ''}</div>`);
    if (!stack.parentNode) answer.before(stack);
  } else if (event.type === 'tool_result') {
    const last = row.querySelector('.tool-run:last-child');
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
  } else if (event.type === 'tool_result_legacy') {
    const last = row.querySelector('.tool-run:last-child');
    if (last) last.textContent = `${event.success ? '已完成' : '失败'} · ${event.tool}`;
  } else if (event.type === 'tool_confirm') {
    // 需要确认的工具调用
    const stack = row.querySelector('.tool-stack') || document.createElement('div');
    stack.className = 'tool-stack';
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
          <button class="tool-confirm-btn tool-confirm-reject" onclick="rejectTool('${escapeHtml(confirmId)}', '${escapeHtml(event.run_id || runId)}')">拒绝</button>
          <button class="tool-confirm-btn tool-confirm-approve" onclick="approveTool('${escapeHtml(confirmId)}', '${escapeHtml(event.run_id || runId)}')">允许执行</button>
        </div>
      </div>`;
    stack.insertAdjacentHTML('beforeend', confirmMarkup);
    if (!stack.parentNode) answer.before(stack);
  } else if (event.type === 'choice') {
    // AI回复包含可选项，显示选择按钮
    showChoiceButtons(event.choices, event.choice_groups);
  } else if (event.type === 'cancelled') {
    clearVisionProgress();
    collapseReasoning();
    setActivity('');
    answer.innerHTML = `<p>${escapeHtml(event.message || '任务已取消')}</p>`;
  } else if (event.type === 'run_failed') {
    clearVisionProgress();
    collapseReasoning();
    setActivity('');
    // 工具协议解析失败：只展示可读错误，不显示原始 XML/JSON 或命令参数。
    answer.innerHTML = `<p>执行失败：${escapeHtml(event.error || '任务执行失败')}</p>`;
    $('#runtimeStatus').textContent = '执行失败';
  } else if (event.type === 'done') {
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
  } else if (event.type === 'error') {
    clearVisionProgress();
    collapseReasoning();
    answer.innerHTML = `<p>执行失败：${escapeHtml(event.message)}</p>`;
    $('#runtimeStatus').textContent = '执行失败';
  }
  scrollToBottom();
}

function showChoiceButtons(choices, choiceGroups = []) {
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

  const selected = [];
  let groupIndex = 0;

  const formatAnswer = (group, choice, index) => {
    const prompt = group.prompt || `选择 ${index + 1}`;
    return /[：:？?]$/.test(prompt) ? `${prompt}${choice}` : `${prompt}：${choice}`;
  };

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
        sendMessage(answer);
      });
      options.appendChild(btn);
    });
    container.appendChild(options);
  };

  renderGroup();
  scrollToBottom();
}

function hideChoiceButtons() {
  const existing = $('#choiceButtons');
  if (existing) existing.remove();
}

async function approveTool(confirmId, runId = state.chatRunId) {
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

async function rejectTool(confirmId, runId = state.chatRunId) {
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

function setBusy(busy) {
  const sendBtn = $('#sendButton');
  sendBtn.disabled = false;
  sendBtn.classList.toggle('is-stop', busy);
  sendBtn.textContent = busy ? '■' : '↑';
  sendBtn.title = busy ? '停止当前请求' : '发送';
  $$('#choiceButtons button').forEach((button) => { button.disabled = busy; });
  sendBtn.setAttribute('aria-label', busy ? '停止当前请求' : '发送');
  $('#messageInput').disabled = false;
  const reasoningButton = $('#deepReasoningButton');
  if (reasoningButton) reasoningButton.disabled = busy;
  updateUnloadModelButton();
  if (!busy && $('#runtimeStatus').textContent !== '执行失败') $('#runtimeStatus').textContent = '就绪';
}

async function cancelCurrentRun() {
  const runId = state.chatRunId;
  if (runId) {
    try {
      await api('/api/chat/cancel', { method: 'POST', body: { run_id: runId } });
    } catch (error) {
      console.debug('[naiba] cancel chat failed:', error.message);
    }
  }
}

function resizeTextarea() {
  const input = $('#messageInput');
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
}

function openSidebar() {
  $('#sidebar').classList.add('open');
  $('#sidebarBackdrop').classList.add('open');
}

function closeSidebar() {
  $('#sidebar').classList.remove('open');
  $('#sidebarBackdrop').classList.remove('open');
}

function bindEvents() {
  $('#authForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await authenticate($('#tokenInput').value.trim());
      $('#authDialog').close();
      await initialize();
    } catch (error) {
      $('#authError').textContent = error.message;
    }
  });
  $('#newChatButton').addEventListener('click', createConversation);
  $('#conversationList').addEventListener('click', (event) => {
    const item = event.target.closest('.conversation-item');
    if (!item) return;
    if (event.target.closest('.delete-conversation')) deleteConversation(item.dataset.conversationId);
    else if (event.target.closest('.conversation-settings')) openConversationSettings(item.dataset.conversationId);
    else openConversation(item.dataset.conversationId);
  });
  $('#modelSelect').addEventListener('change', saveModelSelection);
  $('#unloadModel').addEventListener('click', unloadCurrentModel);
  $('#agentSelect').addEventListener('change', saveAgentSelection);
  $('#openSkills').addEventListener('click', () => $('#skillsDialog').showModal());
  $('#openTasks').addEventListener('click', () => $('#tasksDialog').showModal());
  $('#activeTaskBar').addEventListener('click', (event) => {
    if (event.target.closest('[data-open-tasks]')) $('#tasksDialog').showModal();
  });
  $('#permissionModeSwitch').addEventListener('click', (event) => {
    const button = event.target.closest('[data-permission-mode]');
    if (button) switchPermissionMode(button.dataset.permissionMode);
  });
  $('#taskList').addEventListener('click', (event) => {
    const item = event.target.closest('[data-task-id]');
    if (!item) return;
    const task = state.tasks.find((value) => value.id === item.dataset.taskId);
    if (task) { $('#tasksDialog').close(); openConversation(task.conversation_id); }
  });
  $('#mcpStatus').addEventListener('click', () => {
    $('#settingsDialog').showModal();
    switchSettingsTab('connections');
  });
  $('#openSettings').addEventListener('click', () => $('#settingsDialog').showModal());
  $$('[data-close]').forEach((button) => button.addEventListener('click', () => $(`#${button.dataset.close}`).close()));
  $('#conversationSettingsForm').addEventListener('submit', saveConversationSettings);
  $('#autoSkills').addEventListener('change', (event) => {
    state.autoSkills = event.target.checked;
    localStorage.setItem('naibaChatAutoSkills', String(state.autoSkills));
    updateSkillSummary();
  });
  $('#skillSearch').addEventListener('input', (event) => renderSkills(event.target.value));
  $('#skillList').addEventListener('change', (event) => {
    if (event.target.type !== 'checkbox') return;
    state.selectedSkills = event.target.checked
      ? [...new Set([...state.selectedSkills, event.target.value])]
      : state.selectedSkills.filter((id) => id !== event.target.value);
    localStorage.setItem('naibaChatSkillIds', JSON.stringify(state.selectedSkills));
    updateSkillSummary();
  });
  $('#composerForm').addEventListener('submit', (event) => {
    event.preventDefault();
    sendMessage();
  });
  $('#sendButton').addEventListener('click', (event) => {
    if (!state.chatRunId && !state.abortController) return;
    event.preventDefault();
    if (state.chatRunId) cancelCurrentRun();
  });
  $('#messageInput').addEventListener('input', resizeTextarea);
  $('#messageInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendMessage();
    }
  });
  $('#attachButton').addEventListener('click', () => $('#fileInput').click());
  $('#fileInput').addEventListener('change', (event) => { uploadFiles([...event.target.files]); event.target.value = ''; });
  const composerWrap = document.querySelector('.composer-wrap');
  if (composerWrap) {
    composerWrap.addEventListener('dragover', (event) => {
      if (!event.dataTransfer?.types?.includes('Files')) return;
      event.preventDefault();
      composerWrap.classList.add('dragover');
    });
    composerWrap.addEventListener('dragleave', (event) => {
      if (event.relatedTarget && composerWrap.contains(event.relatedTarget)) return;
      composerWrap.classList.remove('dragover');
    });
    composerWrap.addEventListener('drop', (event) => {
      if (!event.dataTransfer?.files?.length) return;
      event.preventDefault();
      composerWrap.classList.remove('dragover');
      uploadFiles([...event.dataTransfer.files]);
    });
  }
  $('#webSearchButton').addEventListener('click', toggleWebSearch);
  $('#deepReasoningButton').addEventListener('click', toggleDeepReasoning);
  $('#contextUsageButton').addEventListener('click', toggleContextUsagePopover);
  $('#contextUsagePopover').addEventListener('click', (event) => event.stopPropagation());
  document.addEventListener('click', closeContextUsagePopover);
  $('#messageInput').addEventListener('paste', handlePasteImage);
  $('#saveVision').addEventListener('click', saveVisionSettings);
  $('#testVision').addEventListener('click', testVisionConnection);
  $('#visionProvider').addEventListener('change', () => saveVisionSettings({ quiet: true }));
  $('#addVisionProvider').addEventListener('click', openVisionProviderForm);
  $('#deleteVisionProvider').addEventListener('click', deleteVisionProvider);
  $('#saveSearch').addEventListener('click', saveSearchSettings);
  $('#testSearch').addEventListener('click', testSearchConnection);
  $('#searchProfileSelect').addEventListener('change', (event) => {
    const profile = searchProfiles().find((item) => item.id === event.target.value) || {};
    renderSearchProfileFields(profile);
    $('#deleteSearchProfile').disabled = !event.target.value;
    if (event.target.value) {
      const profiles = searchProfiles();
      persistSearchProfiles(profiles, event.target.value, true).catch((error) => toast(`搜索 API 切换失败：${error.message}`));
    }
  });
  $('#addSearchProfile').addEventListener('click', addSearchProfile);
  $('#deleteSearchProfile').addEventListener('click', deleteSearchProfile);
  $('#pendingFiles').addEventListener('click', (event) => {
    const button = event.target.closest('[data-remove-file]');
    if (!button) return;
    state.pendingFiles.splice(Number(button.dataset.removeFile), 1);
    renderPendingFiles();
  });

  $('#messages').addEventListener('click', async (event) => {
    const codeCopyButton = event.target.closest('[data-copy-code]');
    if (codeCopyButton) {
      const code = codeCopyButton.closest('.code-block')?.querySelector('code');
      if (!code) return;
      try {
        await copyText(code.textContent);
        codeCopyButton.textContent = '已复制';
        clearTimeout(codeCopyButton.copyResetTimer);
        codeCopyButton.copyResetTimer = setTimeout(() => {
          if (codeCopyButton.isConnected) codeCopyButton.textContent = '复制';
        }, 1500);
        toast('已复制代码');
      } catch (error) {
        toast(`复制失败：${error.message}`);
      }
      return;
    }
    const copyButton = event.target.closest('[data-copy-message]');
    if (copyButton) {
      const text = copyButton.closest('.message-body').querySelector('.answer-content').innerText;
      try {
        await copyText(text);
        toast('已复制回复');
      } catch (error) {
        toast(`复制失败：${error.message}`);
      }
      return;
    }
    const editButton = event.target.closest('[data-edit-message]');
    if (editButton) {
      startEditMessage(editButton.closest('.message-row'));
    }
  });
  $$('.starter-grid button').forEach((button) => button.addEventListener('click', () => sendMessage(button.dataset.prompt)));
  $('#copyAddress').addEventListener('click', async () => {
    if (!state.bootstrap) return;
    try {
      await copyText(state.bootstrap.lan_url);
      toast('手机访问地址已复制');
    } catch (error) {
      toast(`复制失败：${error.message}`);
    }
  });
  $('#openSidebar').addEventListener('click', openSidebar);
  $('#closeSidebar').addEventListener('click', closeSidebar);
  $('#sidebarBackdrop').addEventListener('click', closeSidebar);
  $('#openWorkspace').addEventListener('click', () => {
    renderWorkspaceControl();
    $('#workspaceDialog').showModal();
    $('#workspaceDialogInput').focus();
  });
  $('#saveWorkspace').addEventListener('click', saveWorkspaceSettings);
  $$('.settings-nav button').forEach((button) => button.addEventListener('click', () => switchSettingsTab(button.dataset.settingsTab)));
  $('#addProvider').addEventListener('click', addProvider);
  $('#providerSelect').addEventListener('change', (event) => {
    const provider = state.bootstrap.providers.find((item) => item.id === event.target.value);
    showProviderForm(provider || {});
  });
  $('#providerForm').addEventListener('submit', saveProvider);
  $('#editProvider').addEventListener('click', editProvider);
  $('#cancelProvider').addEventListener('click', cancelProviderEdit);
  $('#testProvider').addEventListener('click', testProvider);
  $('#loadProviderModels').addEventListener('click', () => loadProviderModels());
  $('#unloadProviderModel').addEventListener('click', unloadConfiguredProviderModel);
  $('#providerFormat').addEventListener('change', () => {
    syncProviderKindOptions();
    updateProviderFormatGuide();
    updateProviderContextField();
  });
  $('#providerKind').addEventListener('input', () => {
    syncProviderKindOptions();
    updateProviderFormatGuide();
    updateProviderContextField();
  });
  $('#providerModel').addEventListener('change', () => {
    toggleCustomModel();
    applyProviderModelCapabilities();
  });
  $('#toggleProviderKey').addEventListener('click', toggleProviderKey);
  $('#providerApiKey').addEventListener('input', (event) => {
    if (event.target.value) $('#providerKeyStatus').textContent = '待保存';
  });
  $('#deleteProvider').addEventListener('click', async () => {
    const providerId = $('#providerId').value;
    if (!providerId) return;
    const provider = state.bootstrap.providers.find((item) => item.id === providerId);
    if (!confirm(`删除供应商"${provider?.name || ''}"？`)) return;
    await api(`/api/providers/${encodeURIComponent(providerId)}`, { method: 'DELETE' });
    const data = await api('/api/bootstrap');
    state.bootstrap = { ...state.bootstrap, ...data };
    $('#providerId').value = '';
    renderProviders();
    populateModels();
    populateVisionSettings();
    toast('供应商已删除');
  });
  $('#saveAgent').addEventListener('click', saveAgentSettings);
  $('#addAgent').addEventListener('click', () => showAgentForm(null));
  $('#agentList').addEventListener('click', (event) => {
    const editButton = event.target.closest('[data-agent-edit]');
    if (editButton) {
      const agent = (state.bootstrap?.agents || []).find((item) => item.id === editButton.dataset.agentEdit);
      showAgentForm(agent || {});
      return;
    }
    const deleteButton = event.target.closest('[data-agent-delete]');
    if (deleteButton) deleteAgent(deleteButton.dataset.agentDelete);
  });
  $('#agentSkillList').addEventListener('change', (event) => {
    if (event.target.type !== 'checkbox') return;
    state.agentFormSkillIds = event.target.checked
      ? [...new Set([...state.agentFormSkillIds, event.target.value])]
      : state.agentFormSkillIds.filter((id) => id !== event.target.value);
  });
  $('#cancelAgent').addEventListener('click', hideAgentForm);
  $('#saveAgentForm').addEventListener('click', saveAgentForm);
  $('#saveRuntime').addEventListener('click', saveRuntimeSettings);
  $('#saveToken').addEventListener('click', saveAccessToken);
  $('#checkUpdate').addEventListener('click', checkUpdate);
  $('#installUpdate').addEventListener('click', installUpdate);
  $('#openSkillImport').addEventListener('click', () => {
    setSkillImportStatus('');
    $('#skillImportDialog').showModal();
  });
  $('#skillImportFolder').addEventListener('click', () => $('#skillImportFolderInput').click());
  $('#skillImportFiles').addEventListener('click', () => $('#skillImportFileInput').click());
  $('#skillImportFolderInput').addEventListener('change', (event) => { skillImportFolderFiles(event.target.files); event.target.value = ''; });
  $('#skillImportFileInput').addEventListener('change', (event) => {
    const file = event.target.files[0];
    event.target.value = '';
    if (!file) return;
    const lower = file.name.toLowerCase();
    if (lower.endsWith('.zip')) skillImportZipFile(file);
    else if (lower.endsWith('.md')) skillImportMdFile(file);
    else setSkillImportStatus('仅支持 .zip 或 .md 文件', 'error');
  });
  const dropZone = $('#skillDropZone');
  if (dropZone) {
    dropZone.addEventListener('dragover', (event) => { event.preventDefault(); dropZone.classList.add('dragover'); });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
    dropZone.addEventListener('drop', (event) => {
      event.preventDefault();
      dropZone.classList.remove('dragover');
      const dt = event.dataTransfer;
      if (!dt) return;
      const folderEntry = [...dt.items].find((it) => it.kind === 'file' && it.webkitGetAsEntry && it.webkitGetAsEntry() && it.webkitGetAsEntry().isDirectory);
      if (folderEntry && folderEntry.webkitGetAsEntry) {
        readDirectoryEntry(folderEntry.webkitGetAsEntry()).then((files) => skillImportFolderFiles(files));
        return;
      }
      const files = [...dt.files];
      const zip = files.find((f) => f.name.toLowerCase().endsWith('.zip'));
      const md = files.find((f) => f.name.toLowerCase().endsWith('.md'));
      if (files.length === 1 && (zip || md)) {
        if (zip) skillImportZipFile(zip); else skillImportMdFile(md);
      } else if (files.length) {
        skillImportFolderFiles(files);
      }
    });
  }
  $('#backupData').addEventListener('click', backupData);
  $('#saveDataDir').addEventListener('click', saveDataDir);
  $('#moveDataDir').addEventListener('click', moveDataDir);
  $('#runMigration').addEventListener('click', runMigration);
  $('#mergeData').addEventListener('click', mergeData);
}

function setSkillImportStatus(message, kind) {
  const el = $('#skillImportStatus');
  if (!el) return;
  el.textContent = message;
  el.className = 'skill-import-status' + (kind ? ' ' + kind : '');
}

function hasSkillFrontmatter(text) {
  const m = text.match(/^---\s*\n([\s\S]*?)\n---/);
  if (!m) return false;
  const block = m[1];
  return /^\s*name\s*:/im.test(block) && /^\s*description\s*:/im.test(block);
}

async function skillImportFolderFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  if (files.length > 2000) { setSkillImportStatus('文件夹内文件数量过多（超过 2000）', 'error'); return; }
  const totalSize = files.reduce((sum, f) => sum + f.size, 0);
  if (totalSize > 80 * 1024 * 1024) { setSkillImportStatus('文件夹总大小不能超过 80 MB', 'error'); return; }
  setSkillImportStatus(`正在上传 ${files.length} 个文件…`);
  try {
    const payload = [];
    for (const file of files) {
      const data = await readAsDataUrl(file);
      payload.push({ path: file._relPath || file.webkitRelativePath || file.name, data });
    }
    const result = await api('/api/skills/install_folder', { method: 'POST', body: { files: payload } });
    if (result.configured) state.skillDirs = result.configured;
    renderInstalledSkills(result.skills || []);
    setSkillImportStatus(`已安装 ${result.files} 个文件到 ${result.dir}`, 'ok');
  } catch (error) {
    setSkillImportStatus(`安装失败：${error.message}`, 'error');
  }
}

async function skillImportZipFile(file) {
  setSkillImportStatus(`正在上传 ${file.name}…`);
  try {
    const data = await readAsDataUrl(file);
    const result = await api('/api/skills/install', { method: 'POST', body: { name: file.name, data } });
    if (result.configured) state.skillDirs = result.configured;
    renderInstalledSkills(result.skills || []);
    setSkillImportStatus(`已安装到 ${result.dir}`, 'ok');
  } catch (error) {
    setSkillImportStatus(`安装失败：${error.message}`, 'error');
  }
}

async function skillImportMdFile(file) {
  setSkillImportStatus(`正在读取 ${file.name}…`);
  try {
    const text = await file.text();
    if (!hasSkillFrontmatter(text)) {
      setSkillImportStatus('该 .md 缺少 name + description 的 YAML 头，无法作为 Skill 安装。', 'error');
      return;
    }
    const data = await readAsDataUrl(file);
    const result = await api('/api/skills/install_folder', { method: 'POST', body: { files: [{ path: 'SKILL.md', data }] } });
    if (result.configured) state.skillDirs = result.configured;
    renderInstalledSkills(result.skills || []);
    setSkillImportStatus(`已作为 Skill 安装到 ${result.dir}`, 'ok');
  } catch (error) {
    setSkillImportStatus(`安装失败：${error.message}`, 'error');
  }
}

function readDirectoryEntry(entry) {
  const files = [];
  const walk = (ent, prefix) => new Promise((res) => {
    if (ent.isFile) {
      ent.file((file) => { file._relPath = prefix + file.name; files.push(file); res(); });
    } else if (ent.isDirectory) {
      const reader = ent.createReader();
      const readBatch = () => reader.readEntries((batch) => {
        if (!batch.length) { res(); return; }
        Promise.all(batch.map((b) => walk(b, prefix + ent.name + '/'))).then(readBatch);
      });
      readBatch();
    } else {
      res();
    }
  });
  return walk(entry, '').then(() => files);
}

async function loadInstalledSkills(showToast) {
  try {
    const data = await api('/api/skills/scan', { method: 'POST', body: {} });
    if (data.configured) state.skillDirs = data.configured;
    renderInstalledSkills(data.skills || []);
    if (showToast) toast('已重新扫描 Skill');
  } catch (error) {
    toast(`扫描失败：${error.message}`);
  }
}

let lastInstalledSkills = [];

function isSkillBuiltin(skill) {
  return skill.source === 'builtin';
}

function renderInstalledSkills(skills) {
  lastInstalledSkills = skills || [];
  const list = $('#installedSkillList');
  list.innerHTML = '';
  $('#installedSkillCount').textContent = String(skills.length);
  if (!skills.length) {
    list.innerHTML = '<div class="connection-item"><small>未加载任何 Skill</small></div>';
    return;
  }
  skills.forEach((skill) => {
    const item = document.createElement('div');
    item.className = 'skill-item connection-item';
    const info = document.createElement('div');
    const b = document.createElement('b');
    b.textContent = skill.name;
    const small = document.createElement('small');
    small.textContent = skill.description || skill.path;
    small.className = 'desc';
    info.append(b, small);
    item.append(info);
    if (skill.source === 'builtin' || skill.source === 'external') {
      const tag = document.createElement('span');
      tag.className = 'skill-tag';
      tag.textContent = skill.source === 'builtin' ? '内置' : '外部';
      item.append(tag);
    }
    const del = document.createElement('button');
    del.className = 'skill-delete';
    del.type = 'button';
    del.title = skill.source === 'managed' ? '删除 Skill（移动到回收目录）' : '隐藏 Skill（可恢复原文件）';
    del.textContent = '删除';
    del.addEventListener('click', () => deleteInstalledSkill(skill));
    item.append(del);
    list.append(item);
  });
}

async function deleteInstalledSkill(skill) {
  const refs = (state.bootstrap.agents || [])
    .filter((a) => (a.skill_ids || []).map(String).includes(String(skill.id)))
    .map((a) => a.name);
  const dir = skill.root || skill.path || '';
  const msg = `删除 Skill「${skill.name}」？\n目录：${dir}\n${refs.length ? `被以下 Agent 引用：${refs.join('、')}（引用将被移除）` : '未被任何 Agent 引用'}`;
  if (!confirm(msg)) return;
  try {
    const result = await api(`/api/skills/${encodeURIComponent(skill.id)}`, { method: 'DELETE' });
    state.selectedSkills = state.selectedSkills.filter((id) => id !== skill.id);
    localStorage.setItem('naibaChatSkillIds', JSON.stringify(state.selectedSkills));
    if (result.skills) state.bootstrap.skills = result.skills;
    if (result.agents) state.bootstrap.agents = result.agents;
    renderInstalledSkills(result.skills || lastInstalledSkills.filter((s) => s.id !== skill.id));
    renderSkills($('#skillSearch')?.value || '');
    toast(result.hidden ? '已删除 Skill（原文件保留并隐藏）' : `已删除，回收位置：${result.recycled_to || '未知'}`);
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

function renderDataMigration() {
  const m = state.bootstrap?.data_migration || {};
  const configured = m.configured_data_dir || state.bootstrap?.settings?.resolved_data_dir || m.data_dir || '';
  if ($('#dataDir') && document.activeElement !== $('#dataDir')) $('#dataDir').value = configured;
  $('#migrationDbVersion').textContent = m.db_version != null ? String(m.db_version) : '-';
  $('#migrationDataDir').textContent = m.restart_required
    ? `${m.data_dir || '-'}（重启后切换到 ${configured}）`
    : (m.data_dir || '-');
  $('#migrationHealthy').textContent = m.healthy === true ? '✓ 健康' : (m.healthy === false ? '✗ 异常' : '-');
  $('#migrationApplied').textContent = Array.isArray(m.applied_versions)
    ? (m.applied_versions.length ? m.applied_versions.join(', ') : '无')
    : '-';
  $('#migrationBackup').textContent = m.backup_location || '-';
}

async function saveDataDir() {
  const value = $('#dataDir')?.value.trim() || '';
  try {
    const result = await api('/api/settings', { method: 'POST', body: { data_dir: value } });
    Object.assign(state.bootstrap.settings, result.settings || {});
    const target = result.resolved_data_dir || value;
    state.bootstrap.data_migration = {
      ...(state.bootstrap.data_migration || {}),
      configured_data_dir: target,
      restart_required: Boolean(result.restart_required),
    };
    renderDataMigration();
    $('#migrationMessage').textContent = result.restart_required
      ? `数据目录已保存为 ${target}，请完全退出并重新启动 NaibaChat 后生效。`
      : '数据目录未改变。';
  } catch (error) {
    $('#migrationMessage').textContent = `保存数据目录失败：${error.message}`;
  }
}

async function moveDataDir() {
  const value = $('#dataDir')?.value.trim() || '';
  if (!value) {
    $('#migrationMessage').textContent = '请先填写目标数据目录';
    return;
  }
  if (!confirm('将当前数据库、上传文件和相关数据复制到新目录，完成后需要重启。继续？')) return;
  try {
    const result = await api('/api/migration/move-data', { method: 'POST', body: { data_dir: value } });
    if (!result.ok) {
      $('#migrationMessage').textContent = `迁移失败：${result.error || '未知错误'}`;
      return;
    }
    $('#migrationMessage').textContent = `数据已复制到 ${result.target_data_dir || value}，请完全退出并重新启动 NaibaChat。`;
    await loadDataMigrationHealth();
  } catch (error) {
    $('#migrationMessage').textContent = `迁移失败：${error.message}`;
  }
}

async function loadDataMigrationHealth() {
  try {
    const m = await api('/api/migration/health');
    state.bootstrap.data_migration = m;
    renderDataMigration();
  } catch (error) {
    toast(`读取迁移状态失败：${error.message}`);
  }
}

async function backupData() {
  try {
    const r = await api('/api/migration/backup', { method: 'POST', body: {} });
    if (r.error) { $('#migrationMessage').textContent = '备份失败：' + r.error; return; }
    const files = Array.isArray(r.files) ? `（${r.files.length} 个文件）` : '';
    $('#migrationMessage').textContent = `已备份到：${r.backup_dir || ''}${files}`;
    if (r.backup_location) { state.bootstrap.data_migration = state.bootstrap.data_migration || {}; state.bootstrap.data_migration.backup_location = r.backup_location; }
    renderDataMigration();
  } catch (error) {
    $('#migrationMessage').textContent = '备份失败：' + error.message;
  }
}

async function runMigration() {
  try {
    const r = await api('/api/migration/run', { method: 'POST', body: {} });
    if (r.ok) {
      $('#migrationMessage').textContent = `迁移完成（数据库版本 ${r.db_version ?? '-'}，${r.healthy ? '健康' : '异常'}）`;
      state.bootstrap.data_migration = r;
    } else {
      $('#migrationMessage').textContent = '迁移失败：' + (r.error || '未知错误');
    }
    renderDataMigration();
  } catch (error) {
    $('#migrationMessage').textContent = '迁移失败：' + error.message;
  }
}

async function mergeData() {
  const source = $('#mergeSource').value.trim();
  if (!source) { $('#mergeMessage').textContent = '请填写旧数据目录路径'; return; }
  try {
    const r = await api('/api/migration/merge', { method: 'POST', body: { source } });
    if (r.ok) {
      $('#mergeMessage').textContent = '合并完成：' + (r.report || (r.db_version != null ? `数据库版本 ${r.db_version}` : ''));
      if (r.data_migration) state.bootstrap.data_migration = r.data_migration;
      renderDataMigration();
    } else {
      $('#mergeMessage').textContent = '合并失败：' + (r.error || '未知错误');
    }
  } catch (error) {
    $('#mergeMessage').textContent = '合并失败：' + error.message;
  }
}

function switchSettingsTab(name) {
  $$('.settings-nav button').forEach((button) => button.classList.toggle('active', button.dataset.settingsTab === name));
  $$('[data-settings-panel]').forEach((panel) => { panel.hidden = panel.dataset.settingsPanel !== name; });
  if (name === 'agent') renderAgentManager();
  if (name === 'skills') loadInstalledSkills(false);
  if (name === 'datamigration') loadDataMigrationHealth();
  if (name === 'updates') api('/api/update').then((status) => {
    state.bootstrap.update = status;
    renderUpdateStatus(status);
  }).catch((error) => toast(`读取更新状态失败：${error.message}`));
}

bindEvents();
initialize();
