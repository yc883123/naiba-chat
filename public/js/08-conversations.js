// ============================================================
// 08-conversations.js —— 拆分自 public/app.js 第 2330-3114 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, api, escapeHtml, state, toast } from "./01-core.js";
import { renderMessages } from "./04-messages.js";
import { activeTaskStatuses, loadTasks, renderPermissionModeSwitch, taskStatusLabel } from "./06-tasks-plans.js";
import { applyConversationAgent, applyConversationModel } from "./07-models-agents.js";
import { readAsDataUrl } from "./10-upload.js";
import { detachRunSubscription, resumeConversationRun } from "./11-run-stream.js";
import { applyConversationLightweight, hideChoiceButtons, updateDeepReasoningButton } from "./12-chat-input.js";
import { prefillPresetSkillsInComposer } from "./13-skill-refs.js";
import { closeFilePanel, closeSidebar } from "./14-file-panel.js";
export async function loadConversations() {
  const result = await api('/api/conversations');
  state.conversations = result.conversations;
  renderSidebar();
  if (!state.conversationId && state.conversations.length) {
    await openConversation(state.conversations[0].id);
  } else if (!state.conversations.length) {
    renderMessages([]);
    renderPermissionModeSwitch();
  }
  renderComposerWorkspace();
}

export function formatRelativeTime(ts) {
  if (!ts) return '';
  const d = new Date(String(ts).replace(' ', 'T'));
  if (isNaN(d.getTime())) return '';
  const diff = Date.now() - d.getTime();
  const min = Math.floor(diff / 60000);
  if (min < 1) return '刚刚';
  if (min < 60) return `${min}分钟`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}小时`;
  const day = Math.floor(hr / 24);
  if (day < 7) return `${day}天`;
  const week = Math.floor(day / 7);
  if (week < 5) return `${week}周`;
  const month = Math.floor(day / 30);
  if (month < 12) return `${month}月`;
  return `${Math.floor(day / 365)}年`;
}

export function currentConversationWorkspaceGroup() {
  const c = state.conversations.find((x) => x.id === state.conversationId);
  return c ? (c.workspace_group || '').trim() : '';
}

// ---- 侧栏虚拟化（懒加载）：只渲染可视范围内的行，滚动时按窗口重绘 ----
export let sidebarRowCache = [];
export let sidebarOffsetCache = [];
export let sidebarTotalH = 0;
export let sidebarMetrics = null;
export let sidebarScrollToActive = false;
export let sidebarScrollRaf = 0;
export let sidebarShowAll = new Set(); // 已“展开全部会话”的工作区名集合（默认全部折叠到 5 条）
export const SIDE_BUFFER = 240; // 视口上下预渲染缓冲（px）
export const SIDE_CONV_LIMIT = 5; // 每个展开工作区默认显示的最新会话数

export function sidebarMetricsNow() {
  if (sidebarMetrics) return sidebarMetrics;
  const holder = document.createElement('div');
  holder.style.cssText = 'position:fixed;left:-9999px;top:0;visibility:hidden;width:260px;';
  holder.innerHTML = '<div class="workspace-group"><div class="workspace-group-header">X</div></div>'
    + '<button class="workspace-new-chat">＋</button><div class="conversation-item"><span>X</span></div>'
    + '<button class="workspace-showmore">展开其余 0 个会话</button>';
  document.body.appendChild(holder);
  sidebarMetrics = {
    header: holder.querySelector('.workspace-group-header').offsetHeight || 32,
    newchat: holder.querySelector('.workspace-new-chat').offsetHeight || 34,
    item: holder.querySelector('.conversation-item').offsetHeight || 40,
    showmore: holder.querySelector('.workspace-showmore').offsetHeight || 34,
  };
  holder.remove();
  return sidebarMetrics;
}

export function sidebarRowHeight(row) {
  const m = sidebarMetricsNow();
  if (row.type === 'header') return m.header;
  if (row.type === 'newchat') return m.newchat;
  if (row.type === 'showmore' || row.type === 'showless') return m.showmore;
  return m.item;
}

export function computeSidebarOffsets(rows) {
  const offsets = new Array(rows.length);
  let y = 0;
  for (let i = 0; i < rows.length; i++) { offsets[i] = y; y += sidebarRowHeight(rows[i]); }
  return { offsets, totalH: y };
}

export function sidebarRowAt(offsets, pos) {
  let lo = 0, hi = offsets.length - 1, ans = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (offsets[mid] <= pos) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}

export function sidebarRowHtml(row) {
  if (row.type === 'header') {
    return `<div class="workspace-group ${row.isExp ? 'expanded' : ''}" data-workspace-name="${escapeHtml(row.wsName)}" data-workspace-dir="${escapeHtml(row.dir)}">
      <div class="workspace-group-header" data-action="toggle-group">
        <span class="workspace-caret">▸</span>
        <span class="workspace-group-name">${escapeHtml(row.label)}</span>
        <span class="workspace-count">${row.count}</span>
        ${row.isUngrouped ? '' : `<button class="workspace-delete" data-action="delete-workspace" data-workspace-name="${escapeHtml(row.wsName)}" title="删除工作区" aria-label="删除工作区">×</button>`}
      </div>
    </div>`;
  }
  if (row.type === 'newchat') {
    return `<button class="workspace-new-chat" data-action="new-in-group" data-workspace-group="${escapeHtml(row.wsName)}" data-workspace-dir="${escapeHtml(row.dir)}">＋ 新会话</button>`;
  }
  if (row.type === 'showmore') {
    return `<button class="workspace-showmore" data-action="show-more" data-workspace-name="${escapeHtml(row.wsName)}">展开其余 ${row.remaining} 个会话</button>`;
  }
  if (row.type === 'showless') {
    return `<button class="workspace-showmore" data-action="show-less" data-workspace-name="${escapeHtml(row.wsName)}">收起</button>`;
  }
  const c = row.c;
  return `<div class="conversation-item ${c.id === state.conversationId ? 'active' : ''}" data-conversation-id="${c.id}">
    <button class="conversation-settings" title="对话设置" aria-label="${escapeHtml(c.title)} 的设置"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h0a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h0a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v0a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"></path></svg></button>
    <button class="conversation-open" title="${escapeHtml(c.title)}">${escapeHtml(c.title)}</button>
    <span class="conversation-time">${escapeHtml(formatRelativeTime(c.updated_at))}</span>
    <button class="delete-conversation" title="删除对话" aria-label="删除对话">删除</button>
  </div>`;
}

export function renderSidebarWindow(targetScrollTop) {
  const tree = $('#sidebarWorkspaceTree');
  if (!tree) return;
  if (!sidebarRowCache.length) {
    tree.innerHTML = '<div class="workspace-empty">暂无对话</div>';
    return;
  }
  const vh = tree.clientHeight || Math.max(240, Math.round(window.innerHeight * 0.4));
  // 关键修复：窗口必须按「夹紧后的真实滚动位置」渲染。
  // 会话列表展开/收起、点击末尾条目触发跳转时，旧的 scrollTop 可能超出新的
  // 可滚动范围（列表比视口矮或比之前短）。此前用未夹紧的值去算窗口，只渲染出
  // 末尾几行，上半部分全空白——表现为“点击末尾条目后上面的条目不显示”。
  const maxScroll = Math.max(0, sidebarTotalH - vh);
  const st = Math.max(0, Math.min(Number(targetScrollTop) || 0, maxScroll));
  let start = Math.max(0, sidebarRowAt(sidebarOffsetCache, st - SIDE_BUFFER));
  let end = sidebarRowAt(sidebarOffsetCache, st + vh + SIDE_BUFFER) + 1;
  if (end <= start) end = start + 1;
  end = Math.min(sidebarRowCache.length, end);
  const html = sidebarRowCache.slice(start, end).map(sidebarRowHtml).join('');
  tree.innerHTML = `<div class="sidebar-virtual" style="height:${sidebarTotalH}px">`
    + `<div class="sidebar-virtual-window" style="top:${sidebarOffsetCache[start]}px">${html}</div></div>`;
  // 高度突变后浏览器可能自行钳位 scrollTop；把夹紧后的值再写回一次，保证窗口与滚动一致。
  if (tree.scrollTop !== st) tree.scrollTop = st;
}

export function sidebarClampWidth(w) {
  return Math.max(170, Math.min(Math.max(170, window.innerWidth * 0.3), w));
}

export function restoreSidebarWidth() {
  const saved = parseFloat(localStorage.getItem('naibaChatSidebarW') || '');
  const base = (saved && !isNaN(saved)) ? saved : 272;
  document.documentElement.style.setProperty('--sidebar-w', sidebarClampWidth(base) + 'px');
}

export function renderSidebar() {
  const tree = $('#sidebarWorkspaceTree');
  if (!tree) return;
  const search = (state.workspaceSearch || '').trim().toLowerCase();
  const activeWs = currentConversationWorkspaceGroup();
  if (!state.expandedGroups.has('__init')) {
    // 启动时只展开“当前会话所处的工作区”，其余工作区折叠；当前会话尚未确定时暂不展开任何组。
    state.expandedGroups = new Set(['__init']);
    if (state.conversations.some((c) => c.id === state.conversationId)) {
      state.expandedGroups.add(activeWs);
    }
  }
  const groups = new Map();
  for (const c of state.conversations) {
    const key = (c.workspace_group || '').trim();
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(c);
  }
  const registered = state.workspaces || [];
  const orderedNames = [];
  const seen = new Set();
  for (const ws of registered) {
    if (!seen.has(ws.name)) { seen.add(ws.name); orderedNames.push(ws.name); }
  }
  for (const key of groups.keys()) {
    if (key && !seen.has(key)) { seen.add(key); orderedNames.push(key); }
  }
  orderedNames.push('');
  const sortConv = (list) => {
    const arr = [...list];
    if (state.workspaceSort === 'name') arr.sort((a, b) => String(a.title).localeCompare(String(b.title), 'zh'));
    else arr.sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
    return arr;
  };

  const rows = [];
  for (const wsName of orderedNames) {
    const isUngrouped = wsName === '';
    const label = isUngrouped ? '未分组' : wsName;
    const dir = (registered.find((w) => w.name === wsName) || {}).dir || '';
    let list = sortConv(groups.get(wsName) || []);
    if (search) {
      const filtered = list.filter((c) => String(c.title || '').toLowerCase().includes(search) || label.toLowerCase().includes(search));
      if (!filtered.length) continue;
      list = filtered;
    }
    const isExp = state.expandedGroups.has(wsName);
    rows.push({ type: 'header', wsName, label, dir, isUngrouped, isExp, count: list.length });
    if (isExp) {
      rows.push({ type: 'newchat', wsName, dir });
      const showAll = sidebarShowAll.has(wsName);
      const limit = SIDE_CONV_LIMIT;
      const shown = showAll || list.length <= limit ? list : list.slice(0, limit);
      for (const c of shown) rows.push({ type: 'item', c, wsName });
      if (list.length > limit && !showAll) {
        rows.push({ type: 'showmore', wsName, remaining: list.length - limit });
      } else if (list.length > limit && showAll) {
        rows.push({ type: 'showless', wsName });
      }
    }
  }

  const { offsets, totalH } = computeSidebarOffsets(rows);
  sidebarRowCache = rows; sidebarOffsetCache = offsets; sidebarTotalH = totalH;
  let st = tree.scrollTop;
  if (sidebarScrollToActive) {
    sidebarScrollToActive = false;
    const idx = rows.findIndex((r) => r.type === 'item' && r.c.id === state.conversationId);
    if (idx >= 0) st = offsets[idx];
  }
  // renderSidebarWindow 内部会按「夹紧后的真实滚动位置」切窗口并回写 scrollTop，
  // 不再在窗口算完后单独赋值——避免高度突变时窗口与滚动状态错位。
  renderSidebarWindow(st);
}

export function renderComposerWorkspace() {
  const select = $('#composerWorkspaceSelect');
  if (!select) return;
  const current = state.conversations.find((c) => c.id === state.conversationId);
  const currentGroup = current ? (current.workspace_group || '').trim() : '';
  const options = ['', ...(state.workspaces || []).map((w) => w.name)];
  select.innerHTML = options.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name || '未分组')}</option>`).join('');
  select.value = currentGroup;
}

export async function onComposerWorkspaceChange(event) {
  const id = state.conversationId;
  if (!id) return;
  const group = event.target.value || '';
  try {
    const updated = await api(`/api/conversations/${id}/settings`, { method: 'POST', body: { workspace_group: group } });
    const index = state.conversations.findIndex((c) => c.id === id);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
    if (updated.workspace_dir) state.workspaceDir = updated.workspace_dir;
    renderSidebar();
    renderComposerWorkspace();
    toast(group ? `已切换到工作区「${group}」` : '已移至未分组');
  } catch (error) {
    toast(`切换工作区失败：${error.message}`);
    renderComposerWorkspace();
  }
}

export async function onSidebarTreeClick(event) {
  const actionEl = event.target.closest('[data-action]');
  if (actionEl) {
    const action = actionEl.dataset.action;
    const groupEl = actionEl.closest('.workspace-group');
    if (action === 'toggle-group') {
      const name = groupEl?.dataset.workspaceName || '';
      if (state.expandedGroups.has(name)) state.expandedGroups.delete(name);
      else state.expandedGroups.add(name);
      renderSidebar();
    } else if (action === 'show-more') {
      const name = actionEl.dataset.workspaceName || '';
      sidebarShowAll.add(name);
      renderSidebar();
    } else if (action === 'show-less') {
      const name = actionEl.dataset.workspaceName || '';
      sidebarShowAll.delete(name);
      renderSidebar();
    } else if (action === 'new-in-group') {
      createConversation(actionEl.dataset.workspaceGroup || '', actionEl.dataset.workspaceDir || '', true);
    } else if (action === 'delete-workspace') {
      deleteWorkspace(actionEl.dataset.workspaceName || '');
    }
    return;
  }
  const item = event.target.closest('.conversation-item');
  if (!item) return;
  if (event.target.closest('.conversation-settings')) openConversationSettings(item.dataset.conversationId);
  else if (event.target.closest('.delete-conversation')) deleteConversation(item.dataset.conversationId);
  else if (event.target.closest('.conversation-open')) openConversation(item.dataset.conversationId);
}

export async function pick_workspace_directory(initial = '') {
  try {
    return await api('/api/workspace/pick', { method: 'POST', body: { initial } });
  } catch (error) {
    toast(`目录选择失败：${error.message}`);
    return { cancelled: true };
  }
}

export async function createWorkspace() {
  const result = await pick_workspace_directory();
  if (!result || result.cancelled || !result.path) return;
  const dir = result.resolved || result.path;
  const suggestedName = String(dir.split(/[\\/]/).filter(Boolean).pop() || '新工作区');
  const name = (window.prompt('工作区名称：', suggestedName) || '').trim();
  if (!name) return;
  try {
    const data = await api('/api/workspaces', { method: 'POST', body: { name, dir } });
    state.workspaces = data.workspaces || [];
    state.expandedGroups.add(name);
    toast(`已创建工作区「${name}」`);
    // 新建工作区后立即在该工作区内创建一个新对话并打开，选择框同步显示该工作区。
    try {
      await createConversation(name, dir, true);
    } catch (error) {
      toast(`工作区已创建，但新建对话失败：${error.message}`);
    }
  } catch (error) {
    toast(`创建工作区失败：${error.message}`);
  }
}

export async function deleteWorkspace(name) {
  if (!name) return;
  const count = state.conversations.filter((c) => (c.workspace_group || '').trim() === name).length;
  const hint = count ? `其下 ${count} 个对话将归档到「未分组」。` : '';
  if (!confirm(`确定删除工作区「${name}」？${hint}`)) return;
  try {
    const data = await api('/api/workspaces/delete', { method: 'POST', body: { name } });
    state.workspaces = data.workspaces || [];
    state.conversations.forEach((c) => {
      if ((c.workspace_group || '').trim() === name) c.workspace_group = '';
    });
    state.expandedGroups.delete(name);
    renderSidebar();
    renderComposerWorkspace();
    toast(`已删除工作区「${name}」`);
  } catch (error) {
    toast(`删除工作区失败：${error.message}`);
  }
}

export async function createConversation(workspaceGroup = '', workspaceDir = '', prefillSkills = false) {
  detachRunSubscription();
  hideChoiceButtons();
  const knownAgentIds = new Set((state.bootstrap?.agents || []).map((a) => String(a.id)));
  const prevAgentId = state.conversations.find((c) => String(c.id) === state.conversationId)?.agent_id;
  const defaultAgentId = String(state.bootstrap?.default_agent_id || '');
  // 新建会话默认沿用上一个会话使用的 Agent；若其已被删除则回退默认/首个，避免把失效 id 发给后端。
  const nextAgentId = String(
    (prevAgentId && knownAgentIds.has(String(prevAgentId)) && String(prevAgentId))
    || (knownAgentIds.has(defaultAgentId) && defaultAgentId)
    || (state.bootstrap?.agents?.[0]?.id) || ''
  );
  const conversation = await api('/api/conversations', {
    method: 'POST',
    body: {
      interaction_mode: 'craft',
      permission_mode: 'auto',
      web_search_enabled: false,
      deep_reasoning_enabled: false,
      agent_id: nextAgentId,
      // 新建对话继承当前全局工作区目录；若在某工作区内新建则覆盖为该工作区目录并绑定分组。
      workspace_dir: workspaceDir || state.bootstrap?.settings?.workspace_dir || '',
      workspace_group: workspaceGroup || '',
    },
  });
  state.conversationId = conversation.id;
  if (conversation.workspace_dir) {
    state.workspaceDir = conversation.workspace_dir;
  }
  state.conversations.unshift(conversation);
  state.expandedGroups.add(currentConversationWorkspaceGroup());
  renderComposerWorkspace();
  sidebarScrollToActive = true;
  renderSidebar();
  applyConversationModel(conversation);
  applyConversationAgent(conversation);
  state.webSearchEnabled = Boolean(Number(conversation.web_search_enabled || 0));
  state.deepReasoningEnabled = Boolean(Number(conversation.deep_reasoning_enabled || 0));
  state.reasoningEffort = conversation.reasoning_effort || (state.deepReasoningEnabled ? 'medium' : 'auto');
  updateDeepReasoningButton();
  applyConversationLightweight(conversation);
  renderMessages([]);
  renderPermissionModeSwitch();
  closeSidebar();
  if (prefillSkills) prefillPresetSkillsInComposer(conversation);
  renderConversationRuleBar();
  $('#messageInput').focus();
}

export async function openConversation(id) {
  if (id !== state.conversationId) {
    detachRunSubscription();
    hideChoiceButtons();
    // 文件面板属于当前会话；切换会话时收起并清空打开的标签
    closeFilePanel(true);
  }
  const conversation = await api(`/api/conversations/${id}`);
  state.conversationId = id;
  if (conversation.workspace_dir) {
    state.workspaceDir = conversation.workspace_dir;
  }
  state.expandedGroups.add(currentConversationWorkspaceGroup());
  renderComposerWorkspace();
  const index = state.conversations.findIndex((item) => item.id === id);
  if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...conversation };
  state.conversationSnapshot = conversationSnapshot(conversation);
  console.log('[naiba] openConversation', id.slice(0, 8), '服务器返回消息数=', (conversation.messages || []).length);
  // 若打开的会话处于“最新 5 条”预览之外，自动展开该工作区的全部会话以便其在侧栏可见。
  const visGroup = currentConversationWorkspaceGroup();
  if (visGroup) {
    const wsConvs = state.conversations.filter((c) => (c.workspace_group || '').trim() === visGroup);
    const recent = [...wsConvs].sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
    if (recent.findIndex((c) => c.id === id) >= SIDE_CONV_LIMIT) sidebarShowAll.add(visGroup);
  }
  sidebarScrollToActive = true;
  renderSidebar();
  applyConversationModel(conversation);
  applyConversationAgent(conversation);
  renderMessages(conversation.messages || []);
  renderPermissionModeSwitch();
  // 联网搜索开关由对话数据库字段恢复，不依赖当前浏览器。
  state.webSearchEnabled = Boolean(Number(conversation.web_search_enabled || 0));
  state.deepReasoningEnabled = Boolean(Number(conversation.deep_reasoning_enabled || 0));
  state.reasoningEffort = conversation.reasoning_effort || (state.deepReasoningEnabled ? 'medium' : 'auto');
  updateDeepReasoningButton();
  applyConversationLightweight(conversation);
  await resumeConversationRun(id);
  renderConversationRuleBar();
  closeSidebar();
}

export function conversationSnapshot(conversation) {
  const messages = Array.isArray(conversation?.messages) ? conversation.messages : [];
  const last = messages.at(-1);
  return [conversation?.updated_at || '', messages.length, last?.id || '', last?.role || ''].join('|');
}

export async function syncCurrentConversation() {
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
    renderSidebar();
    renderMessages(conversation.messages || []);
    renderPermissionModeSwitch();
    state.webSearchEnabled = Boolean(Number(conversation.web_search_enabled || 0));
    state.deepReasoningEnabled = Boolean(Number(conversation.deep_reasoning_enabled || 0));
    updateDeepReasoningButton();
    applyConversationLightweight(conversation);
    renderConversationRuleBar();
  } catch (error) {
    console.debug('[naiba] 对话同步失败:', error.message);
  } finally {
    state.syncInFlight = false;
  }
}

export function startConversationSync() {
  if (state.syncPolling) return;
  state.syncPolling = true;
  scheduleConversationSync(1800);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      if (state.syncTimer) window.clearTimeout(state.syncTimer);
      state.syncTimer = null;
    } else {
      scheduleConversationSync(0);
    }
  });
}

export function scheduleConversationSync(delay = null) {
  if (!state.syncPolling || document.visibilityState === 'hidden') return;
  if (state.syncTimer) window.clearTimeout(state.syncTimer);
  const activeTask = state.tasks.some((task) => activeTaskStatuses.has(task.status));
  const activeRun = Boolean(state.chatRunId || state.abortController || state.checkRunEligible);
  const interval = activeTask || activeRun ? 1800 : 10000;
  state.syncTimer = window.setTimeout(async () => {
    state.syncTimer = null;
    await syncCurrentConversation();
    scheduleConversationSync();
  }, delay ?? interval);
}

export function taskModeLabel(task) {
  return '普通';
}

export function taskElapsed(task) {
  const start = Number(task.started_at || task.created_at || 0);
  const end = Number(task.finished_at || Date.now());
  if (!start || end < start) return '';
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}

export function renderRunTasks() {
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

export function renderConversationRuleBar() {
  const bar = $('#conversationRuleBar');
  const text = $('#conversationRuleText');
  if (!bar || !text) return;
  const conversation = state.conversations.find((item) => item.id === state.conversationId);
  if (!state.conversationId || !conversation) {
    bar.hidden = true;
    return;
  }
  // 选项按钮（#choiceButtons）展开时让位，避免规则栏与按钮重叠。
  // 按钮存在期间只维护 hidden 状态、绝不显示；按钮被 hideChoiceButtons() 移除后，
  // 下一次渲染自然走 hidden=false 把规则栏还回来。
  if ($('#choiceButtons')) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  const prompt = String(conversation.system_prompt || '').trim();
  if (!prompt) {
    text.textContent = '本次对话规则：未设置';
    bar.classList.remove('has-rule');
    bar.title = '在「对话设置」中设置本次对话规则';
  } else {
    const compact = prompt.replace(/\s+/g, ' ');
    const summary = compact.length > 40 ? `${compact.slice(0, 40)}…` : compact;
    text.textContent = `本次对话规则：${summary}`;
    bar.classList.add('has-rule');
    bar.title = '在「对话设置」中查看或修改';
  }
}

export async function importCharacterCard(file) {
  if (!file) return;
  try {
    const data = await readAsDataUrl(file);
    const result = await api('/api/character-card/parse', {
      method: 'POST',
      body: { name: file.name, data },
    });
    const prompt = String(result.system_prompt || '').trim();
    if (!prompt) {
      toast('角色卡解析结果为空');
      return;
    }
    if ($('#conversationSystemPrompt').value.trim() && $('#conversationSystemPrompt').value.trim() !== prompt
      && !confirm('当前系统提示词已有内容，是否覆盖？')) return;
    $('#conversationSystemPrompt').value = prompt;
    await loadConversationPromptPresets();
    const conversation = state.conversations.find((item) => item.id === state.conversationSettingsId);
    const agentId = String(conversation?.agent_id || '');
    const agent = (state.bootstrap?.agents || []).find((item) => String(item.id) === agentId);
    const agentPrompt = String(agent?.system_prompt || '').trim();
    const cardName = result.meta?.name || '未知角色';
    if (agentPrompt) {
      toast(`已导入角色卡「${cardName}」。提示：当前 Agent 自带系统提示词，建议切换到提示词留空的 Agent 再扮演。`);
    } else {
      toast(`已导入角色卡「${cardName}」，确认后点保存`);
    }
  } catch (error) {
    toast(`导入失败：${error.message}`);
  }
}

export async function loadConversationPromptPresets() {
  try {
    const result = await api('/api/conversation-prompt-presets');
    state.conversationPromptPresets = Array.isArray(result.presets) ? result.presets : [];
    renderConversationPromptPresetSelect();
    renderConversationPromptPresets();
  } catch (error) {
    state.conversationPromptPresets = [];
    renderConversationPromptPresetSelect();
  }
}

export function renderConversationPromptPresetSelect() {
  const select = $('#conversationPromptPresetSelect');
  if (!select) return;
  select.innerHTML = '<option value="">选择快捷系统提示词…</option>'
    + state.conversationPromptPresets.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)}</option>`).join('');
}

export function renderConversationPromptPresets() {
  const container = $('#conversationPromptPresetList');
  if (!container) return;
  const query = String($('#conversationPromptPresetSearch')?.value || '').trim().toLowerCase();
  const items = state.conversationPromptPresets.filter((item) => !query || `${item.title} ${item.system_prompt}`.toLowerCase().includes(query));
  container.innerHTML = items.length ? items.map((item) => {
    const preview = String(item.system_prompt || '').replace(/\s+/g, ' ').slice(0, 150);
    return `<article class="conversation-preset-item" data-conversation-preset-id="${escapeHtml(item.id)}"><div><b>${escapeHtml(item.title)}</b><small>${escapeHtml(item.source || '手动创建')}</small><p>${escapeHtml(preview)}${String(item.system_prompt || '').length > 150 ? '…' : ''}</p></div><span><button class="control-button tiny" type="button" data-conversation-preset-edit="${escapeHtml(item.id)}">编辑</button><button class="danger-button tiny" type="button" data-conversation-preset-delete="${escapeHtml(item.id)}">删除</button></span></article>`;
  }).join('') : '<p class="hint">尚无快捷系统提示词。</p>';
}

export function openConversationPromptPresetForm(id = '') {
  const item = state.conversationPromptPresets.find((preset) => preset.id === id) || {};
  state.editingConversationPromptPresetId = id;
  $('#conversationPromptPresetId').value = id;
  $('#conversationPromptPresetTitle').value = item.title || '';
  $('#conversationPromptPresetText').value = item.system_prompt || '';
  $('#conversationPromptPresetForm').hidden = false;
  $('#conversationPromptPresetTitle').focus();
}

export function closeConversationPromptPresetForm() {
  state.editingConversationPromptPresetId = '';
  $('#conversationPromptPresetForm').hidden = true;
}

export async function saveConversationPromptPreset(event) {
  event.preventDefault();
  const id = state.editingConversationPromptPresetId;
  const title = $('#conversationPromptPresetTitle').value;
  const system_prompt = $('#conversationPromptPresetText').value;
  try {
    await api(id ? `/api/conversation-prompt-presets/${encodeURIComponent(id)}` : '/api/conversation-prompt-presets', { method: 'POST', body: { title, system_prompt } });
    closeConversationPromptPresetForm();
    await loadConversationPromptPresets();
    toast(id ? '已更新快捷提示词' : '已新增快捷提示词');
  } catch (error) { toast(`保存失败：${error.message}`); }
}

export async function importConversationPromptPresetCard(file) {
  if (!file) return;
  try {
    const result = await api('/api/character-card/parse', { method: 'POST', body: { name: file.name, data: await readAsDataUrl(file) } });
    if (!result.system_prompt) throw new Error('角色卡解析结果为空');
    await loadConversationPromptPresets();
    toast(`已收录角色卡「${result.preset?.title || result.meta?.name || file.name}」为快捷提示词`);
  } catch (error) { toast(`导入失败：${error.message}`); }
}

export async function applyConversationPromptPreset(id) {
  const item = state.conversationPromptPresets.find((preset) => preset.id === id);
  const field = $('#conversationSystemPrompt');
  if (!item || !field) return;
  const next = String(item.system_prompt || '');
  if (field.value.trim() && field.value.trim() !== next && !confirm('当前系统提示词已有不同内容，是否覆盖？')) {
    $('#conversationPromptPresetSelect').value = '';
    return;
  }
  field.value = next;
}

export function openConversationSettings(id) {
  const conversation = state.conversations.find((item) => item.id === id);
  if (!conversation) return;
  state.conversationSettingsId = id;
  $('#conversationSettingsTitle').textContent = conversation.title || '当前对话';
  $('#conversationTitle').value = conversation.title_customized ? (conversation.title || '') : '';
  $('#conversationSystemPrompt').value = conversation.system_prompt || '';
  $('#conversationStreamEnabled').checked = Number(conversation.stream_enabled ?? 1) !== 0;
  $('#conversationSettingsDialog').showModal();
}

export async function saveConversationSettings(event) {
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
    renderSidebar();
    if (id === state.conversationId) {
      applyConversationLightweight(updated);
    }
    renderConversationRuleBar();
    toast('对话设置已保存');
  } catch (error) {
    toast(`保存失败：${error.message}`);
  } finally {
    saveButton.disabled = false;
  }
}

export async function clearConversationMessages() {
  const id = state.conversationSettingsId;
  if (!id) return;
  if (!confirm('确定清空这个对话的全部消息和工具记录吗？此操作无法恢复。')) return;
  if (!confirm('请再次确认：要永久清空当前对话吗？')) return;
  try {
    await api(`/api/conversations/${encodeURIComponent(id)}/messages`, { method: 'DELETE' });
    $('#conversationSettingsDialog').close();
    if (id === state.conversationId) renderMessages([]);
    await loadConversations();
    toast('对话已清空');
  } catch (error) {
    toast(`清空失败：${error.message}`);
  }
}

export async function clearTerminalTasks() {
  if (!confirm('清理所有已结束、失败、取消或中断的异步任务记录吗？运行中的任务不会受影响。')) return;
  try {
    const result = await api('/api/tasks/clear', { method: 'DELETE' });
    await loadTasks();
    toast(`已清理 ${Number(result.deleted || 0)} 个任务`);
  } catch (error) {
    toast(`清理失败：${error.message}`);
  }
}

export async function deleteConversation(id) {
  if (id === state.conversationId && state.chatRunId) {
    toast('请先停止当前回复再删除对话');
    return;
  }
  const conversation = state.conversations.find((item) => item.id === id);
  if (!confirm(`删除对话"${conversation?.title || '新对话'}"？`)) return;
  await api(`/api/conversations/${id}`, { method: 'DELETE' });
  const wasCurrent = state.conversationId === id;
  state.conversations = state.conversations.filter((item) => item.id !== id);
  if (wasCurrent) state.conversationId = '';
  renderSidebar();
  // 删除非当前会话：只刷新侧栏。绝不能重新加载当前会话视图，否则会把正在流式
  // 输出的消息行挤出 DOM（renderMessages 重建列表），导致流式输出消失、要等 run
  // 结束重新渲染才重现。当前会话的流式输出必须保持原样继续。
  if (!wasCurrent) return;
  if (state.conversations.length) await openConversation(state.conversations[0].id);
  else {
    renderMessages([]);
  }
}

// 当前对话绑定的 Agent 的固定 Skill id 列表；未绑定或已删除时回退到默认 Agent
export function currentAgentFixedSkillIds() {
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

// 有效启用的 Skill = 仅当前会话 Agent 预设的固定 Skill（不再允许用户自行选择/切换模式）。
export function effectiveSkillIds() {
  return [...new Set(currentAgentFixedSkillIds())];
}

