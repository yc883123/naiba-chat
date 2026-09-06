// ============================================================
// 14-file-panel.js —— 拆分自 public/app.js 第 6329-6657 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, api, escapeHtml, state, toast } from "./01-core.js";
import { renderSidebar } from "./08-conversations.js";
import { markdownFilePreview } from "./12-chat-input.js";
export const filePanelState = { open: false, activeKey: '', tabs: [] };
export const FILE_MD_NAME_RE = /\.(md|markdown|mdown)$/i;

export function filePanelTabKey(raw) {
  return String(raw || '').replace(/\\/g, '/').toLowerCase();
}

export function filePanelUsable() {
  return window.innerWidth > 760 && !!$('#filePanel');
}

export function filePanelWidthPx() {
  const saved = parseFloat(localStorage.getItem('naibaChatFilePanelW') || ''); const max = Math.max(300, Math.floor(window.innerWidth * 0.5));
  return Math.max(280, Math.min(max, Number.isFinite(saved) ? saved : Math.round(window.innerWidth * 0.36)));
}

export function applyFilePanelOpenClass() {
  const shell = $('#appShell');
  if (!shell) return;
  shell.classList.toggle('file-panel-open', filePanelState.open);
  if (filePanelState.open) shell.style.setProperty('--file-panel-w', `${filePanelWidthPx()}px`);
}

export function openFilePanel(rawPath) {
  if (!filePanelUsable()) return false; // 手机端仅展示总结，不打开面板
  if (!rawPath) return false;
  if (!state.conversationId) {
    toast('请先打开一个会话');
    return false;
  }
  filePanelState.open = true;
  applyFilePanelOpenClass();
  const tab = ensureFileTab(rawPath, true);
  renderFilePanel();
  if (tab && !tab.info && !tab.loading) loadFileTab(tab);
  return true;
}

export function closeFilePanel(clearTabs = false) {
  filePanelState.open = false;
  if (clearTabs) {
    filePanelState.tabs = [];
    filePanelState.activeKey = '';
  } else if (!filePanelState.tabs.some((item) => item.key === filePanelState.activeKey)) {
    // Keep the most recently opened file visible when the panel is reopened.
    filePanelState.activeKey = filePanelState.tabs[0]?.key || '';
  }
  applyFilePanelOpenClass();
  renderFilePanel();
}

export function ensureFileTab(rawPath, activate = false) {
  const key = filePanelTabKey(rawPath);
  let tab = filePanelState.tabs.find((item) => item.key === key);
  if (!tab) {
    const name = String(rawPath).replace(/\\/g, '/').split('/').pop() || rawPath;
    tab = { key, raw: String(rawPath), name, info: null, loading: false, error: '', editing: false, draft: null };
    filePanelState.tabs.push(tab);
  }
  if (activate) filePanelState.activeKey = key;
  return tab;
}

export function activeFileTab() {
  return filePanelState.tabs.find((item) => item.key === filePanelState.activeKey) || null;
}

export async function loadFileTab(tab) {
  if (!tab || tab.loading || tab.info) return;
  tab.loading = true;
  tab.error = '';
  renderFilePanel();
  try {
    const info = await api(`/api/conversations/${encodeURIComponent(state.conversationId)}/file/open?path=${encodeURIComponent(tab.raw)}`);
    tab.info = info;
    tab.draft = null;
    tab.editing = false;
  } catch (error) {
    tab.error = error.message || '读取文件失败';
  } finally {
    tab.loading = false;
    renderFilePanel();
  }
}

export function activateFileTab(key) {
  const tab = filePanelState.tabs.find((item) => item.key === key);
  if (!tab) return;
  filePanelState.activeKey = key;
  renderFilePanel();
  if (!tab.info && !tab.loading && !tab.error) loadFileTab(tab);
}

export function removeFileTab(key) {
  const index = filePanelState.tabs.findIndex((item) => item.key === key);
  if (index < 0) return;
  filePanelState.tabs.splice(index, 1);
  if (filePanelState.activeKey === key) {
    const next = filePanelState.tabs[index] || filePanelState.tabs[index - 1] || null;
    filePanelState.activeKey = next ? next.key : '';
  }
  if (!filePanelState.tabs.length) filePanelState.open = false;
  renderFilePanel();
}

export function renderFilePanel() {
  const panel = $('#filePanel');
  if (!panel) return;
  applyFilePanelOpenClass();
  renderFilePanelTabs();
  renderFilePanelBody();
  updateFileTabsButton();
}

export function renderFilePanelTabs() {
  const tabsEl = $('#fileTabs');
  if (!tabsEl) return;
  if (!filePanelState.tabs.length) {
    tabsEl.innerHTML = '';
    return;
  }
  tabsEl.innerHTML = filePanelState.tabs.map((tab) => {
    const dirty = tab.editing && tab.draft !== null && tab.draft !== tab.info?.content;
    const active = tab.key === filePanelState.activeKey;
    return `<span class="file-tab${active ? ' active' : ''}${dirty ? ' file-tab-dirty' : ''}" role="tab" aria-selected="${active}" data-file-tab="${escapeHtml(tab.key)}" title="${escapeHtml(tab.raw)}"><span class="file-tab-name">${escapeHtml(tab.name)}</span><button type="button" class="file-tab-x" data-file-tab-close="${escapeHtml(tab.key)}" aria-label="关闭 ${escapeHtml(tab.name)}" tabindex="-1"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg></button></span>`;
  }).join('');
  const activeEl = tabsEl.querySelector('[data-file-tab].active');
  if (activeEl && typeof activeEl.scrollIntoView === 'function') activeEl.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

export function fileToolbarMeta(tab) {
  const info = tab.info || {};
  const parts = [];
  if (info.kind === 'image') parts.push('图片');
  else if (info.kind === 'binary') parts.push('二进制');
  else if (tab.editing) parts.push('编辑中');
  else parts.push(FILE_MD_NAME_RE.test(info.name || '') ? 'Markdown' : '文本');
  if (info.size != null) parts.push(formatFileSize(info.size));
  if (info.truncated) parts.push('仅预览前 2MB');
  return parts.join(' · ');
}

export function renderFilePanelBody() {
  const body = $('#filePanelBody');
  if (!body) return;
  if (!filePanelState.open) {
    body.innerHTML = '';
    return;
  }
  const tab = activeFileTab();
  if (!tab) {
    body.innerHTML = '<div class="file-panel-hint">点击消息末尾「本轮修改文件」中的文件名，在右侧查看文件内容。<br>Markdown / 文本可手动编辑并保存回磁盘。</div>';
    return;
  }
  const info = tab.info;
  if (tab.loading) {
    body.innerHTML = '<div class="file-panel-hint"><div class="file-loading">正在读取文件…</div></div>';
    return;
  }
  if (tab.error) {
    body.innerHTML = `<div class="file-panel-hint">无法读取文件：${escapeHtml(tab.error)}</div>`;
    return;
  }
  if (!info) {
    body.innerHTML = '<div class="file-panel-hint">文件尚未加载。</div>';
    return;
  }
  const savableText = info.kind === 'text' && info.savable && !info.truncated && !tab.editing;
  const actionHtml = savableText
    ? '<button type="button" class="control-button" data-file-edit>编辑</button>'
    : tab.editing
      ? '<button type="button" class="control-button" data-file-edit-cancel>取消</button><button type="button" class="primary-button" data-file-save>保存</button>'
      : '';
  const truncNote = info.truncated ? '<small>（截断）</small>' : '';
  body.innerHTML = `
    <div class="file-view">
      <div class="file-view-toolbar">
        <div class="file-view-title">
          <b title="${escapeHtml(info.path || tab.raw)}">${escapeHtml(info.name || tab.name)}${truncNote}</b>
          <small>${escapeHtml(fileToolbarMeta(tab))}${info.path ? ` · ${escapeHtml(info.path)}` : ''}</small>
        </div>
        <div class="file-view-actions">${actionHtml}</div>
      </div>
      ${fileContentViewHtml(tab)}
    </div>`;
  if (tab.editing) {
    const textarea = body.querySelector('.file-edit-textarea');
    if (textarea) {
      textarea.focus();
      textarea.addEventListener('input', () => { tab.draft = textarea.value; });
      textarea.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); cancelFileEdit(tab.key); }
        else if ((event.ctrlKey || event.metaKey) && (event.key === 's' || event.key === 'S')) { event.preventDefault(); saveFileTab(tab.key); }
        else if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); saveFileTab(tab.key); }
      });
    }
  }
}

export function fileContentViewHtml(tab) {
  const info = tab.info || {};
  if (tab.editing) {
    const value = tab.draft !== null && tab.draft !== undefined ? tab.draft : (info.content || '');
    return `<div class="file-edit-area"><textarea class="file-edit-textarea" spellcheck="false" aria-label="编辑 ${escapeHtml(info.name || '')}">${escapeHtml(value)}</textarea><div class="file-edit-foot"><span>Ctrl/⌘ + S 或 Ctrl/⌘ + Enter 保存 · Esc 取消</span></div></div>`;
  }
  if (info.kind === 'image') {
    const url = convFileRawUrl(info.path || tab.raw);
    return `<div class="file-image-wrap"><img src="${escapeHtml(url)}" alt="${escapeHtml(info.name || '')}" data-large-url="${escapeHtml(url)}"></div>`;
  }
  if (info.kind === 'binary') {
    return `<div class="file-binary-note"><p>这是二进制文件，无法在此预览。</p><p>大小：${escapeHtml(formatFileSize(info.size))}${info.path ? ` · <code>${escapeHtml(info.path)}</code>` : ''}</p></div>`;
  }
  const text = String(info.content || '');
  if (FILE_MD_NAME_RE.test(info.name || '') && !info.truncated) {
    // 文件预览始终按纯文本/Markdown 规则渲染，不受对话富文本开关影响。
    return `<div class="file-preview-md message-body answer-content">${markdownFilePreview(text)}</div>`;
  }
  return `<pre class="file-preview-text">${escapeHtml(text)}</pre>`;
}

export function convFileRawUrl(path) {
  return `/api/conversations/${encodeURIComponent(state.conversationId)}/file/raw?token=${encodeURIComponent(state.token)}&path=${encodeURIComponent(String(path || ''))}`;
}

export function startFileEdit(key) {
  const tab = filePanelState.tabs.find((item) => item.key === key);
  if (!tab || !tab.info || tab.info.kind !== 'text' || !tab.info.savable || tab.info.truncated) {
    toast('该文件不可编辑（仅支持编辑本会话改动过、工作区内且未截断的文本文件）');
    return;
  }
  tab.editing = true;
  tab.draft = tab.info.content || '';
  renderFilePanel();
}

export function cancelFileEdit(key) {
  const tab = filePanelState.tabs.find((item) => item.key === key);
  if (!tab) return;
  tab.editing = false;
  tab.draft = null;
  renderFilePanel();
}

export async function saveFileTab(key) {
  const tab = filePanelState.tabs.find((item) => item.key === key);
  if (!tab || !tab.info) return;
  const textarea = $('#filePanelBody .file-edit-textarea');
  const content = textarea ? textarea.value : (tab.draft !== null ? tab.draft : tab.info.content);
  const targetPath = tab.raw;
  try {
    await api(`/api/conversations/${encodeURIComponent(state.conversationId)}/file/save`, {
      method: 'POST',
      body: { path: targetPath, content },
    });
    toast(`已保存 ${tab.name}`);
    tab.draft = null;
    tab.editing = false;
    tab.info = null; // 重新读取以刷新 content/mtime
    loadFileTab(tab);
  } catch (error) {
    toast(`保存失败：${error.message}`);
  }
}

export function formatFileSize(bytes) {
  const size = Number(bytes || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export function openSidebar() {
  $('#sidebar').classList.add('open');
  $('#sidebarBackdrop').classList.add('open');
}

export function closeSidebar() {
  $('#sidebar').classList.remove('open');
  $('#sidebarBackdrop').classList.remove('open');
}

// ---- 左右侧栏折叠 / 展开（桌面端；手机端侧栏保持抽屉式开关）----
export function sidebarDesktop() {
  return window.innerWidth > 760;
}

export function setLeftSidebarCollapsed(collapsed) {
  const shell = $('#appShell');
  if (!shell) return;
  if (collapsed && !sidebarDesktop()) return; // 手机抽屉由 openSidebar/closeSidebar 管理
  shell.classList.toggle('sidebar-collapsed', Boolean(collapsed));
  if (collapsed) localStorage.setItem('naibaChatSidebarCollapsed', '1');
  else localStorage.removeItem('naibaChatSidebarCollapsed');
  renderSidebar();
}

export function restoreLeftSidebarCollapse() {
  const shell = $('#appShell');
  if (!shell) return;
  const collapsed = sidebarDesktop() && localStorage.getItem('naibaChatSidebarCollapsed') === '1';
  shell.classList.toggle('sidebar-collapsed', Boolean(collapsed));
}

// 文件面板重开：右侧栏收起但标签还在时，从顶栏「文件 N」重新展开
export function reopenFilePanel() {
  if (!filePanelUsable()) return;
  // 仅当已点开过文件（存在保留的标签）时顶栏按钮才出现；空会话不展示入口
  if (!filePanelState.tabs.length) return;
  filePanelState.open = true;
  applyFilePanelOpenClass();
  renderFilePanel();
}

export function updateFileTabsButton() {
  const button = $('#openFileTabs');
  if (!button) return;
  const hasTabs = filePanelState.tabs.length > 0;
  // 旧逻辑：顶栏「文件 N」是"面板收起后的重开入口"——点过文件 chip 才有按钮
  const show = filePanelUsable() && !filePanelState.open && hasTabs;
  button.hidden = !show;
  button.classList.toggle('has-tabs', hasTabs);
  button.title = '重新打开文件面板（保留已打开的文件标签）';
  const count = $('#fileTabsCount');
  if (count) {
    count.textContent = hasTabs ? String(filePanelState.tabs.length) : '';
    count.hidden = !hasTabs;
  }
}

