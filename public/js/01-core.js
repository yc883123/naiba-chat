// ============================================================
// 01-core.js —— 拆分自 public/app.js 第 1-397 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { resizeTextarea } from "./13-skill-refs.js";
export const urlToken = new URLSearchParams(location.search).get('token') || '';
if (urlToken) {
  localStorage.setItem('naibaChatToken', urlToken);
  history.replaceState(null, '', location.pathname);
}

export const storedSkillIds = JSON.parse(localStorage.getItem('naibaChatSkillIds') || localStorage.getItem('lanSkillIds') || '[]');
export const storedSkillMode = localStorage.getItem('naibaChatSkillMode');
export const legacyAutoSkills = localStorage.getItem('naibaChatAutoSkills') ?? localStorage.getItem('lanAutoSkills');
export const initialSkillMode = ['auto', 'pinned', 'exclusive'].includes(storedSkillMode)
  ? storedSkillMode
  : (legacyAutoSkills === 'false' && storedSkillIds.length ? 'pinned' : 'auto');

export const state = {
  token: urlToken || localStorage.getItem('naibaChatToken') || localStorage.getItem('lanSkillToken') || '',
  bootstrap: null,
  conversations: [],
  conversationId: '',
  selectedSkills: storedSkillIds,
  skillMode: initialSkillMode,
  pendingFiles: [],
  abortController: null,
  chatRunId: '',
  runConversationId: '',
  runSequence: 0,
  runEvents: {},
  runRow: null,
  runReconnectTimers: new Set(),
  cancelRequested: false,
  cancelConversationId: '',
  cancelledRunIds: new Set(),
  // Run 过程看护/重连状态
  runGeneration: 0,        // 每次重建流自增，旧代回调一律丢弃，防竞态覆盖
  runAttempt: 0,           // 本次连接生命周期内的重连次数（指数退避用）
  runReconnectAt: 0,       // 重连冷却截止时间戳；0 表示无需冷却
  runLastActivityAt: 0,    // 事件流最近一次活跃时间戳（含 heartbeat，用于看门狗判死）
  runContentActivityAt: 0, // 最近一次真实内容事件时间戳（不含 heartbeat，用于“等待中”计时）
  runWatchdogTimer: null,  // 看门狗定时器句柄
  runWaitTimer: null,      // “无进展等待”轻量计时器句柄（每 1s）
  runWaitShown: false,     // 当前是否正在显示“等待中 · 已等待 X 秒”
  runWaitPrevText: '',     // 显示等待前的 #runtimeStatus 原文，用于恢复
  runProbeMisses: 0,       // 看门狗连续判定空闲计数
  runRecovering: false,    // 防止 看门狗/轮询 双触发重连的互斥锁
  connectionState: 'connected', // 'connected' | 'reconnecting'（去重角标依据）
  checkRunEligible: false, // 是否处于"等待轮询兜底恢复"的状态
  elapsedTimer: null,      // “已等待 X 秒”计时器句柄
  elapsedBase: '',
  elapsedSince: 0,
  taskSubmitting: false,
  conversationSettingsId: '',
  providerEditing: false,
  providerIsNew: false,
  providerKindTab: 'online',
  syncTimer: null,
  syncInFlight: false,
  syncPolling: false,
  updatePollTimer: null,
  conversationSnapshot: '',
  // 首轮上下文（系统提示词 + 工具集）折叠卡数据；切换会话时由 openConversation 拉取
  firstTurnInfo: null,
  agentFormSkillIds: [],
  agentFormToolScope: [],
  agentFormIsNew: false,
  // 旧配置兼容用：当前工具目录里已不存在的工具名（如已移除的 activate_skill、
  // 临时掉线的 MCP 工具）。原样保留、单独展示，不参与勾选/计数/预设匹配，
  // 保存时随已知工具一起写回，避免用户重配。
  agentFormUnknownTools: [],
  // 旧 Agent 的 tool_scope 为空 = 不限制（运行时全放行，且新工具自动纳入）。
  // 界面上按“全选”展示，但只要用户没动过就仍以空数组保存，避免被固化成死列表。
  agentFormUnrestricted: false,
  agentFormScopeTouched: false,
  // 自定义工具模板：把某个自定义组合存成命名模板（localStorage 全局持久化），
  // 之后在任意 Agent 表单里点一下模板芯片即可一键复刻。
  toolTemplates: [],
  toolTemplatesLoaded: false,
  toolCatalog: null,
  tasks: [],
  taskTimer: null,
  taskPollInFlight: false,
  taskPolling: false,
  mcpPollInFlight: false,
  mcpPolling: false,
  mcpPollTimer: null,
  visionTimer: null,
  visionStartedAt: 0,
  webSearchEnabled: false,
  deepReasoningEnabled: false,
  reasoningEffort: 'auto',
  lightweightMode: false,
  lightweightDisabledFeatures: [],
  richTextEnabled: false,
  contextUsage: null,
  providerModelCapabilities: {},
  workspaces: [],
  workspaceSort: 'updated',
  workspaceSearch: '',
  expandedGroups: new Set(),
  customPrompts: [],
  editingStarterPrompt: -1,
  conversationPromptPresets: [],
  editingConversationPromptPresetId: '',
  // A fresh update check should immediately surface a newer release in the
  // closed select; user choices made afterwards must still be preserved.
  updateAutoSelectLatest: false,
};
export const draggedFileCache = new Map();

export const $ = (selector) => document.querySelector(selector);
export const $$ = (selector) => [...document.querySelectorAll(selector)];

export const emptyStateElement = $('#emptyState');

export async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (options.body && typeof options.body !== 'string') {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { ...options, headers });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

export function toast(message) {
  const element = $('#toast');
  element.textContent = message;
  if (typeof element.show === 'function' && !element.open) {
    element.show();
  }
  element.classList.remove('show');
  // 强制一次重排再显示，确保每次都能播放淡入动画
  void element.offsetWidth;
  element.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    element.classList.remove('show');
    if (typeof element.close === 'function' && element.open) {
      element.close();
    }
  }, 2200);
}

export async function copyText(text) {
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

export let contextMenuSelection = '';
export let contextMenuPreviousFocus = null;
export let contextMenuMode = 'selection'; // 'selection' | 'edit'
export let contextMenuTarget = null;
export let contextMenuRangeStart = 0;
export let contextMenuRangeEnd = 0;

export function editableElement(target) {
  if (!(target instanceof Element)) return null;
  const editable = target.closest('textarea, input, [contenteditable="true"]');
  if (!editable) return null;
  if (editable instanceof HTMLInputElement
      && ['button', 'checkbox', 'color', 'file', 'hidden', 'image', 'radio', 'range', 'reset', 'submit'].includes(editable.type)) {
    return null;
  }
  return editable;
}

export function ensureContextMenu() {
  let menu = $('#textContextMenu');
  if (menu) return menu;
  menu = document.createElement('div');
  menu.id = 'textContextMenu';
  menu.className = 'text-context-menu';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', '文本操作');
  menu.hidden = true;
  document.body.append(menu);
  return menu;
}

export function setContextMenuItems() {
  const menu = ensureContextMenu();
  if (contextMenuMode === 'edit') {
    menu.setAttribute('aria-label', '文本框操作');
    menu.innerHTML = `
      <button type="button" role="menuitem" data-context-action="undo">撤销</button>
      <button type="button" role="menuitem" data-context-action="redo">重做</button>
      <button type="button" role="menuitem" data-context-action="cut">剪切</button>
      <button type="button" role="menuitem" data-context-action="copy">复制</button>
      <button type="button" role="menuitem" data-context-action="paste">粘贴</button>
      <button type="button" role="menuitem" data-context-action="delete">删除</button>
      <button type="button" role="menuitem" data-context-action="select-all">全选</button>`;
  } else {
    menu.setAttribute('aria-label', '选中文本操作');
    menu.innerHTML = `
      <button type="button" role="menuitem" data-context-action="copy">复制选中</button>
      <button type="button" role="menuitem" data-context-action="quote">快速发送</button>`;
  }
}

export function hideTextContextMenu() {
  const menu = $('#textContextMenu');
  if (menu) menu.hidden = true;
}

export function showTextContextMenu(event, selection = '', mode = 'selection', target = null) {
  const menu = ensureContextMenu();
  contextMenuSelection = selection;
  contextMenuMode = mode;
  contextMenuTarget = target;
  contextMenuPreviousFocus = document.activeElement;
  if (target && typeof target.selectionStart === 'number') {
    contextMenuRangeStart = target.selectionStart;
    contextMenuRangeEnd = target.selectionEnd;
  } else {
    contextMenuRangeStart = 0;
    contextMenuRangeEnd = 0;
  }
  setContextMenuItems();
  menu.hidden = false;
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  menu.style.left = `${Math.max(6, Math.min(event.clientX, window.innerWidth - width - 6))}px`;
  menu.style.top = `${Math.max(6, Math.min(event.clientY, window.innerHeight - height - 6))}px`;
  // 不要自动聚焦菜单按钮，否则文本框会失焦，选中高亮会消失。
}

export function focusContextTarget() {
  const el = contextMenuTarget;
  if (!el) return;
  el.focus({ preventScroll: true });
  if (typeof el.setSelectionRange === 'function') {
    try {
      el.setSelectionRange(contextMenuRangeStart, contextMenuRangeEnd);
    } catch (_) { /* 忽略 */ }
  }
}

export function editableSelectedText() {
  const el = contextMenuTarget;
  if (!el) return '';
  if (typeof el.value === 'string' && typeof el.selectionStart === 'number') {
    return el.value.substring(el.selectionStart, el.selectionEnd);
  }
  const sel = window.getSelection();
  return sel ? sel.toString() : '';
}

export function insertTextIntoEditable(text) {
  const el = contextMenuTarget;
  if (!el) return false;
  if (typeof el.value === 'string' && typeof el.selectionStart === 'number') {
    const start = el.selectionStart;
    const end = el.selectionEnd;
    el.value = el.value.slice(0, start) + text + el.value.slice(end);
    el.setSelectionRange(start + text.length, start + text.length);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  }
  if (el.isContentEditable) {
    el.focus();
    return document.execCommand('insertText', false, text);
  }
  return false;
}

export async function runTextContextAction(action) {
  try {
    if (contextMenuMode === 'edit') {
      if (['undo', 'redo', 'cut', 'copy', 'paste', 'delete', 'select-all'].includes(action)) {
        focusContextTarget();
      }
      if (action === 'undo') {
        document.execCommand('undo');
        toast('已撤销');
      } else if (action === 'redo') {
        document.execCommand('redo');
        toast('已重做');
      } else if (action === 'cut') {
        if (document.execCommand('cut')) toast('已剪切');
        else toast('剪切失败：浏览器未授权');
      } else if (action === 'copy') {
        const text = editableSelectedText();
        if (text) {
          await copyText(text);
          toast('已复制');
        } else {
          toast('没有可复制的内容');
        }
      } else if (action === 'paste') {
        let ok = false;
        try { ok = document.execCommand('paste'); } catch (_) { /* 忽略 */ }
        if (!ok && navigator.clipboard?.readText) {
          try {
            const text = await navigator.clipboard.readText();
            ok = insertTextIntoEditable(text);
          } catch (_) { /* 忽略 */ }
        }
        if (ok) toast('已粘贴');
        else toast('粘贴失败：浏览器未授权');
      } else if (action === 'delete') {
        const el = contextMenuTarget;
        if (el && typeof el.value === 'string' && typeof el.selectionStart === 'number') {
          if (el.selectionStart === el.selectionEnd) {
            toast('请先选择要删除的内容');
          } else {
            insertTextIntoEditable('');
            toast('已删除');
          }
        } else if (document.execCommand('delete')) {
          toast('已删除');
        } else {
          toast('删除失败');
        }
      } else if (action === 'select-all') {
        const el = contextMenuTarget;
        if (el && typeof el.select === 'function') el.select();
        else if (el && typeof el.setSelectionRange === 'function') el.setSelectionRange(0, el.value.length);
        else document.execCommand('selectAll');
      }
      return;
    }

    if (action === 'copy') {
      await copyText(contextMenuSelection);
      toast('已复制选中内容');
    } else if (action === 'quote') {
      const input = $('#messageInput');
      const quoted = `"${contextMenuSelection.trim()}"`;
      input.value = String(input.value || '').replace(/\s+$/, '') + quoted;
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
      input.dispatchEvent(new Event('input', { bubbles: true }));
      resizeTextarea();
      toast('已追加到输入框');
    }
  } catch (error) {
    toast(`操作失败：${error.message}`);
  } finally {
    hideTextContextMenu();
  }
}

export function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}
export function restoreSafeHtml(escaped) {
  if (!String(escaped || '').includes('&lt;')) return String(escaped || '');
  const colors = new Set(['black','silver','gray','white','maroon','red','purple','fuchsia','green','lime','olive','yellow','navy','blue','teal','aqua','orange','aliceblue','transparent']);
  const decode = (s) => String(s).replace(/&quot;/g, '"').replace(/&#039;/g, "'").replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
  return String(escaped || '').replace(/&lt;!--[\s\S]*?--&gt;/gi, '').replace(/&lt;(\/?)(font|span|b|i|u|s)([\s\S]*?)&gt;/gi, (full, slash, name, raw) => {
    const tag = name.toLowerCase(); if (slash) return `</${tag}>`; const attrs = decode(raw).trim(); if (!attrs) return `<${tag}>`; if (!['font','span'].includes(tag)) return full;
    const m = attrs.match(/^color\s*=\s*["']([^"']+)["']$/i); if (!m) return full; const value = m[1].trim();
    if (!(/^#[0-9a-f]{3,8}$/i.test(value) || colors.has(value.toLowerCase()))) return full; return `<${tag} color="${escapeHtml(value)}">`;
  });
}

// 语言别名归一化：把常见标识归到同一套规则。
export function normalizeLanguage(language) {
  const lang = String(language || '').toLowerCase().trim();
  if (/^(js|javascript|jsx|mjs|cjs)$/.test(lang)) return 'js';
  if (/^(ts|typescript|tsx)$/.test(lang)) return 'ts';
  if (/^(py|python|python3)$/.test(lang)) return 'py';
  if (/^(json|json5|jsonc)$/.test(lang)) return 'json';
  if (/^(sh|bash|shell|zsh|powershell|ps1|cmd|bat)$/.test(lang)) return 'bash';
  if (/^(html|htm|xml|svg)$/.test(lang)) return 'html';
  if (/^(css|scss|less)$/.test(lang)) return 'css';
  if (/^(ya?ml)$/.test(lang)) return 'yaml';
  if (/^(java|c|cpp|csharp|cs|go|rust|rs|php|rb|ruby|swift|kt|kotlin|scala|sql)$/.test(lang)) return 'js';
  return '';
}

// 单条组合正则 + 线性扫描：token 先 escape 再包 span，输出安全的 HTML。
