const urlToken = new URLSearchParams(location.search).get('token') || '';
if (urlToken) {
  localStorage.setItem('naibaChatToken', urlToken);
  history.replaceState(null, '', location.pathname);
}

const storedSkillIds = JSON.parse(localStorage.getItem('naibaChatSkillIds') || localStorage.getItem('lanSkillIds') || '[]');
const storedSkillMode = localStorage.getItem('naibaChatSkillMode');
const legacyAutoSkills = localStorage.getItem('naibaChatAutoSkills') ?? localStorage.getItem('lanAutoSkills');
const initialSkillMode = ['auto', 'pinned', 'exclusive'].includes(storedSkillMode)
  ? storedSkillMode
  : (legacyAutoSkills === 'false' && storedSkillIds.length ? 'pinned' : 'auto');

const state = {
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
  updatePollTimer: null,
  conversationSnapshot: '',
  agentFormSkillIds: [],
  agentFormToolScope: [],
  agentFormIsNew: false,
  toolCatalog: null,
  tasks: [],
  taskTimer: null,
  visionTimer: null,
  visionStartedAt: 0,
  webSearchEnabled: false,
  deepReasoningEnabled: false,
  reasoningEffort: 'auto',
  lightweightMode: false,
  lightweightDisabledFeatures: ['tools', 'skills'],
  contextUsage: null,
  providerModelCapabilities: {},
  workspaces: [],
  workspaceSort: 'updated',
  workspaceSearch: '',
  expandedGroups: new Set(),
  customPrompts: [],
  editingStarterPrompt: -1,
};
const draggedFileCache = new Map();

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
  if (!response.ok) {
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function toast(message) {
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

let contextMenuSelection = '';
let contextMenuPreviousFocus = null;
let contextMenuMode = 'selection'; // 'selection' | 'edit'
let contextMenuTarget = null;
let contextMenuRangeStart = 0;
let contextMenuRangeEnd = 0;

function editableElement(target) {
  if (!(target instanceof Element)) return null;
  const editable = target.closest('textarea, input, [contenteditable="true"]');
  if (!editable) return null;
  if (editable instanceof HTMLInputElement
      && ['button', 'checkbox', 'color', 'file', 'hidden', 'image', 'radio', 'range', 'reset', 'submit'].includes(editable.type)) {
    return null;
  }
  return editable;
}

function ensureContextMenu() {
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

function setContextMenuItems() {
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

function hideTextContextMenu() {
  const menu = $('#textContextMenu');
  if (menu) menu.hidden = true;
}

function showTextContextMenu(event, selection = '', mode = 'selection', target = null) {
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

function focusContextTarget() {
  const el = contextMenuTarget;
  if (!el) return;
  el.focus({ preventScroll: true });
  if (typeof el.setSelectionRange === 'function') {
    try {
      el.setSelectionRange(contextMenuRangeStart, contextMenuRangeEnd);
    } catch (_) { /* 忽略 */ }
  }
}

function editableSelectedText() {
  const el = contextMenuTarget;
  if (!el) return '';
  if (typeof el.value === 'string' && typeof el.selectionStart === 'number') {
    return el.value.substring(el.selectionStart, el.selectionEnd);
  }
  const sel = window.getSelection();
  return sel ? sel.toString() : '';
}

function insertTextIntoEditable(text) {
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

async function runTextContextAction(action) {
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

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

// ---- Markdown 内联处理：逐段 / 逐单元格执行，避免加粗/代码/链接跨行、跨段落或跨表格行泄漏 ----
function markdownInline(s) {
  return String(s || '')
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([\s\S]+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

// 防御式表格单元格切分：按 | 切分一行，但：
//  1) 不切分被内联标签（<code>/<a>/<strong>…）包裹的 |（例如 <code>a|b</code> 里的 |）；
//  2) 支持 \| 转义（\| 作为一个字面 | 留在单元格里）。
const TABLE_SHIELD_TAGS = new Set(['code','a','strong','b','em','i','span','del','s','u','sub','sup','pre','mark','kbd']);
function splitMarkdownTableRow(row) {
  const text = String(row || '').trim().replace(/^\|/, '').replace(/\|$/, '');
  const cells = [];
  let current = '';
  let inTag = false;
  const stack = [];
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === '<') {
      const m = text.slice(i).match(/^<\/?\s*([A-Za-z][\w-]*)/);
      if (m) {
        const tag = m[1].toLowerCase();
        if (TABLE_SHIELD_TAGS.has(tag)) {
          if (text[i + 1] === '/') stack.pop();
          else stack.push(tag);
        }
      }
      inTag = true;
      current += ch;
      continue;
    }
    if (ch === '>') { inTag = false; current += ch; continue; }
    if (inTag) { current += ch; continue; }
    if (ch === '|' && text[i - 1] === '\\') {
      current = current.replace(/\\$/, '') + '|';
      continue;
    }
    if (ch === '|' && stack.length === 0) {
      cells.push(current.trim());
      current = '';
      continue;
    }
    current += ch;
  }
  cells.push(current.trim());
  return cells;
}

// 只有“以 | 开头、以 | 结尾且中间至少一个字符”的行才可能是一个表格行，
// 避免把普通含 | 的文本（例如 “a | b，不是表格”）误判。
function isMarkdownTableRow(line) {
  const t = String(line || '').trim();
  if (!t.startsWith('|') || !t.endsWith('|')) return false;
  return t.length > 2;
}

// 分隔行：所有单元格都是 - 和可选 :（如 | :-- | --: |）。
function isMarkdownTableSeparator(line) {
  const t = String(line || '').trim();
  if (!t.startsWith('|') || !t.endsWith('|')) return false;
  const cells = splitMarkdownTableRow(t);
  return cells.length >= 1 && cells.every((c) => /^:?-{3,}:?$/.test(c.trim()));
}

function collectMarkdownTable(lines, start) {
  if (start + 1 >= lines.length) return null;
  if (!isMarkdownTableRow(lines[start])) return null;
  if (!isMarkdownTableSeparator(lines[start + 1])) return null;
  const rows = [lines[start], lines[start + 1]];
  let j = start + 2;
  while (j < lines.length) {
    const t = lines[j].trim();
    if (!t) break;
    if (!t.startsWith('|')) break;
    rows.push(lines[j]);
    j++;
  }
  return { rows, end: j };
}

function renderMarkdownTable(rows) {
  // 每行先做内联（把 ** / `code` / [text](url) 换成 <strong>/<code>/<a>），再按 | 切分，
  // 这样 “|” 在内联代码/链接/加粗里不会把单元格切坏；同时把加粗限制在“单行”内，避免跨行泄漏。
  const header = splitMarkdownTableRow(markdownInline(rows[0]));
  const body = rows.slice(2).map((r) => splitMarkdownTableRow(markdownInline(r)));
  const colCount = Math.max(1, header.length, ...body.map((c) => c.length));
  const th = header.map((c) => `<th>${c}</th>`).join('');
  const tbody = body.map((cells) => {
    let tds = '';
    for (let k = 0; k < colCount; k++) tds += `<td>${k < cells.length ? cells[k] : ''}</td>`;
    return `<tr>${tds}</tr>`;
  }).join('');
  return `<div class="table-wrap"><table><thead><tr>${th}</tr></thead><tbody>${tbody}</tbody></table></div>`;
}

function markdown(text) {
  const codeBlocks = [];
  const addCodeBlock = (language, code) => {
    const index = codeBlocks.length;
    const lang = escapeHtml(String(language || '').trim());
    codeBlocks.push(
      `<div class="code-block"><div class="code-block-bar">` +
      `<span class="code-lang">${lang}</span>` +
      `<button type="button" class="code-copy" data-copy-code>复制</button>` +
      `</div><pre><code data-language="${lang}">${code}</code></pre></div>`
    );
    return index;
  };
  let safe = escapeHtml(text)
    // 完整（已配对 ```...````）围栏代码块先提取，避免其内容被后续行内替换误伤。
    .replace(/```([^\n]*)\n([\s\S]*?)```/g, (_, language, code) => `\n@@CODE_${addCodeBlock(language, code)}@@\n`)
    // 流式输出时常见“未封闭的尾部围栏”：只有开栏 ```，尚未等到对应 ```。
    // 之前这种块会被当作普通文本显示（露出 ``` ... 原始标记），导致代码区看起来
    // “流式中断”，要等整个回复结束（done 全量渲染）才突然变成代码框。
    // 这里把它也提取成代码框，随内容增长实时渲染在框内，不再中断。
    .replace(/```([^\n]*)\n([\s\S]*)$/, (_, language, code) => `\n@@CODE_${addCodeBlock(language, code)}@@`);
  const blocks = [];
  let paragraph = [];
  let listType = '';
  let listItems = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    // 段落结束时统一做内联，保证加粗/代码/链接只在单个段落内匹配，
    // 避免一个未闭合 ** 跨过段落到后续内容才闭合导致“吞掉”整段。
    blocks.push(`<p>${markdownInline(paragraph.join('<br>'))}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (!listItems.length) return;
    blocks.push(`<${listType}>${listItems.map((item) => `<li>${markdownInline(item)}</li>`).join('')}</${listType}>`);
    listType = '';
    listItems = [];
  };
  const startListItem = (type, item) => {
    flushParagraph();
    if (listType && listType !== type) flushList();
    listType = type;
    listItems.push(item);
  };

  const lines = safe.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) {
      flushParagraph();
      flushList();
      continue;
    }
    const codeMatch = line.match(/^@@CODE_(\d+)@@$/);
    if (codeMatch) {
      flushParagraph();
      flushList();
      blocks.push(codeBlocks[Number(codeMatch[1])]);
      continue;
    }
    // 表格：只有“表头行 + 紧跟的分隔行”才识别为表格，避免普通含 | 文本误判。
    const table = collectMarkdownTable(lines, i);
    if (table) {
      flushParagraph();
      flushList();
      blocks.push(renderMarkdownTable(table.rows));
      i = table.end - 1;
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      const level = heading[1].length;
      blocks.push(`<h${level}>${markdownInline(heading[2])}</h${level}>`);
      continue;
    }
    if (/^(?:-{3,}|\*{3,}|_{3,})$/.test(line)) {
      flushParagraph();
      flushList();
      blocks.push('<hr>');
      continue;
    }
    const unordered = line.match(/^[-*+]\s+(.+)$/);
    if (unordered) {
      startListItem('ul', unordered[1]);
      continue;
    }
    const ordered = line.match(/^\d{1,3}[.)、]\s+(.+)$/);
    if (ordered) {
      startListItem('ol', ordered[1]);
      continue;
    }
    flushList();
    paragraph.push(line);
  }
  flushParagraph();
  flushList();
  return blocks.join('');
}

function fileUrl(source) {
  const value = String(source || '');
  if (/^https?:\/\//i.test(value) && !/^https?:\/\/(?:127\.0\.0\.1|localhost):8188\//i.test(value)) return value;
  return `/api/file?token=${encodeURIComponent(state.token)}&path=${encodeURIComponent(value)}`;
}

function attachmentThumbPath(attachment) {
  if (attachment.thumb_path) return attachment.thumb_path;
  const p = String(attachment.path || attachment.source || '');
  if (!p || /^https?:\/\//i.test(p)) return '';
  const dot = p.lastIndexOf('.');
  return (dot > 0 ? p.slice(0, dot) : p) + '_thumb.webp';
}

function attachmentThumbUrl(attachment) {
  const thumb = attachmentThumbPath(attachment);
  const source = attachment.path || attachment.source || '';
  return thumb ? fileUrl(thumb) : fileUrl(source);
}

function openImageLightbox(largeUrl) {
  const img = $('#imageLightboxImg');
  const box = $('#imageLightbox');
  if (!img || !box || !largeUrl) return;
  if (!/^(\/api\/file|https?:\/\/)/i.test(largeUrl)) return;
  img.onerror = () => closeImageLightbox();
  img.src = largeUrl;
  box.hidden = false;
}

function closeImageLightbox() {
  const box = $('#imageLightbox');
  if (box) box.hidden = true;
  const img = $('#imageLightboxImg');
  if (img) img.removeAttribute('src');
}

// ---- 大图右键 → 复制图片到剪贴板 ----
// pywebview（WebView2）默认关闭了浏览器右键菜单（AreDefaultContextMenusEnabled 仅 debug 开启），
// 因此在 pywebview 窗口内自绘一个轻量菜单；真实浏览器保留其原生“复制图片”。
function isPywebview() {
  return Boolean(window.pywebview && window.pywebview.api);
}

function ensureImageContextMenu() {
  const menu = $('#imageContextMenu');
  if (menu) return menu;
  const m = document.createElement('div');
  m.className = 'image-context-menu';
  m.id = 'imageContextMenu';
  m.setAttribute('role', 'menu');
  m.setAttribute('aria-label', '图片操作');
  m.innerHTML = '<button type="button" role="menuitem" data-image-context-action="copy">复制图片</button>';
  m.hidden = true;
  document.body.append(m);
  return m;
}

function hideImageContextMenu() {
  const menu = $('#imageContextMenu');
  if (menu) menu.hidden = true;
}

function showImageContextMenu(event) {
  const menu = ensureImageContextMenu();
  menu.hidden = false;
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  menu.style.left = `${Math.max(6, Math.min(event.clientX, window.innerWidth - width - 6))}px`;
  menu.style.top = `${Math.max(6, Math.min(event.clientY, window.innerHeight - height - 6))}px`;
  // 不自动聚焦按钮，避免图片失焦影响后续复制路径。
}

function bytesToBase64(bytes) {
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

// 取当前大图的字节。优先同源 fetch（/api/file 由其自身服务，必然可读）；
// 跨源或 fetch 失败时回退 canvas 转 PNG（跨源且未开 CORS 的图会被污染并抛错）。
async function imageBytesFrom(img) {
  const url = img.currentSrc || img.src;
  if (url) {
    try {
      const resp = await fetch(url);
      if (resp.ok) {
        const blob = await resp.blob();
        const bytes = new Uint8Array(await blob.arrayBuffer());
        if (blob.type && blob.type.startsWith('image/')) {
          return { bytes, mime: blob.type };
        }
      }
    } catch (_) { /* 跨源或网络错误，走 canvas 回退 */ }
  }
  const canvas = document.createElement('canvas');
  canvas.width = img.naturalWidth || img.width;
  canvas.height = img.naturalHeight || img.height;
  if (!canvas.width || !canvas.height) throw new Error('图片尚未加载完成');
  const ctx = canvas.getContext('2d');
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
  const dataUrl = canvas.toDataURL('image/png');
  const b64 = dataUrl.split(',')[1] || '';
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return { bytes, mime: 'image/png' };
}

async function copyLightboxImage() {
  const img = $('#imageLightboxImg');
  if (!img) throw new Error('未找到图片');
  const { bytes, mime } = await imageBytesFrom(img);
  // 1) 原生剪贴板 API：127.0.0.1 / localhost 是安全上下文，WebView2 通常可用。
  if (navigator.clipboard?.write && window.ClipboardItem) {
    try {
      await navigator.clipboard.write([new ClipboardItem({ [mime]: new Blob([bytes], { type: mime }) })]);
      return 'clipboard';
    } catch (_) {
      // WebView 可能未授予剪贴板权限，回退到 Python 桥。
    }
  }
  // 2) pywebview 桥：把图片写入 Windows CF_DIB 剪贴板（桌面端最可靠）。
  if (window.pywebview?.api?.copy_image_to_clipboard) {
    const b64 = bytesToBase64(bytes);
    const result = await window.pywebview.api.copy_image_to_clipboard(b64);
    if (result && result.ok) return 'python';
    throw new Error((result && result.error) || '复制到剪贴板失败');
  }
  throw new Error('当前环境不支持复制图片');
}

async function runImageContextAction(action) {
  try {
    if (action === 'copy') {
      const via = await copyLightboxImage();
      toast(via === 'python' ? '已复制图片到剪贴板' : '已复制图片');
    }
  } catch (error) {
    toast(`复制失败：${error.message}`);
  } finally {
    hideImageContextMenu();
  }
}

// 点击缩略图 → 弹大图；拖拽历史缩略图 → 以"大图 URL"拖动；缩略图 404 → 回退原图。
document.addEventListener('click', (event) => {
  const target = event.target.closest?.('[data-large-url]');
  if (target) openImageLightbox(target.getAttribute('data-large-url'));
});
document.addEventListener('dragstart', (event) => {
  const target = event.target.closest?.('[data-large-url]');
  if (!target) return;
  const url = target.getAttribute('data-large-url');
  if (!url) return;
  try { event.dataTransfer.effectAllowed = 'copy'; } catch (_) { /* ignore */ }
  try { event.dataTransfer.setData('text/uri-list', url); } catch (_) { /* ignore */ }
  try { event.dataTransfer.setData('text/plain', url); } catch (_) { /* ignore */ }
  // 解析大图 URL 里的真实文件路径：拖到输入框可复用该图片。
  let absoluteUrl = url;
  try {
    const parsed = new URL(url, location.href);
    absoluteUrl = parsed.href;
    const filePath = decodeURIComponent(parsed.searchParams.get('path') || '');
    if (filePath) event.dataTransfer.setData('application/x-naiba-file-path', filePath);
  } catch (_) { /* ignore */ }
  // 若已预取到该图字节，作为真实文件加入拖拽（拖到桌面另存、拖进输入框都生效）。
  try {
    const cached = draggedFileCache.get(absoluteUrl);
    if (cached && event.dataTransfer.items?.add) {
      event.dataTransfer.items.add(cached);
      event.dataTransfer.setData('DownloadURL', `${cached.type || 'application/octet-stream'}:${cached.name}:${absoluteUrl}`);
    }
  } catch (_) { /* ignore */ }
});
document.addEventListener('error', (event) => {
  const img = event.target;
  if (!(img && String(img.tagName).toUpperCase() === 'IMG' && img.classList.contains('thumbnail') && !img.dataset.fallback)) return;
  img.dataset.fallback = '1';
  const large = img.getAttribute('data-large-url');
  if (large && img.getAttribute('src') !== large) img.src = large;
}, true);

// 点击缩略图上的「发送到输入框」按钮：把该图加入待发送附件。
// 不依赖 HTML5 拖拽，因此在内嵌 webview 窗口里同样可用。
document.addEventListener('click', (event) => {
  const reuse = event.target.closest?.('[data-reuse-source]');
  if (!reuse) return;
  event.stopPropagation();
  let source = reuse.getAttribute('data-reuse-source') || '';
  const name = reuse.getAttribute('data-reuse-name') || 'image';
  const thumb = reuse.getAttribute('data-reuse-thumb') || '';
  if (!source) return;
  // 若是 /api/file URL，解码出真实文件路径；否则直接使用绝对路径。
  try {
    const parsed = new URL(source, location.href);
    if (parsed.pathname === '/api/file') source = decodeURIComponent(parsed.searchParams.get('path') || source);
  } catch (_) { /* keep source */ }
  if (state.pendingFiles.some((file) => file.path === source)) return;
  const chip = { name: String(source).split(/[\\/]/).pop() || name, path: source, size: 0 };
  if (thumb) chip.thumb_path = thumb;
  state.pendingFiles.push(chip);
  renderPendingFiles();
  const ta = $('#composerTextarea') || document.querySelector('.composer textarea, .composer input');
  if (ta) ta.focus();
});

function mediaMarkup(attachments = []) {
  if (!attachments.length) return '';
  const items = attachments.map((attachment) => {
    const source = attachment.source || attachment.path;
    const lower = `${String(source).toLowerCase().split('?')[0]} ${String(attachment.name || '').toLowerCase()}`;
    const url = fileUrl(source);
    const safeUrl = escapeHtml(url);
    const name = escapeHtml(attachment.name || '生成文件');
    if (/\.(png|jpe?g|webp|gif)$/.test(lower)) {
      const thumbUrl = attachmentThumbUrl(attachment);
      const reusePath = attachment.source || attachment.path || '';
      const reuseThumb = attachment.thumb_path || '';
      return `<span class="media-item"><img class="media-image thumbnail" src="${escapeHtml(thumbUrl)}" alt="${name}" loading="lazy" draggable="true" data-large-url="${safeUrl}"><button class="thumb-reuse" type="button" title="发送到输入框（复用此图）" aria-label="发送到输入框" data-reuse-source="${escapeHtml(reusePath)}" data-reuse-name="${name}" data-reuse-thumb="${escapeHtml(reuseThumb)}">↩</button></span>`;
    }
    if (/\.(mp4|webm|mov|m4v|ogv)(?:\s|$)/.test(lower)) return `<video src="${safeUrl}" controls playsinline preload="metadata"></video>`;
    if (/\.(wav|mp3|m4a|ogg|flac)(?:\s|$)/.test(lower)) return `<audio src="${safeUrl}" controls preload="metadata"></audio>`;
    return `<a class="file-chip" href="${safeUrl}" target="_blank" rel="noreferrer">${name}</a>`;
  }).join('');
  return `<div class="media-grid">${items}</div>`;
}

function toolRunMarkup(run = {}) {
  return `<details class="tool-run">
    <summary>${run.success ? '已执行' : '执行失败'} · ${escapeHtml(run.tool)}${run.reason ? ` · ${escapeHtml(run.reason)}` : ''}</summary>
    <pre>${escapeHtml(JSON.stringify(run.arguments || {}, null, 2))}\n\n${escapeHtml(run.result || '')}</pre>
  </details>`;
}

function toolMarkup(runs = []) {
  if (!runs.length) return '';
  return `<div class="tool-stack">${runs.map((run) => toolRunMarkup(run)).join('')}</div>`;
}

function activityMarkup(activity = []) {
  if (!Array.isArray(activity) || !activity.length) return '';
  // 找出最后一段 reasoning（正式回复的思考），保持展开；其余工具思考折叠。
  let lastReasoningIndex = -1;
  activity.forEach((item, index) => {
    if (item && item.type === 'reasoning') lastReasoningIndex = index;
  });
  let html = '';
  activity.forEach((item, index) => {
    try {
      if (item.type === 'reasoning') html += reasoningMarkup([item.text], index === lastReasoningIndex);
      else if (item.type === 'tool' && item.run) html += toolRunMarkup(item.run);
      else if (item.type === 'prose') html += `<div class="stream-prose">${markdown(item.text)}</div>`;
    } catch (_) { /* 单个条目异常不影响整体 */ }
  });
  return html;
}

function reasoningMarkup(reasoning, finalOpen = false) {
  const list = Array.isArray(reasoning) ? reasoning.filter(Boolean) : (reasoning ? [reasoning] : []);
  if (!list.length) return '';
  // 每次工具调用/思考段单独一行（可折叠）；正式回复的最后一段思考保持展开，不折叠。
  return list.map((text, index) => {
    const clean = String(text || '').trim();
    const preview = clean.replace(/\s+/g, ' ').slice(0, 80);
    const summary = preview ? `思考：${preview}${clean.length > preview.length ? '…' : ''}` : '思考';
    const isFinal = finalOpen && index === list.length - 1;
    const body = `<summary>${escapeHtml(summary)}</summary><div class="reasoning-content">${markdown(clean)}</div></details>`;
    return isFinal
      ? `<details class="reasoning-block" open>${body}`
      : `<details class="reasoning-block tool-reasoning">${body}`;
  }).join('');
}

function usageMarkup(usage) {
  if (!usage || typeof usage !== 'object') return '';
  const input = Number(usage.input_tokens || 0);
  const output = Number(usage.output_tokens || 0);
  const cached = Number(usage.cached_tokens || 0);
  const total = Number(usage.total_tokens || input + output);
  const performance = usage.performance || {};
  const vision = performance.vision || usage.lanes?.vision || {};
  const chat = performance.chat || usage.lanes?.chat || {};
  const visualMs = Number(vision.total_ms || vision.diagnostics?.total_ms || 0);
  const chatMs = Number(chat.total_ms || 0);
  const visionCacheHit = Boolean(vision.cache_hit);
  const requestCount = Number(vision.requests || 0) + Number(usage.requests || 0);
  if (!input && !output && !visualMs && !chatMs) {
    return '<div class="usage-line">Token / 缓存命中率：供应商未返回</div>';
  }
  const rate = input ? Number(usage.cache_hit_rate ?? (cached / input * 100)).toFixed(1) : '0.0';
  const requests = Number(usage.requests || 1);
  const miss = Math.max(0, Number(usage.uncached_tokens ?? (input - cached)));
  const tokenLine = (input || output)
    ? `<div class="usage-line" title="本轮 ${requests} 次模型请求">本轮 ${total.toLocaleString()} tokens · 输入 ${input.toLocaleString()} · 输出 ${output.toLocaleString()} · 缓存命中率 ${rate}%（命中 ${cached.toLocaleString()} / 重算 ${miss.toLocaleString()}）</div>`
    : '';
  const laneLine = (visualMs || chatMs || visionCacheHit)
    ? `<div class="usage-line usage-performance">${visionCacheHit ? '视觉缓存命中' : (visualMs ? `视觉 ${(visualMs / 1000).toFixed(1)}s` : '')}${(visionCacheHit || visualMs) && chatMs ? ' → ' : ''}${chatMs ? `聊天 ${(chatMs / 1000).toFixed(1)}s` : ''} · 共 ${requestCount || requests} 次请求</div>`
    : '';
  const warnings = Array.isArray(performance.warnings) ? performance.warnings : [];
  const warningLine = warnings.map((item) => `<div class="usage-warning">${escapeHtml(item)}</div>`).join('');
  return `${tokenLine}${laneLine}${warningLine}`;
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
    state.contextAtCeiling = false;
    updateContextComposerLock();
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
  const atCeiling = limit > 0 && percent >= 100;
  if (atCeiling !== state.contextAtCeiling) {
    state.contextAtCeiling = atCeiling;
    if (atCeiling) toast('上下文已达到上限，请新建对话后继续。');
  }
  updateContextComposerLock(Boolean(state.chatBusy));
}

function updateContextComposerLock(busy = false) {
  const atCeiling = Boolean(state.contextAtCeiling);
  const input = $('#messageInput');
  const sendBtn = $('#sendButton');
  // Always lock the input at the ceiling so the user cannot draft a new turn.
  if (input) {
    input.disabled = atCeiling;
    input.placeholder = atCeiling ? '上下文已满，请新建对话后继续' : '';
  }
  // During an in-progress run the send button doubles as the stop control, so
  // keep it clickable; otherwise lock it at the ceiling too.
  if (sendBtn) {
    sendBtn.disabled = atCeiling && !busy;
    sendBtn.title = atCeiling ? '上下文已满，请新建对话后继续' : '发送';
  }
}

// Legacy provider context_size: migrated to context_window and kept only for
// compatibility with older configuration readers.
// providerContextSize remains only as a hidden legacy selector marker;
// providerContextWindow is the active provider-scoped control.

function positionContextUsagePopover() {
  const popover = $('#contextUsagePopover');
  const button = $('#contextUsageButton');
  if (!popover || !button || popover.hidden) return;
  const edge = 12;
  const gap = 9;
  const buttonRect = button.getBoundingClientRect();
  const popoverRect = popover.getBoundingClientRect();
  const rightAligned = buttonRect.right - popoverRect.width;
  const maxLeft = Math.max(edge, window.innerWidth - popoverRect.width - edge);
  const left = Math.min(Math.max(edge, rightAligned), maxLeft);
  let top = buttonRect.top - popoverRect.height - gap;
  if (top < edge) {
    top = Math.min(
      buttonRect.bottom + gap,
      Math.max(edge, window.innerHeight - popoverRect.height - edge),
    );
  }
  popover.style.left = `${Math.round(left)}px`;
  popover.style.top = `${Math.round(top)}px`;
}

function toggleContextUsagePopover(event) {
  event.stopPropagation();
  const popover = $('#contextUsagePopover');
  const button = $('#contextUsageButton');
  const open = popover.hidden;
  if (open && popover.parentElement !== document.body) document.body.appendChild(popover);
  popover.hidden = !open;
  button.setAttribute('aria-expanded', String(open));
  if (open) positionContextUsagePopover();
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
  const parts = [];
  const user = skills.filter((s) => s?.source !== 'auto');
  const auto = skills.filter((s) => s?.source === 'auto');
  if (user.length) parts.push(`已启用 Skill：${user.map((s) => escapeHtml(s?.name || s)).join('、')}`);
  if (auto.length) parts.push(`已自动匹配 Skill：${auto.map((s) => escapeHtml(s?.name || s)).join('、')}`);
  return parts.length ? `<div class="skill-usage">${parts.join('<br>')}</div>` : '';
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
  row.__messageMetadata = metadata;
  row.dataset.interjection = String(Boolean(metadata.interjection));
  row.dataset.interjectionGuided = String(Boolean(metadata.interjection_guided));
  row.dataset.interjectionConsumed = String(Boolean(metadata.interjection_consumed));
  if (Array.isArray(metadata.attachments)) {
    metadata.attachments.forEach((attachment) => {
      const source = attachment.source || attachment.path;
      if (source && /\.(png|jpe?g|gif|webp)$/i.test(source)) preloadDraggedFile(source, attachment.name);
    });
  }
  if (message.role === 'user') {
    const actions = message.id ? `<div class="message-actions"><button data-branch-message title="从这条消息分支到新会话继续">分支</button>${metadata.interjection && !metadata.interjection_consumed ? '<button data-delete-message title="删除这条消息">删除</button>' : ''}</div>` : '';
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
      <div class="message-body">
        ${skillMarkup(metadata.skills)}
        ${hideBottomContent ? abortedBadge : ''}
        ${reasoningToolHtml}
        ${temporary ? '<div class="run-activity activity">正在准备</div>' : ''}
        <div class="answer-content" data-raw="" ${hideBottomContent ? 'style="display:none"' : ''}>${temporary ? '' : abortedBadge + markdown(message.content)}</div>
        ${temporary ? '' : sourcesMarkup(metadata.sources)}
        ${mediaMarkup(metadata.attachments)}
        ${temporary ? '' : usageMarkup({ ...(metadata.usage || {}), performance: metadata.performance || metadata.usage?.performance })}
        ${temporary ? '' : `<div class="message-actions"><button data-copy-message>复制</button></div>`}
      </div>`;
  }
  return row;
}

function preloadDraggedFile(source, name = '') {
  const url = new URL(fileUrl(source), location.href).href;
  if (draggedFileCache.has(url)) return;
  fetch(url).then((response) => response.ok ? response.blob() : Promise.reject(new Error('image fetch failed')))
    .then((blob) => draggedFileCache.set(url, new File([blob], name || 'image' + (blob.type ? '.' + blob.type.split('/')[1] : ''), { type: blob.type })))
    .catch(() => {});
}

function isPendingRunGuidance(message) {
  const metadata = message?.metadata || {};
  return message?.role === 'user'
    && Boolean(metadata.interjection)
    && !metadata.interjection_guided
    && !metadata.interjection_consumed
    && !metadata.interjection_stopped;
}

function runGuidanceElement(message) {
  const row = document.createElement('article');
  const metadata = message.metadata || {};
  row.className = 'message-row user run-guidance-card';
  row.dataset.messageId = message.id || '';
  row.dataset.interjection = 'true';
  row.dataset.runGuidance = 'true';
  row.dataset.rawContent = message.content || '';
  row.innerHTML = `<div class="message-body"><span class="run-guidance-icon" aria-hidden="true">≡</span><div class="run-guidance-preview">${escapeHtml(message.content)}</div>
    <div class="run-guidance-actions">
      <button type="button" data-edit-message title="编辑排队消息" aria-label="编辑排队消息">✎</button>
      <button type="button" data-delete-message title="删除排队消息" aria-label="删除排队消息">×</button>
      <button type="button" data-guide-message title="立即发送此消息" aria-label="立即发送此消息">↑</button>
    </div>
  </div>`;
  return row;
}

function renderRunGuidance(messages) {
  const container = $('#runGuidanceList');
  if (!container) return;
  const pending = (messages || []).filter(isPendingRunGuidance);
  container.replaceChildren(...pending.map(runGuidanceElement));
  container.hidden = pending.length === 0;
}

function promoteRunGuidance(messageId) {
  const row = messageId
    ? document.querySelector(`.run-guidance-card[data-message-id="${CSS.escape(String(messageId))}"]`)
    : null;
  if (!row) return;
  row.classList.remove('run-guidance-card');
  delete row.dataset.runGuidance;
  row.querySelector('.run-guidance-actions')?.remove();
  $('#messages').append(row);
  const container = $('#runGuidanceList');
  if (container && !container.children.length) container.hidden = true;
  scrollToBottom();
}

function startEditMessage(row) {
  const editablePendingGuidance = row?.dataset.interjection === 'true'
    && row?.dataset.interjectionGuided !== 'true'
    && row?.dataset.interjectionConsumed !== 'true';
  if (!row || (state.chatRunId && !editablePendingGuidance)) {
    if (state.chatRunId) toast('请先停止当前对话再编辑');
    return;
  }
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

async function confirmEditMessage(row, newText) {
  const text = newText.trim();
  if (!text) {
    toast('内容不能为空');
    return;
  }
  const messageId = row.dataset.messageId;
  if (!messageId || !state.conversationId) return;
  if (state.chatRunId && row.dataset.interjection === 'true'
      && row.dataset.interjectionGuided !== 'true'
      && row.dataset.interjectionConsumed !== 'true') {
    try {
      const result = await api('/api/chat/interject/edit', {
        method: 'POST',
        body: { conversation_id: state.conversationId, run_id: state.chatRunId, message_id: messageId, message: text },
      });
      row.replaceWith(runGuidanceElement(result.message));
    } catch (error) {
      toast(`编辑失败：${error.message}`);
    }
    return;
  }
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
async function branchMessage(row) {
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

function uploadedFileMarkup(files = []) {
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

let stickToBottom = true;

function isNearBottom(threshold = 80) {
  const messages = $('#messages');
  if (!messages) return true;
  return (messages.scrollHeight - messages.scrollTop - messages.clientHeight) < threshold;
}

// 默认滚动：只在用户仍停留在底部（跟随）时才自动滚到最新内容；
// 用户滚轮上滑阅读历史时，后续任何 delta/工具事件都不再把页面强行拉回底部。
function scrollToBottom() {
  if (!stickToBottom) return;
  const messages = $('#messages');
  if (messages) messages.scrollTop = messages.scrollHeight;
}

// 强制滚到底部：用于确实需要展示最新内容的地方（渲染后一次性定位）。
function forceScrollToBottom() {
  const messages = $('#messages');
  if (messages) messages.scrollTop = messages.scrollHeight;
}

function scheduleStreamingMarkdown(element, raw) {
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
function getStreamingProseSegment(row, answer) {
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
function moveBottomProseInline(row, answer) {
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

function renderMessages(messages) {
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
    const visibleMessages = messages.filter((message) => !isPendingRunGuidance(message));
    empty.hidden = visibleMessages.length > 0;
    container.append(empty);
    if (visibleMessages.length) {
      visibleMessages.forEach((message) => container.append(messageElement(message)));
      scrollToBottom();
    }
    renderRunGuidance(messages);
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

function renderNetworkAccess() {
  const access = state.bootstrap || {};
  const configuredHost = String(access.settings?.host || '0.0.0.0');
  const pendingRestart = Boolean(access.lan_restart_required);
  const address = access.lan_enabled && access.lan_url
    ? access.lan_url
    : (pendingRestart || configuredHost === '127.0.0.1' ? '当前仅本机访问' : '手机访问不可用');
  const reason = pendingRestart
    ? '手机访问已启用，请完全退出并重新启动 naiba-chat。'
    : (access.lan_reason || '手机与电脑需连接同一局域网。');
  $('#lanAddress').textContent = address;
  $('#connectionAddress').textContent = access.lan_url || access.local_url || '未检测到可用地址';
  $('#connectionReason').textContent = reason;
  $('#enableLanActions').hidden = access.lan_enabled || pendingRestart || configuredHost === '0.0.0.0';
  const copyButton = $('#copyAddress');
  copyButton.disabled = !access.lan_enabled || !access.lan_url;
  copyButton.title = copyButton.disabled ? reason : '复制手机访问地址';
}

async function enableLanAccess() {
  try {
    const result = await api('/api/settings', { method: 'POST', body: { host: '0.0.0.0' } });
    Object.assign(state.bootstrap.settings, result.settings || {});
    state.bootstrap.lan_restart_required = Boolean(result.restart_required);
    renderNetworkAccess();
    toast('手机访问已启用，请完全退出并重新启动 naiba-chat');
  } catch (error) {
    toast(`启用手机访问失败：${error.message}`);
  }
}

async function initialize() {
  restoreSidebarWidth();
  sidebarScrollToActive = true;
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
  state.workspaces = Array.isArray(state.bootstrap?.workspaces) ? state.bootstrap.workspaces : [];
  renderNetworkAccess();
  populateModels();
  renderAgents();
  renderAgentManager();
  // 恢复模式 Tab 状态
  $$('.mode-tab').forEach(tab => tab.classList.toggle('active', tab.dataset.mode === state.mode));
  populateRuntimeSettings();
  populateVisionSettings();
  populateSearchSettings();
  renderSkills();
  renderProviders();
  renderMcp();
  renderUpdateStatus(state.bootstrap.update || {});
  await loadConversations();
  await loadStarterPrompts();
  await loadTasks();
  startTaskSync();
  startConversationSync();
  startMcpPoll();
  startUpdatePoll();
}

// 后台检查可能启动时才开始（checking 阶段），前端以 30 秒间隔轮询感知结果。
function startUpdatePoll() {
  if (state.updatePollTimer) return;
  state.updatePollTimer = window.setInterval(async () => {
    const status = state.bootstrap.update || {};
    if (['checking', 'downloading', 'restarting'].includes(status.phase)) return;
    try {
      const next = await api('/api/update');
      if (next.checked_at !== status.checked_at || next.phase !== status.phase) {
        state.bootstrap.update = next;
        renderUpdateStatus(next);
      }
    } catch (_) { /* 网络抖动时忽略，下一轮重试 */ }
  }, 30000);
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
    await maybeRecoverRunFromPoll();
  } catch (error) {
    console.debug('[naiba] 任务同步失败:', error.message);
  }
}

// 轮询兜底恢复流：仅在处于"等待恢复/重连冷却已过"且前端无活动流时，
// 探测后端是否仍有当前对话的活跃 Run，若有则 resumeRun 拉回流。
// 只会在 runReconnectAt（>0 表示要恢复）且冷却已过时查询，避免每个轮询周期都打 /api/runs。
async function maybeRecoverRunFromPoll() {
  if (state.cancelRequested) return;
  if (state.abortController) return; // 已有活动流，无需兜底
  if (state.runRecovering) return;
  if (!state.checkRunEligible) return; // 未进入"等恢复"状态就不查询
  if (!state.conversationId) return;
  if (state.runReconnectAt && Date.now() < state.runReconnectAt) return; // 冷却中
  state.runRecovering = true;
  const conversationId = state.conversationId;
  try {
    const result = await api(`/api/runs?conversation_id=${encodeURIComponent(conversationId)}&active_only=1`);
    if (state.conversationId !== conversationId || state.abortController) return;
    const run = (result.runs || [])[0];
    if (run && run.id) {
      console.warn('[naiba] 轮询兜底：检测到活跃 Run，恢复流 run=', run.id);
      state.checkRunEligible = false;
      await resumeRun(run);
    }
    // 后端已无活跃 Run：说明任务其实已完成，静默回到就绪，避免无限查询。
    else {
      state.checkRunEligible = false;
      setConnectionState('connected');
      if (state.chatRunId) {
        state.chatRunId = '';
        state.runConversationId = '';
        state.runSequence = 0;
        stopRunWatchdog();
        clearElapsedStatus();
        setBusy(false);
      }
    }
  } catch (error) {
    // 服务仍不可达：保持等待，冷却窗口由 enterReconnectCoolDown 控制
    console.debug('[naiba] 轮询兜底恢复流失败:', error.message);
    if (!state.runReconnectAt) {
      state.runReconnectAt = Date.now() + RUN_RECONNECT_COOLDOWN;
    }
  } finally {
    state.runRecovering = false;
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
  const mode = conversation?.permission_mode || 'auto';
  return ['confirm', 'auto', 'full'].includes(mode) ? mode : 'auto';
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
  $('#currentVersion').textContent = status.current_commit ? `${current} · ${status.current_commit.slice(0, 7)}` : current;
  const select = $('#updateVersionSelect');
  const releases = Array.isArray(status.releases) ? status.releases : [];
  const previousValue = select.value;
  // 重建版本下拉：仅保留可安装项，当前版本标记为「当前」。
  const options = releases
    .filter((release) => release.installable)
    .map((release) => {
      const option = document.createElement('option');
      option.value = release.tag;
      const label = release.current ? `${release.version}（当前）` : release.version;
      option.textContent = release.published_at ? `${label} · ${release.published_at.slice(0, 10)}` : label;
      return option;
    });
  select.replaceChildren(...options);
  select.disabled = options.length === 0 || ['checking', 'downloading', 'restarting'].includes(status.phase);
  // 保留用户已选版本；否则默认选中最新可安装版本。
  if (previousValue && options.some((option) => option.value === previousValue)) {
    select.value = previousValue;
  } else if (options.length > 0) {
    select.value = options[0].value;
  }
  const selectedTag = select.value;
  const selected = releases.find((release) => release.tag === selectedTag);
  const notes = selected && Array.isArray(selected.release_notes)
    ? selected.release_notes.filter((note) => String(note || '').trim())
    : (selected && selected.release_notes ? [String(selected.release_notes)] : []);
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
    idle: '启动后仅检查更新，不会自动安装；请手动选择版本后点击「立即更新」。',
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
  const pending = status.pending_verification;
  if (pending && pending.pending) {
    $('#updateMessage').textContent = pending.ok
      ? `上次更新已完成并验证通过（${pending.target_version}）。`
      : `上次更新到 ${pending.target_version} 后版本校验失败，请重新检查更新或手动安装。`;
  } else if (status.mode === 'source') {
    $('#updateMessage').textContent = '源码模式不支持一键更新，请在终端中执行 git pull --ff-only origin master。';
  } else if (selected && selected.current) {
    $('#updateMessage').textContent = '当前已安装该版本，无需更新。';
  } else if (status.phase === 'available' && selected) {
    $('#updateMessage').textContent = `将安装 ${selected.version}，完成后程序自动重启。`;
  }
  const canInstall = status.supported && status.mode !== 'source'
    && selected && !selected.current
    && !['downloading', 'restarting'].includes(status.phase);
  $('#installUpdate').hidden = !canInstall;
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
  const select = $('#updateVersionSelect');
  const tag = select.value;
  const status = state.bootstrap.update || {};
  const releases = Array.isArray(status.releases) ? status.releases : [];
  const selected = releases.find((release) => release.tag === tag);
  if (!selected) {
    toast('请先选择要安装的版本');
    return;
  }
  if (!confirm(`确定要安装版本 ${selected.version} 吗？更新完成后程序将自动重启。`)) {
    return;
  }
  button.disabled = true;
  try {
    const newStatus = await api('/api/update/install', { method: 'POST', body: { tag } });
    state.bootstrap.update = newStatus;
    renderUpdateStatus(newStatus);
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

// 会话 Agent：首轮前可下拉选择；首轮固化工具集后只读展示（切换会破坏缓存，需新开对话）。
function applyConversationAgent(conversation) {
  const select = $('#agentSelect');
  if (!select) return;
  const agents = state.bootstrap?.agents || [];
  const agentId = String(conversation?.agent_id || '');
  let agent = agents.find((a) => String(a.id) === agentId);
  const locked = Boolean(conversation?.enabled_tool_ids && conversation.enabled_tool_ids.length);
  if (agent && [...select.options].some((o) => o.value === agent.id)) {
    select.value = agent.id;
  } else {
    const fallback = String(state.bootstrap?.default_agent_id || '');
    if ([...select.options].some((o) => o.value === fallback)) {
      select.value = fallback;
    } else if (select.options.length) {
      select.selectedIndex = 0;
    }
    agent = agents.find((a) => String(a.id) === fallback) || agents[0] || null;
  }
  select.disabled = locked;
  select.title = locked
    ? `当前会话已绑定 Agent「${agent?.name || ''}」并固化工具集，会话内不可切换。如需切换，请让 AI 总结当前对话，复制总结后新开对话。`
    : '选择该会话使用的 Agent（发送首条消息后固化，之后不可切换）。';
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
    renderSidebar();
    renderSkills($('#skillSearch')?.value || '');
    toast('Agent 已切换');
    appendPresetSkillsToComposer(value);
  } catch (error) {
    toast(`切换失败：${error.message}`);
    applyConversationAgent(state.conversations.find((item) => item.id === state.conversationId));
  }
}

async function loadConversations() {
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

function formatRelativeTime(ts) {
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

function currentConversationWorkspaceGroup() {
  const c = state.conversations.find((x) => x.id === state.conversationId);
  return c ? (c.workspace_group || '').trim() : '';
}

// ---- 侧栏虚拟化（懒加载）：只渲染可视范围内的行，滚动时按窗口重绘 ----
let sidebarRowCache = [];
let sidebarOffsetCache = [];
let sidebarTotalH = 0;
let sidebarMetrics = null;
let sidebarScrollToActive = false;
let sidebarScrollRaf = 0;
let sidebarShowAll = new Set(); // 已“展开全部会话”的工作区名集合（默认全部折叠到 5 条）
const SIDE_BUFFER = 240; // 视口上下预渲染缓冲（px）
const SIDE_CONV_LIMIT = 5; // 每个展开工作区默认显示的最新会话数

function sidebarMetricsNow() {
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

function sidebarRowHeight(row) {
  const m = sidebarMetricsNow();
  if (row.type === 'header') return m.header;
  if (row.type === 'newchat') return m.newchat;
  if (row.type === 'showmore' || row.type === 'showless') return m.showmore;
  return m.item;
}

function computeSidebarOffsets(rows) {
  const offsets = new Array(rows.length);
  let y = 0;
  for (let i = 0; i < rows.length; i++) { offsets[i] = y; y += sidebarRowHeight(rows[i]); }
  return { offsets, totalH: y };
}

function sidebarRowAt(offsets, pos) {
  let lo = 0, hi = offsets.length - 1, ans = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (offsets[mid] <= pos) { ans = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return ans;
}

function sidebarRowHtml(row) {
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
    <button class="conversation-settings" title="对话设置" aria-label="${escapeHtml(c.title)} 的设置">⚙</button>
    <button class="conversation-open" title="${escapeHtml(c.title)}">${escapeHtml(c.title)}</button>
    <span class="conversation-time">${escapeHtml(formatRelativeTime(c.updated_at))}</span>
    <button class="delete-conversation" title="删除对话" aria-label="删除对话">删除</button>
  </div>`;
}

function renderSidebarWindow(scrollTop) {
  const tree = $('#sidebarWorkspaceTree');
  if (!tree) return;
  if (!sidebarRowCache.length) {
    tree.innerHTML = '<div class="workspace-empty">暂无对话</div>';
    return;
  }
  const vh = tree.clientHeight || Math.max(240, Math.round(window.innerHeight * 0.4));
  let start = Math.max(0, sidebarRowAt(sidebarOffsetCache, scrollTop - SIDE_BUFFER));
  let end = sidebarRowAt(sidebarOffsetCache, scrollTop + vh + SIDE_BUFFER) + 1;
  if (end < start + 1) end = start + 1;
  end = Math.min(sidebarRowCache.length, end + 1);
  const top = sidebarOffsetCache[start];
  const html = sidebarRowCache.slice(start, end).map(sidebarRowHtml).join('');
  tree.innerHTML = `<div class="sidebar-virtual" style="height:${sidebarTotalH}px"><div class="sidebar-virtual-window" style="top:${top}px">${html}</div></div>`;
}

function sidebarClampWidth(w) {
  return Math.max(170, Math.min(Math.max(170, window.innerWidth * 0.3), w));
}

function restoreSidebarWidth() {
  const saved = parseFloat(localStorage.getItem('naibaChatSidebarW') || '');
  const base = (saved && !isNaN(saved)) ? saved : 272;
  document.documentElement.style.setProperty('--sidebar-w', sidebarClampWidth(base) + 'px');
}

function renderSidebar() {
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
  renderSidebarWindow(st);
  tree.scrollTop = st;
}

function renderComposerWorkspace() {
  const select = $('#composerWorkspaceSelect');
  if (!select) return;
  const current = state.conversations.find((c) => c.id === state.conversationId);
  const currentGroup = current ? (current.workspace_group || '').trim() : '';
  const options = ['', ...(state.workspaces || []).map((w) => w.name)];
  select.innerHTML = options.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name || '未分组')}</option>`).join('');
  select.value = currentGroup;
}

async function onComposerWorkspaceChange(event) {
  const id = state.conversationId;
  if (!id) return;
  const group = event.target.value || '';
  try {
    const updated = await api(`/api/conversations/${id}/settings`, { method: 'POST', body: { workspace_group: group } });
    const index = state.conversations.findIndex((c) => c.id === id);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
    renderSidebar();
    renderComposerWorkspace();
    toast(group ? `已切换到工作区「${group}」` : '已移至未分组');
  } catch (error) {
    toast(`切换工作区失败：${error.message}`);
    renderComposerWorkspace();
  }
}

async function onSidebarTreeClick(event) {
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

async function pick_workspace_directory(initial = '') {
  try {
    return await api('/api/workspace/pick', { method: 'POST', body: { initial } });
  } catch (error) {
    toast(`目录选择失败：${error.message}`);
    return { cancelled: true };
  }
}

async function createWorkspace() {
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

async function deleteWorkspace(name) {
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

async function createConversation(workspaceGroup = '', workspaceDir = '', prefillSkills = false) {
  detachRunSubscription();
  hideChoiceButtons();
  const conversation = await api('/api/conversations', {
    method: 'POST',
    body: {
      interaction_mode: 'craft',
      permission_mode: 'auto',
      web_search_enabled: false,
      deep_reasoning_enabled: false,
      // 新建会话默认沿用上一个会话使用的 Agent；无上一个会话时回退默认 Agent。
      agent_id: (state.conversations.find((c) => String(c.id) === state.conversationId)?.agent_id)
        || state.bootstrap?.default_agent_id || '',
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
  state.lightweightMode = Boolean(Number(conversation.lightweight_mode || 0));
  state.lightweightDisabledFeatures = Array.isArray(conversation.lightweight_disabled_features)
    ? conversation.lightweight_disabled_features : ['tools', 'skills'];
  updateLightweightModeControl();
  renderMessages([]);
  renderPermissionModeSwitch();
  closeSidebar();
  if (prefillSkills) prefillPresetSkillsInComposer(conversation);
  $('#messageInput').focus();
}

async function openConversation(id) {
  if (id !== state.conversationId) {
    detachRunSubscription();
    hideChoiceButtons();
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
  state.lightweightMode = Boolean(Number(conversation.lightweight_mode || 0));
  state.lightweightDisabledFeatures = Array.isArray(conversation.lightweight_disabled_features)
    ? conversation.lightweight_disabled_features : ['tools', 'skills'];
  updateLightweightModeControl();
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
    renderSidebar();
    renderMessages(conversation.messages || []);
    renderPermissionModeSwitch();
    state.webSearchEnabled = Boolean(Number(conversation.web_search_enabled || 0));
    state.deepReasoningEnabled = Boolean(Number(conversation.deep_reasoning_enabled || 0));
    updateDeepReasoningButton();
    state.lightweightMode = Boolean(Number(conversation.lightweight_mode || 0));
    state.lightweightDisabledFeatures = Array.isArray(conversation.lightweight_disabled_features)
      ? conversation.lightweight_disabled_features : ['tools', 'skills'];
    updateLightweightModeControl();
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
  const disabled = Array.isArray(conversation.lightweight_disabled_features)
    ? conversation.lightweight_disabled_features : ['tools', 'skills'];
  $$('input[name="lightweightFeature"]').forEach((input) => {
    input.checked = disabled.includes(input.value);
  });
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
        lightweight_disabled_features: $$('input[name="lightweightFeature"]:checked').map((input) => input.value),
      },
    });
    const index = state.conversations.findIndex((item) => item.id === id);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
    $('#conversationSettingsDialog').close();
    renderSidebar();
    if (id === state.conversationId) {
      state.lightweightDisabledFeatures = updated.lightweight_disabled_features || ['tools', 'skills'];
      updateLightweightModeControl();
    }
    toast('对话设置已保存');
  } catch (error) {
    toast(`保存失败：${error.message}`);
  } finally {
    saveButton.disabled = false;
  }
}

async function clearConversationMessages() {
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

async function clearTerminalTasks() {
  if (!confirm('清理所有已结束、失败、取消或中断的异步任务记录吗？运行中的任务不会受影响。')) return;
  try {
    const result = await api('/api/tasks/clear', { method: 'DELETE' });
    await loadTasks();
    toast(`已清理 ${Number(result.deleted || 0)} 个任务`);
  } catch (error) {
    toast(`清理失败：${error.message}`);
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
  renderSidebar();
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

// 有效启用的 Skill = 仅当前会话 Agent 预设的固定 Skill（不再允许用户自行选择/切换模式）。
function effectiveSkillIds() {
  return [...new Set(currentAgentFixedSkillIds())];
}

function renderSkills(filter = '') {
  if (!state.bootstrap) return;
  const query = filter.trim().toLowerCase();
  const fixed = new Set(currentAgentFixedSkillIds());
  const skills = state.bootstrap.skills.filter((skill) =>
    !query || `${skill.name} ${skill.description} ${skill.ref || ''}`.toLowerCase().includes(query));
  $('#skillList').innerHTML = skills.map((skill) => `
    <button type="button" class="skill-item skill-click" data-skill-insert="${skill.id}">
      <span><b>${escapeHtml(skill.name)}</b>${fixed.has(skill.id) ? '<em>预设</em>' : ''}<p>${escapeHtml(skill.description || '')}</p></span>
      <span class="skill-tag" title="点击插入到输入框">/${escapeHtml(skill.ref || skill.name)}</span>
    </button>`).join('');
  updateSkillSummary();
}

function updateSkillSummary() {
  const fixedCount = currentAgentFixedSkillIds().length;
  $('#skillCount').textContent = `Skill ${state.bootstrap.skills.length}`;
  $('#skillPolicyHint').textContent = '点击某项即在输入框光标处插入 /技能 引用；发送后按“首轮注入 / 后续追加”注入';
  $('#skillsSummary').textContent = `${state.bootstrap.skills.length} 个可用，当前 Agent 预设 ${fixedCount} 个（新建会话自动预填引用）`;
}

function renderProviders() {
  const allProviders = state.bootstrap.model_profiles || state.bootstrap.providers || [];
  const providers = allProviders.filter((provider) => (provider.kind || 'online') === state.providerKindTab);
  $$('[data-provider-kind]').forEach((button) => {
    const active = button.dataset.providerKind === state.providerKindTab;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  const select = $('#providerSelect');
  select.innerHTML = providers.length
    ? providers.map((provider) => `<option value="${provider.id}">${escapeHtml(provider.name)}</option>`).join('')
    : `<option value="">尚未添加${state.providerKindTab === 'local' ? '本地' : '在线'} API</option>`;
  const currentId = $('#providerId').value;
  const current = providers.find((provider) => provider.id === currentId)
    || providers.find((provider) => provider.id === state.bootstrap.settings.provider_id)
    || providers[0];
  showProviderForm(current || {
    kind: state.providerKindTab,
    request_format: state.providerKindTab === 'local' ? 'lm_studio' : 'openai_chat',
  }, { editing: false });
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
  const inferredKind = provider.kind || (['ollama', 'lm_studio', 'llama_cpp', 'unsloth'].includes(provider.request_format) ? 'local' : 'online');
  $('#providerKind').value = inferredKind === 'local' ? '1' : '0';
  $('#providerContextWindow').value = provider.context_window || provider.context_size || '';
  $('#providerMaxOutputTokens').value = provider.max_output_tokens || '';
  $('#providerTemperature').value = provider.temperature ?? '';
  $('#providerReasoningEffort').value = provider.reasoning_effort || 'auto';
  $('#providerSupportsImages').value = provider.supports_images_explicit === true
    ? 'true'
    : (provider.supports_images_explicit === false ? 'false' : 'auto');
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
  updateProviderVisionHint();
  updateUnloadModelButton();
}

function syncProviderKindOptions(previousFormat = '') {
  const local = $('#providerKind').value === '1';
  const allowed = local ? ['lm_studio', 'ollama', 'llama_cpp', 'unsloth'] : ['openai_chat', 'codex_responses', 'gemini', 'claude'];
  const format = $('#providerFormat');
  [...format.options].forEach((option) => { option.hidden = !allowed.includes(option.value); });
  if (!allowed.includes(format.value)) {
    // A legacy llama.cpp endpoint was commonly configured as an online
    // OpenAI-compatible API. Switching its type to local must keep the
    // /v1 protocol instead of silently redirecting it to LM Studio's /api/v1.
    format.value = local && previousFormat === 'openai_chat' ? 'llama_cpp' : allowed[0];
  }
  const hint = $('#providerKindHint');
  if (hint) hint.textContent = local ? '本地 API 可使用 llama.cpp、Unsloth、Ollama 或 LM Studio 服务。' : '在线 API 使用远程模型服务。';
}

function updateProviderFormatGuide() {
  const guide = $('#providerFormatGuide');
  const format = $('#providerFormat').value;
  const guides = {
    ollama: '先启动 Ollama。API URL 通常填写 http://127.0.0.1:11434/v1；API Key 可留空；模型名称可通过 ollama list 查看，然后点击“检查模型”。',
    lm_studio: '先在 LM Studio 的 Developer / Local Server 页面启动服务并加载模型。API URL 通常填写 http://127.0.0.1:1234/v1；API Key 可留空，然后点击“检查模型”。',
    llama_cpp: '先启动 llama.cpp server。API URL 通常填写 http://127.0.0.1:8080/v1；API Key 可留空，然后点击“检查模型”。',
    unsloth: '先启动 Unsloth（桌面版或 unsloth studio）。API URL 通常填写 http://127.0.0.1:8000 或 http://127.0.0.1:8888；API Key 在 Unsloth Settings → API 创建（sk-unsloth-…）；上下文长度由启动参数 unsloth run -c <tokens> 决定。然后点击“检查模型”。',
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
    '#providerSupportsImages',
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
      supports_images: typeof model.supports_images === 'boolean' ? model.supports_images : undefined,
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
  updateProviderVisionHint();
}

function updateProviderVisionHint() {
  const hint = $('#providerVisionHint');
  const choice = $('#providerSupportsImages').value;
  if (choice === 'true') {
    hint.textContent = '已强制设为支持图片；Naiba-chat 会把用户图片直接交给该模型。';
    return;
  }
  if (choice === 'false') {
    hint.textContent = '已强制设为纯文本；用户原图不会发送给该模型。';
    return;
  }
  const capability = state.providerModelCapabilities[$('#providerModel').value];
  if (typeof capability?.supports_images === 'boolean') {
    hint.textContent = `模型目录报告：${capability.supports_images ? '支持图片' : '纯文本'}（仍保持自动检测，不写入强制配置）。`;
    return;
  }
  hint.textContent = '自动检测会优先读取运行端能力；上传图片时才会执行最小图片探针。';
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
  const imageChoice = $('#providerSupportsImages').value;
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
    supports_images: imageChoice === 'auto' ? null : imageChoice === 'true',
  };
}

async function loadProviderModels({ automatic = false } = {}) {
  if (!state.providerEditing) return;
  const values = providerFormValue();
  const localFormat = ['lm_studio', 'ollama', 'llama_cpp', 'unsloth'].includes(values.request_format);
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
  const local = state.providerKindTab === 'local';
  showProviderForm({
    kind: state.providerKindTab,
    request_format: local ? 'lm_studio' : 'openai_chat',
  }, { editing: true, isNew: true });
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
    const sourceLabels = {
      explicit: '手动配置',
      llama_props: 'llama.cpp /props',
      ollama_show: 'Ollama capabilities',
      lm_studio_models: 'LM Studio 模型目录',
      image_probe: '真实图片探针',
      model_name: '模型名推断（未确认）',
    };
    const vision = result.supports_images ? '支持图片' : '纯文本';
    const source = sourceLabels[result.capability_source] || result.capability_source || '未知';
    $('#providerError').textContent = `推理连接成功：${result.response}；视觉：${vision}；来源：${source}`;
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
  if ($('#commandTimeout')) $('#commandTimeout').value = settings.command_timeout;
  if ($('#workspaceDir')) $('#workspaceDir').value = settings.workspace_dir === 'workspace' ? '' : (settings.workspace_dir || '');
  if ($('#resolvedWorkspaceDir')) $('#resolvedWorkspaceDir').textContent = state.bootstrap.resolved_workspace_dir || '-';
  const imaging = settings.imaging || {};
  if ($('#imageUploadOriginal')) $('#imageUploadOriginal').checked = Boolean(imaging.image_upload_original);
  if ($('#imageMaxPixels')) $('#imageMaxPixels').value = Number(imaging.image_max_pixels || 2000000);
  if ($('#thumbnailMaxPixels')) $('#thumbnailMaxPixels').value = Number(imaging.thumbnail_max_pixels || 500000);
  renderImageCompressRow();
  if ($('#imageCacheSize')) $('#imageCacheSize').textContent = formatBytes(Number(state.bootstrap.image_cache_bytes || 0));
  renderWorkspaceControl();
}

function renderImageCompressRow() {
  const row = $('#imageCompressRow');
  if (row) row.hidden = Boolean($('#imageUploadOriginal')?.checked);
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (!value) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let n = value;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

async function cleanImageCache() {
  const btn = $('#cleanImageCache');
  if (!btn) return;
  const prev = btn.textContent;
  btn.disabled = true;
  btn.textContent = '清理中…';
  try {
    const result = await api('/api/imaging/clean', { method: 'POST', body: {} });
    state.bootstrap.image_cache_bytes = Number(result.size || 0);
    $('#imageCacheSize').textContent = formatBytes(Number(result.size || 0));
    toast(`已清理 ${formatBytes(Number(result.freed || 0))}（删除 ${Number(result.removed || 0)} 个文件）`);
  } catch (error) {
    toast(`清理失败：${error.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
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

function workspaceEntryMarkup(entry) {
  const icon = entry.kind === 'directory' ? '▸' : '·';
  return `<button type="button" class="workspace-entry ${entry.kind}" data-workspace-path="${escapeHtml(entry.path)}" data-workspace-kind="${entry.kind}"><span class="workspace-entry-icon">${icon}</span><span class="workspace-entry-name">${escapeHtml(entry.name)}</span>${entry.kind === 'file' && entry.size != null ? `<small>${Number(entry.size).toLocaleString()} B</small>` : ''}</button>`;
}

async function loadWorkspaceTree(path = '') {
  const tree = $('#workspaceTree');
  if (!tree) return;
  tree.innerHTML = '<p class="activity">正在读取工作区…</p>';
  try {
    const result = await api('/api/workspace/browse?path=' + encodeURIComponent(path || ''));
    state.workspaceBrowsePath = result.path || result.root || '';
    $('#workspaceTreePath').textContent = state.workspaceBrowsePath || '-';
    $('#workspaceTreeTitle').textContent = (state.workspaceBrowsePath.split(/[\\/]/).filter(Boolean).pop() || '当前工作区');
    $('#workspaceUp').disabled = !result.parent;
    tree.innerHTML = result.entries?.length ? result.entries.map(workspaceEntryMarkup).join('') : '<p class="activity">此目录为空</p>';
    $$('#workspaceTree .workspace-entry').forEach((button) => button.addEventListener('dblclick', () => {
      if (button.dataset.workspaceKind === 'directory') loadWorkspaceTree(button.dataset.workspacePath);
    }));
    $$('#workspaceTree .workspace-entry').forEach((button) => button.addEventListener('click', () => {
      if (button.dataset.workspaceKind === 'directory') loadWorkspaceTree(button.dataset.workspacePath);
      else { $('#workspaceDialogInput').value = result.root || ''; toast(`已选中文件：${button.querySelector('.workspace-entry-name')?.textContent || ''}`); }
    }));
  } catch (error) { tree.innerHTML = `<p class="form-error">${escapeHtml(error.message)}</p>`; }
}

async function loadMcpServers() {
  const data = await api('/api/mcp');
  state.bootstrap.mcp_servers = data.servers || [];
  renderMcp();
  return state.bootstrap.mcp_servers;
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
  const timeout = $('#visionTimeout'); if (timeout) timeout.value = vision.timeout_ms || 180000;
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
  // web_search 可用性诊断：是否已配置端点 + 是否为当前会话工具集可用（只读提示）
  const active = profiles.find((profile) => profile.id === target) || profiles[0] || null;
  const endpointConfigured = Boolean(active?.endpoint?.trim());
  const status = $('#searchAvailability');
  if (status) {
    status.textContent = endpointConfigured
      ? `web_search 可用性：端点已配置 ✓（${(active.endpoint || '').slice(0, 48)}）。只要 Agent 工具集包含 web_search，模型即可调用。`
      : `web_search 可用性：端点未配置 ✗ —— web_search 工具不会出现在工具清单中。请填写“端点 URL”并点击“测试搜索连接”。`;
  }
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
      timeout_ms: Number($('#visionTimeout')?.value || 180000),
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

async function testVisionCapability(probe) {
  const el = $('#visionTestResult');
  if (el) el.textContent = '测试中…';
  try {
    const result = await api('/api/vision/test', {
      method: 'POST',
      body: { provider_model_key: $('#visionProvider')?.value || '', probe },
    });
    if (el) {
      const label = '视觉识别';
      const errors = {
        connection: '服务连接失败',
        text_inference: '文本推理失败',
        image_load: '图片加载失败',
        vision_capability: '视觉能力不可用',
        unknown: '未知错误',
      };
      el.textContent = result.ok
        ? `${label}可用（延迟 ${result.latency_ms ?? '?'}ms，${result.backend || result.model || ''}）`
        : `${label}不可用 [${errors[result.error_kind] || errors.unknown}]：${result.reason || ''}${result.hint ? `；${result.hint}` : ''}`;
    }
  } catch (error) {
    if (el) el.textContent = `测试失败：${error.message}`;
  }
}

async function testVisionConnection() {
  return testVisionCapability('vision');
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
        <b>${escapeHtml(agent.name)}${agent.id === defaultId ? '<em>默认</em>' : ''}${agent.built_in ? '<em>内置</em>' : ''}</b>
        <small>${agent.skill_ids?.length ? `${agent.skill_ids.length} 个固定 Skill` : '无固定 Skill'}</small>
        ${agent.system_prompt ? `<p>${escapeHtml(agent.system_prompt)}</p>` : ''}
      </div>
      <div class="agent-item-actions">
        <button class="control-button" data-agent-edit="${escapeHtml(agent.id)}" type="button">编辑</button>
        ${agent.built_in ? '' : `<button class="danger-button" data-agent-delete="${escapeHtml(agent.id)}" type="button">删除</button>`}
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
  state.agentFormIsNew = !agent;
  state.agentFormToolScope = agent?.tool_scope ? [...agent.tool_scope] : [];
  renderAgentSkillPicker();
  renderAgentToolPicker();
  $('#agentError').textContent = '';
  $('#addAgent').hidden = true;
  $('#agentForm').hidden = false;
  $('#agentName').focus();
}

// Agent 设置：工具选择的联动规则。创建者工具依赖其查询工具（与后端
// JOB_CREATOR_TOOL_DEPS / 依赖闭包保持一致）。选中创建者时自动带上查询工具；
// 取消某个查询工具时，若仍有选中的创建者依赖它，则同步取消该创建者，保证
// “创建者被允许 ⇔ 其描述里让你查询的工具也被允许”的 invariant 不被打破。
const AGENT_TOOL_DEP_RULES = {
  run_in_background: ['job_output', 'job_status', 'job_wait', 'job_kill'],
  subagent: ['job_output'],
  comfyui_batch: ['job_output', 'job_status', 'job_wait'],
};

function applyAgentToolDependency(scope, changedTool, checked) {
  const result = new Set(scope);
  if (checked) {
    result.add(changedTool);
    const deps = AGENT_TOOL_DEP_RULES[changedTool];
    if (deps) deps.forEach((dep) => result.add(dep));
  } else {
    result.delete(changedTool);
    // 取消的若是某创建者必需的查询工具，则把这些创建者也一并取消。
    for (const [creator, deps] of Object.entries(AGENT_TOOL_DEP_RULES)) {
      if (deps.includes(changedTool) && result.has(creator)) result.delete(creator);
    }
  }
  return [...result];
}

function updateGroupSelectAll(groupEl) {
  if (!groupEl) return;
  const all = groupEl.querySelector('input.group-select-all');
  if (!all) return;
  const toolCbs = [...groupEl.querySelectorAll('.permission-grid input[type="checkbox"]')];
  const selected = toolCbs.filter((cb) => state.agentFormToolScope.includes(cb.value));
  all.checked = toolCbs.length > 0 && selected.length === toolCbs.length;
  // 半选态：该分类下只有部分工具被勾选。
  all.indeterminate = toolCbs.length > 0 && selected.length > 0 && selected.length < toolCbs.length;
}

function syncAgentToolCheckboxes(list) {
  if (!list) return;
  list.querySelectorAll('.permission-grid input[type="checkbox"]').forEach((cb) => {
    cb.checked = state.agentFormToolScope.includes(cb.value);
  });
  // 同步各分类的“全选”框状态（含半选）。
  list.querySelectorAll('.agent-tool-group').forEach(updateGroupSelectAll);
}

async function renderAgentToolPicker() {
  const list = $('#agentToolScope');
  if (!list) return;
  if (!state.toolCatalog) {
    try {
      const data = await api('/api/tool_catalog', { method: 'GET' });
      state.toolCatalog = data.tools || [];
    } catch (error) {
      state.toolCatalog = [];
    }
  }
  const catalog = state.toolCatalog || [];
  // 仍未选择时：新 Agent 用后端默认选中集；旧 Agent（无 tool_scope=不限制）默认全选。
  if (!state.agentFormToolScope.length) {
    state.agentFormToolScope = state.agentFormIsNew
      ? catalog.filter((t) => t.default_selected).map((t) => t.name)
      : catalog.map((t) => t.name);
  }
  // 初始加载也应用依赖联动：创建者被选中时自动带上其查询工具，
  // 保证显示的勾选状态与运行时会实际放行的 allowed_tools 一致（后端也有同款依赖闭包）。
  const depAdds = new Set();
  for (const [creator, deps] of Object.entries(AGENT_TOOL_DEP_RULES)) {
    if (state.agentFormToolScope.includes(creator)) deps.forEach((dep) => depAdds.add(dep));
  }
  if (depAdds.size) {
    state.agentFormToolScope = [...new Set([...state.agentFormToolScope, ...depAdds])];
  }
  list.innerHTML = '';
  const groups = {};
  for (const tool of catalog) {
    (groups[tool.group] = groups[tool.group] || []).push(tool);
  }
  for (const [group, tools] of Object.entries(groups)) {
    const groupEl = document.createElement('div');
    groupEl.className = 'agent-tool-group';
    const head = document.createElement('div');
    head.className = 'agent-tool-group-head';
    const headTitle = document.createElement('span');
    headTitle.textContent = group;
    head.append(headTitle);
    // 分类级“全选”勾选框：紧跟分类名右侧，点击一次勾选/取消该分类所有工具（沿用依赖联动）。
    const allCb = document.createElement('input');
    allCb.type = 'checkbox';
    allCb.className = 'group-select-all';
    allCb.setAttribute('data-group', group);
    allCb.title = `全选/取消全选「${group}」分类下的所有工具`;
    head.append(allCb);
    allCb.addEventListener('change', () => {
      let scope = state.agentFormToolScope;
      for (const tool of tools) {
        scope = applyAgentToolDependency(scope, tool.name, allCb.checked);
      }
      state.agentFormToolScope = scope;
      syncAgentToolCheckboxes(list);
    });
    groupEl.append(head);
    const grid = document.createElement('div');
    grid.className = 'permission-grid';
    for (const tool of tools) {
      const label = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.value = tool.name;
      cb.checked = state.agentFormToolScope.includes(tool.name);
      if (tool.model_target === 'vision') {
        cb.title = '针对支持看图的视觉模型（多模态大脑）：直接读取图片。';
      } else if (tool.model_target === 'text') {
        cb.title = '针对文本模型：通过视觉车道解读图片。';
      }
      cb.addEventListener('change', (e) => {
        state.agentFormToolScope = applyAgentToolDependency(
          state.agentFormToolScope, tool.name, e.target.checked,
        );
        syncAgentToolCheckboxes(list);
      });
      const span = document.createElement('span');
      const b = document.createElement('b');
      b.textContent = tool.name;
      if (AGENT_TOOL_DEP_RULES[tool.name]) {
        b.title = '选中后会自动带上其依赖的查询工具（job_output/job_status/job_wait/job_kill 等）。';
      }
      const small = document.createElement('small');
      small.textContent = tool.description || '';
      span.append(b, small);
      label.append(cb, span);
      grid.append(label);
    }
    groupEl.append(grid);
    list.append(groupEl);
  }
  // 初始渲染后同步一次，让各分类“全选”框进入正确的勾选/半选状态。
  syncAgentToolCheckboxes(list);
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
    tool_scope: state.agentFormToolScope,
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
    command_timeout: Number($('#commandTimeout')?.value || 120),
    workspace_dir: $('#workspaceDir')?.value.trim() || '',
    imaging: {
      image_upload_original: Boolean($('#imageUploadOriginal')?.checked),
      image_max_pixels: Number($('#imageMaxPixels')?.value || 2000000),
      thumbnail_max_pixels: Number($('#thumbnailMaxPixels')?.value || 500000),
    },
  };
  const result = await api('/api/settings', { method: 'POST', body: payload });
  Object.assign(state.bootstrap.settings, result.settings);
  state.bootstrap.resolved_workspace_dir = result.resolved_workspace_dir || state.bootstrap.resolved_workspace_dir;
  if ($('#resolvedWorkspaceDir')) $('#resolvedWorkspaceDir').textContent = state.bootstrap.resolved_workspace_dir || '-';
  if (result.image_cache_bytes !== undefined) {
    state.bootstrap.image_cache_bytes = result.image_cache_bytes;
    if ($('#imageCacheSize')) $('#imageCacheSize').textContent = formatBytes(Number(result.image_cache_bytes || 0));
  }
  renderWorkspaceControl();
  toast('运行参数已保存');
}

async function saveWorkspaceSettings() {
  const value = $('#workspaceDialogInput')?.value.trim() || '';
  try {
    const result = await api('/api/settings', { method: 'POST', body: { workspace_dir: value } });
    Object.assign(state.bootstrap.settings, result.settings);
    state.bootstrap.resolved_workspace_dir = result.resolved_workspace_dir || state.bootstrap.resolved_workspace_dir;
    state.workspaceBrowsePath = state.bootstrap.resolved_workspace_dir;
    if ($('#workspaceDir')) $('#workspaceDir').value = value;
    renderWorkspaceControl();
    if ($('#workspaceDialog')) $('#workspaceDialog').close();
    toast('工作区已保存');
  } catch (error) {
    toast(`工作区保存失败：${error.message}`);
  }
}

async function pickWorkspace(targetId = 'workspaceDialogInput') {
  try {
    const current = String($(targetId)?.value || '');
    const result = await api('/api/workspace/pick', { method: 'POST', body: { initial: current } });
    if (result.cancelled) return;
    const input = $('#' + targetId);
    if (input) input.value = result.path || '';
    if (targetId === 'workspaceDir' && $('#resolvedWorkspaceDir')) {
      $('#resolvedWorkspaceDir').textContent = result.resolved || '-';
    }
  } catch (error) { toast(`目录选择失败：${error.message}`); }
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
  if (server.connected) return { text: '已就绪', color: '#3ecf8e' };
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
    const remove = document.createElement('button');
    remove.className = 'control-button mcp-remove';
    remove.type = 'button';
    remove.textContent = '删除';
    remove.addEventListener('click', () => removeMcpServer(server.id));
    actions.append(test, reconnect, remove);
    item.append(actions);
  });
}

async function mcpAction(serverId, action) {
  try {
    const result = await api('/api/mcp/' + action, { method: 'POST', body: { server_id: serverId } });
    const server = state.bootstrap.mcp_servers.find((item) => item.id === serverId);
    if (server) Object.assign(server, result);
    renderMcp();
    if (action === 'test') {
      const parts = ['MCP 测试完成'];
      if (result.connected !== undefined) parts.push(result.connected ? '已就绪' : '未连接');
      if (result.comfyui_reachable !== undefined) parts.push(result.comfyui_reachable ? 'ComfyUI 可达' : 'ComfyUI 不可达');
      if (result.error) parts.push('错误：' + result.error);
      toast(parts.join(' · '));
    } else {
      toast('MCP 已重新连接');
    }
  } catch (error) {
    toast('MCP 操作失败：' + error.message);
  }
}

async function removeMcpServer(serverId) {
  if (!confirm(`确定删除 MCP 服务「${serverId}」？`)) return;
  try {
    await api('/api/mcp/remove', { method: 'POST', body: { server_id: serverId } });
    state.bootstrap.mcp_servers = (state.bootstrap.mcp_servers || []).filter((item) => item.id !== serverId);
    renderMcp();
    toast(`MCP 服务「${serverId}」已删除`);
  } catch (error) {
    toast('删除 MCP 服务失败：' + error.message);
  }
}

async function saveMcpServer() {
  const id = $('#mcpNewId').value.trim();
  const command = $('#mcpNewCommand').value.trim();
  if (!id) { toast('请填写服务 ID'); return; }
  if (!command) { toast('请填写命令（command）'); return; }
  const env = {};
  const comfyBin = $('#mcpNewComfyBin').value.trim();
  if (comfyBin) env.COMFY_BIN = comfyBin;
  const extraRaw = $('#mcpNewEnvJson').value.trim();
  if (extraRaw) {
    let extra;
    try { extra = JSON.parse(extraRaw); }
    catch (_e) { toast('环境变量 JSON 格式不正确'); return; }
    if (!extra || typeof extra !== 'object' || Array.isArray(extra)) { toast('环境变量 JSON 必须是对象'); return; }
    Object.assign(env, extra);
  }
  try {
    await api('/api/mcp/register', { method: 'POST', body: { id, command, args: [], env, enabled: true } });
    $('#mcpNewId').value = '';
    $('#mcpNewCommand').value = '';
    $('#mcpNewComfyBin').value = '';
    $('#mcpNewEnvJson').value = '';
    $('#mcpAddForm').open = false;
    state.bootstrap.mcp_servers = await loadMcpServers();
    renderMcp();
    toast(`MCP 服务「${id}」已注册`);
  } catch (error) {
    toast('注册 MCP 服务失败：' + error.message);
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
      if (!cur) {
        // 后端出现了本地快照中不存在的 MCP 服务（例如对话内 agent 刚注册的）。
        // light 接口不含工具明细，升级为全量刷新以完整展示。
        await loadMcpServers();
        return;
      }
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
  $('#pendingFiles').innerHTML = state.pendingFiles.map((file, index) => {
    const isImage = /\.(png|jpe?g|webp|gif)$/i.test(file.name || '');
    const thumbUrl = attachmentThumbUrl(file);
    const image = isImage
      ? `<img class="thumbnail" src="${escapeHtml(thumbUrl)}" alt="" draggable="false" data-large-url="${escapeHtml(fileUrl(file.path))}">`
      : '';
    return `<span class="file-chip">${file.uploading ? '上传中 · ' : ''}${image}${escapeHtml(file.name)}<button data-remove-file="${index}" title="移除">×</button></span>`;
  }).join('');
}

function clearRunReconnectTimers() {
  state.runReconnectTimers.forEach((timer) => window.clearTimeout(timer));
  state.runReconnectTimers.clear();
}

function detachRunConnection() {
  state.abortController?.abort();
  clearVisionProgress();
  stopRunWatchdog();
  clearElapsedStatus();
  state.runWaitShown = false;
  state.runWaitPrevText = '';
  state.runGeneration += 1;
  state.abortController = null;
}

function detachRunSubscription() {
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
const RUN_WATCHDOG_INTERVAL = 5000;          // 看门狗扫描周期
const RUN_WATCHDOG_IDLE = 45000;             // 超 45s 无数据 → 判定为“可能是死流”（>3 次 heartbeat）
const RUN_WATCHDOG_PROBE_MAX = 2;            // 连续判定空闲超过该次数才真正探针动作
const RUN_RECONNECT_BASE = 500;              // 退避基准 ms
const RUN_RECONNECT_MAX = 10000;             // 退避上限 ms
const RUN_RECONNECT_ATTEMPTS = 3;            // 自动重连上限，超限交还轮询兜底
const RUN_RECONNECT_COOLDOWN = 15000;        // “服死”后的保守重连冷却 ms，防风暴
const RUN_STREAM_OPEN_TIMEOUT = 15000;       // 建流（流式 fetch 打开）超时护栏 ms
const RUN_WAIT_STATUS_IDLE = 6000;           // 无任何新进展字节超过此阈值 → 显示“等待中 · 已等待 X 秒”

function backoffDelay(attempt) {
  const cap = Math.min(RUN_RECONNECT_MAX, RUN_RECONNECT_BASE * 2 ** Math.max(0, attempt - 1));
  return Math.min(cap, cap / 2 + Math.random() * (cap / 2));
}

// 校验当前仍处于“给定代际的连接所对应的活动流”。
function isRunGenerationActive(generation, controller = null) {
  if (state.cancelRequested) return false;
  if (state.runGeneration !== generation) return false;
  if (controller && state.abortController !== controller) return false;
  return true;
}

// 带超时的 fetch，避免建流永久挂起（carrier 不 fire onOpen 也不 return）。
async function fetchRunEvents(runId, controller) {
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
function setConnectionState(next) {
  if (state.connectionState === next) return;
  state.connectionState = next;
}

// 显示 “{base} · 已等待 X 秒” 到 #runtimeStatus，每秒刷新；先清除旧计时。
function showElapsedStatus(base) {
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

function clearElapsedStatus() {
  if (state.elapsedTimer) window.clearInterval(state.elapsedTimer);
  state.elapsedTimer = null;
  state.elapsedBase = '';
}

function stopRunWatchdog() {
  if (state.runWatchdogTimer) window.clearInterval(state.runWatchdogTimer);
  state.runWatchdogTimer = null;
  if (state.runWaitTimer) window.clearInterval(state.runWaitTimer);
  state.runWaitTimer = null;
  state.runProbeMisses = 0;
}

function startRunWatchdog() {
  stopRunWatchdog();
  state.runWatchdogTimer = window.setInterval(runWatchdogTick, RUN_WATCHDOG_INTERVAL);
  // 轻量等待计时：每秒检查“长时间无新进展”，用于显示“等待中 · 已等待 X 秒”。
  state.runWaitTimer = window.setInterval(runWaitTick, 1000);
}

// 无进展等待提示：run 活跃但长时间没有真实内容进展（delta/reasoning/tool/status）
// 时显示“等待中 · 已等待 X 秒”，让用户明白“还在工作而非卡死”。
// 用 runContentActivityAt（不含 heartbeat）作为依据，后台心跳不会重置计数；
// 一有真实进展事件，runContentActivityAt 被刷新，本逻辑自动恢复展示前文本。
function runWaitTick() {
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
function runWatchdogTick() {
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

async function probeAndRecoverRun() {
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

function enterReconnectCoolDown(showTimer = true) {
  setConnectionState('reconnecting');
  state.runReconnectAt = Date.now() + RUN_RECONNECT_COOLDOWN;
  if (showTimer) showElapsedStatus('重连中…');
}

// 断线自动重连：非 AbortError 且流仍当前时，指数退避 + 抖动后重建流。
function scheduleRunReconnect(run, controller, generation) {
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

function clearStreamingAnswer(answer) {
  if (!answer) return;
  answer.dataset.raw = '';
  answer.dataset.renderScheduled = '0';
  answer.replaceChildren();
}

function createStreamingReasoningBlock(answer) {
  const block = document.createElement('details');
  block.className = 'reasoning-block';
  block.open = true;
  block.dataset.streaming = 'true';
  block.dataset.active = 'true';
  block.innerHTML = '<summary>Thinking</summary><div class="reasoning-content"></div>';
  answer.before(block);
  return block;
}

function collapseToolReasoningBlock() {
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

function createRunRow(run) {
  const row = messageElement({ role: 'assistant', content: '' }, true);
  row.dataset.runId = String(run.id || '');
  row.dataset.runKind = String(run.kind || 'chat');
  row.dataset.lightweightMode = String(state.lightweightMode);
  $('#messages').append(row);
  state.runRow = row;
  scrollToBottom();
  return row;
}

async function consumeRunStream(response, row, conversationId, runId, controller, generation = state.runGeneration) {
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

async function finishRunSubscription(conversationId, controller) {
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

async function resumeRun(run, options = {}) {
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

async function resumeConversationRun(conversationId) {
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

async function sendChatMessage(textOverride = '') {
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
    await sendRunInterjection(text);
    return;
  }
  // 用户新发起一轮：恢复跟随，让新答复从底部开始流式显示。
  stickToBottom = true;
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

async function sendRunInterjection(text) {
  const input = $('#messageInput');
  const runId = state.chatRunId;
  const conversationId = state.conversationId;
  if (!runId || !conversationId) return;
  if (state.pendingFiles.some((file) => file.uploading)) {
    toast('请等待文件上传完成');
    return;
  }
  const attachments = state.pendingFiles.map(({ name, path, size, thumb_path }) => ({ name, path, size, thumb_path }));
  state.pendingFiles = [];
  renderPendingFiles();
  input.value = '';
  resizeTextarea();
  renderInputMirror();
  hideSkillPopup();
  try {
    const result = await api('/api/chat/interject', {
      method: 'POST',
      body: { conversation_id: conversationId, run_id: runId, message: text, attachments },
    });
    const message = result.message || { role: 'user', content: text, metadata: { attachments, interjection: true } };
    const container = $('#runGuidanceList');
    container.append(runGuidanceElement(message));
    container.hidden = false;
    toast('已发送，可删除、编辑或引导当前任务');
    return message;
  } catch (error) {
    toast(`消息发送失败：${error.message}`);
    return null;
  }
}

async function guideAllQueuedMessages() {
  const input = $('#messageInput');
  const text = input.value.trim();
  if (text) await sendRunInterjection(text);
  const rows = [...document.querySelectorAll('.run-guidance-card')];
  for (const row of rows) {
    if (!state.chatRunId) break;
    await guideMessage(row);
  }
}

async function guideMessage(row) {
  const messageId = row?.dataset.messageId;
  if (!messageId || !state.conversationId || !state.chatRunId) return;
  const button = row.querySelector('[data-guide-message]');
  if (button) button.disabled = true;
  try {
    const result = await api('/api/chat/interject/guide', {
      method: 'POST',
      body: { conversation_id: state.conversationId, run_id: state.chatRunId, message_id: messageId },
    });
    promoteRunGuidance(result.message?.id || messageId);
  } catch (error) {
    if (button) button.disabled = false;
    toast(`引导失败：${error.message}`);
  }
}

async function deleteMessage(row) {
  const messageId = row?.dataset.messageId;
  if (!messageId || !state.conversationId) return;
  if (state.chatRunId) {
    try {
      await api('/api/chat/interject/delete', {
        method: 'POST',
        body: { conversation_id: state.conversationId, run_id: state.chatRunId, message_id: messageId },
      });
      row.remove();
      toast('已删除待引导消息');
    } catch (error) {
      toast(`删除失败：${error.message}`);
    }
    return;
  }
  try {
    await api('/api/messages/edit', {
      method: 'POST', body: { conversation_id: state.conversationId, message_id: messageId },
    });
    await openConversation(state.conversationId);
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

const SKILL_INSTALL_PRESET =
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

async function startSkillInstall() {
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
async function loadStarterPrompts() {
  try {
    const r = await api('/api/starter-prompts');
    state.customPrompts = Array.isArray(r.prompts) ? r.prompts : [];
    renderStarterPrompts();
  } catch (error) {
    state.customPrompts = [];
  }
}

function renderStarterPrompts() {
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
    main.textContent = p.title || '自定义指令';
    main.addEventListener('click', () => sendMessage(p.text));
    const edit = document.createElement('button');
    edit.type = 'button';
    edit.className = 'starter-edit';
    edit.title = '编辑此指令';
    edit.textContent = '✎';
    edit.addEventListener('click', (e) => { e.stopPropagation(); openStarterPromptDialog(i); });
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'starter-del';
    del.title = '删除此指令';
    del.textContent = '×';
    del.addEventListener('click', (e) => { e.stopPropagation(); removeStarterPrompt(i); });
    wrap.appendChild(main);
    wrap.appendChild(edit);
    wrap.appendChild(del);
    grid.insertBefore(wrap, addBtn);
  });
}

function openStarterPromptDialog(index = -1) {
  state.editingStarterPrompt = index;
  const p = (index >= 0 ? state.customPrompts[index] : null) || {};
  $('#starterPromptTitle').value = p.title || '';
  $('#starterPromptText').value = p.text || '';
  $('#starterPromptDialog').showModal();
  $('#starterPromptTitle').focus();
}

async function saveStarterPrompt() {
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

async function removeStarterPrompt(index) {
  try {
    const r = await api(`/api/starter-prompts/${index}`, { method: 'DELETE' });
    state.customPrompts = r.prompts || [];
    renderStarterPrompts();
    toast('已删除自定义指令');
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

const SKILL_EDIT_PRESET =
  '用户希望编辑本应用内一个已安装的 Skill。本会话已为你启用 inspect_installed_skill（以及读取/编辑/写入文件）工具。'
  + '请按以下流程执行，并【先等待用户指定要编辑哪个 Skill】：\n'
  + '1. 等待用户给出目标 Skill（支持名称或 id）。\n'
  + '2. 调用 inspect_installed_skill{skill: <名称或id>} 拿到该 Skill 的 path（SKILL.md）与 root（所在目录）。\n'
  + '3. 用 read_file 读取 SKILL.md 及其相关脚本/资源，向用户概述当前内容。\n'
  + '4. 按用户要求，用 edit_file/write_file 修改 SKILL.md、描述、脚本等；修改前可先与用户确认改动点，改完说明改了什么。\n'
  + '5. 提醒用户：改动会持久化到该 Skill 文件；切换/重开会话或重新引用（/技能名）后生效。\n'
  + '6. 若用户给的 Skill 不存在（inspect_installed_skill 返回未找到），向用户说明可用的 Skill，不要凭空编造。';

async function startSkillEdit() {
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

async function sendMessage(textOverride = '') {
  await sendChatMessage(textOverride);
}

function updateDeepReasoningButton() {
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

async function toggleDeepReasoning() {
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

function updateLightweightModeControl() {
  const toggle = $('#lightweightModeToggle');
  const attach = $('#attachButton');
  if (toggle) {
    toggle.checked = state.lightweightMode;
    toggle.disabled = Boolean(state.chatRunId || state.abortController);
  }
  if (attach) attach.disabled = false;
  updateDeepReasoningButton();
}

async function toggleLightweightMode() {
  if (!state.conversationId) await createConversation();
  if (state.chatRunId || state.abortController) return;
  const previous = state.lightweightMode;
  state.lightweightMode = !previous;
  updateLightweightModeControl();
  try {
    const updated = await api(`/api/conversations/${state.conversationId}/settings`, {
      method: 'POST',
      body: {
        lightweight_mode: state.lightweightMode,
      },
    });
    const index = state.conversations.findIndex((item) => item.id === state.conversationId);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
    toast(state.lightweightMode ? '轻量对话已开启（本对话）' : '轻量对话已关闭（本对话）');
  } catch (error) {
    state.lightweightMode = previous;
    updateLightweightModeControl();
    toast(`轻量对话设置保存失败：${error.message}`);
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
  if (event.type === 'debug_cache') {
    // 缓存诊断（NAIBA_DEBUG_CACHE=1 时由后端推送）：逐条 [索引:角色:字节数:哈希]
    console.groupCollapsed(`[CACHE] ${event.label || ''}`);
    (event.lines || []).forEach((line) => console.log(line));
    console.groupEnd();
    window.__CACHE_DEBUG__ ??= [];
    window.__CACHE_DEBUG__.push({ label: event.label || '', lines: event.lines || [] });
    return;
  }
  if (event.type === 'run_started') {
    state.chatRunId = String(event.run_id || '');
    state.runConversationId = conversationId;
    row.dataset.runId = state.chatRunId;
    row.dataset.lightweightMode = String(Boolean(event.lightweight_mode));
  } else if (event.type === 'user_guidance') {
    hideChoiceButtons();
    promoteRunGuidance(event.message_id);
    row.querySelectorAll('.tool-confirm').forEach((confirmation) => {
      const actions = confirmation.querySelector('.tool-confirm-actions');
      if (actions) actions.innerHTML = '<div class="tool-confirm-status">新指令已到达，原确认已撤销</div>';
    });
    setActivity('已收到引导，准备继续');
  } else if (event.type === 'interjection_consumed') {
    const messageRow = event.message_id
      ? document.querySelector(`.message-row[data-message-id="${CSS.escape(String(event.message_id))}"]`)
      : null;
    messageRow?.querySelector('.message-actions')?.remove();
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
  } else if (event.type === 'response_retracted') {
    clearStreamingAnswer(answer);
    row.querySelectorAll('.reasoning-block').forEach((block) => block.remove());
    row.querySelectorAll('.stream-prose').forEach((seg) => seg.remove());
    delete row.dataset.reasoningStreamed;
    setActivity(event.reason || '正在核验执行结果');
  } else if (event.type === 'skills') {
    const user = (event.skills || []).filter((s) => s?.source !== 'auto');
    const auto = (event.skills || []).filter((s) => s?.source === 'auto');
    const parts = [];
    if (user.length) parts.push(`已启用 ${user.map((s) => s?.name).join('、')}`);
    if (auto.length) parts.push(`已自动匹配 ${auto.map((s) => s?.name).join('、')}`);
    setActivity(parts.join('；'));
  } else if (event.type === 'skill_warning') {
    const warning = String(event.message || '本次引用的技能体积较大，已完整注入但可能影响响应速度');
    toast(warning);
    setActivity(warning);
  } else if (event.type === 'tools_available') {
    // Tool schemas are runtime state, not user-facing message content.
    // Keep tool execution/result details available without dumping the full
    // capability list into every response.
  } else if (event.type === 'delta') {
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
  } else if (event.type === 'reasoning_start') {
    clearElapsedStatus();
    state.streamingReasoningBlock = null;
    row.querySelectorAll('.reasoning-block[data-active="true"]').forEach((block) => {
      block.dataset.active = 'false';
      if (!(block.querySelector('.reasoning-content')?.dataset.raw || '').trim()) block.remove();
    });
  } else if (event.type === 'reasoning_delta') {
    if (!String(event.content || '').trim()) return;
    let block = row.querySelector('.reasoning-block[data-active="true"]');
    if (!block) {
      block = createStreamingReasoningBlock(answer);
    }
    state.streamingReasoningBlock = block;
    const content = block.querySelector('.reasoning-content');
    scheduleStreamingMarkdown(content, (content.dataset.raw || '') + String(event.content || ''));
    row.dataset.reasoningStreamed = 'true';
  } else if (event.type === 'reasoning_end') {
    // 工具 vs 正式的分类不在此处做（正文 delta 无法可靠区分：模型可能在工具前
    // 先叙说一句）。这里保持展开；接下来若 tool_start 到来，由 collapseToolReasoningBlock
    // 坍缩成单行；若一直无 tool_start（正式回复）则保持展开。
    const block = state.streamingReasoningBlock;
    if (block) {
      block.dataset.active = 'false';
      const content = block.querySelector('.reasoning-content');
      if (!(content?.dataset.raw || '').trim()) block.remove();
    }
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
    answer.before(details);
    // 让新插入的工具块始终位于末尾（紧贴 answer），从而保持时间顺序。
    scrollToBottom();
  } else if (event.type === 'tool_start_legacy') {
    if (row.dataset.sawTool !== 'true') {
      moveBottomProseInline(row, answer);
      row.dataset.sawTool = 'true';
    }
    collapseToolReasoningBlock();
    answer.insertAdjacentHTML(
      'beforebegin',
      `<div class="tool-run">正在执行 · ${escapeHtml(event.tool)}${event.reason ? ` · ${escapeHtml(event.reason)}` : ''}</div>`
    );
    scrollToBottom();
  } else if (event.type === 'tool_result') {
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
  } else if (event.type === 'tool_result_legacy') {
    const toolRuns = row.querySelectorAll('.tool-run');
    const last = toolRuns[toolRuns.length - 1];
    if (last) last.textContent = `${event.success ? '已完成' : '失败'} · ${event.tool}`;
  } else if (event.type === 'tool_confirm') {
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
          <button class="tool-confirm-btn tool-confirm-reject" onclick="rejectTool('${escapeHtml(confirmId)}', '${escapeHtml(event.run_id || runId)}')">拒绝</button>
          <button class="tool-confirm-btn tool-confirm-approve" onclick="approveTool('${escapeHtml(confirmId)}', '${escapeHtml(event.run_id || runId)}')">允许执行</button>
        </div>
      </div>`;
    answer.insertAdjacentHTML('beforebegin', confirmMarkup);
    scrollToBottom();
  } else if (event.type === 'choice') {
    // AI回复包含可选项，显示选择按钮
    showChoiceButtons(event.choices, event.choice_groups);
  } else if (event.type === 'cancelled') {
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
  } else if (event.type === 'run_failed') {
    clearElapsedStatus();
    clearVisionProgress();
    setActivity('');
    // 工具协议解析失败：只展示可读错误，不显示原始 XML/JSON 或命令参数。
    answer.innerHTML = `<p>执行失败：${escapeHtml(event.error || '任务执行失败')}</p>`;
    $('#runtimeStatus').textContent = '执行失败';
  } else if (event.type === 'context_full') {
    // 上下文已达上限：后端已阻止本次请求，立即锁定输入并提示新建对话。
    state.contextAtCeiling = true;
    updateContextComposerLock(Boolean(state.chatBusy));
  } else if (event.type === 'done') {
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
    const followupRunId = String(event.followup_run_id || '');
    if (followupRunId && !state.cancelRequested && !state.cancelledRunIds.has(String(runId || ''))) {
      void (async () => {
        try {
          const followup = await api(`/api/runs/${encodeURIComponent(followupRunId)}`);
          if (followup?.id && !state.cancelRequested && state.conversationId === conversationId) await resumeRun(followup);
        } catch (error) {
          console.debug('[naiba] 后续插话 Run 恢复失败:', error.message);
        }
      })();
    }
  } else if (event.type === 'error') {
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

function hideChoiceButtons() {
  const existing = $('#choiceButtons');
  if (existing) existing.remove();
}

function fillComposer(text) {
  // 把按钮拼好的内容放进输入框由用户确认，不自动发送。
  const input = $('#messageInput');
  if (!input) return;
  input.value = String(text || '');
  resizeTextarea();
  renderInputMirror();
  updateSkillPopup();
  input.focus();
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
  state.chatBusy = busy;
  const mc = $('#messages');
  if (mc) mc.classList.toggle('conversation-running', busy);
  const sendBtn = $('#sendButton');
  sendBtn.disabled = Boolean(state.cancelRequested);
  sendBtn.classList.toggle('is-stop', busy);
  sendBtn.textContent = state.cancelRequested ? '…' : (busy ? '■' : '↑');
  sendBtn.title = state.cancelRequested ? '正在停止' : (busy ? '停止当前任务' : '发送');
  $$('#choiceButtons button').forEach((button) => { button.disabled = busy; });
  sendBtn.setAttribute('aria-label', state.cancelRequested ? '正在停止' : (busy ? '停止当前任务' : '发送'));
  $('#messageInput').disabled = false;
  updateContextComposerLock(busy);
  updateLightweightModeControl();
  updateUnloadModelButton();
  if (!busy && $('#runtimeStatus').textContent !== '执行失败') $('#runtimeStatus').textContent = '就绪';
}

async function cancelCurrentRun() {
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
function reloadPage() {
  if (state.runWatchdogTimer) stopRunWatchdog();
  if (state.elapsedTimer) clearElapsedStatus();
  window.location.reload();
}

// ============================================================
// Skill 快速引用（/ 索引 + 蓝色高亮 + 顶栏点击插入 + 发送解析）
// ============================================================
function skillList() { return Array.isArray(state.bootstrap?.skills) ? state.bootstrap.skills : []; }

// 按 /ref 反查 skill：优先 ref，其次 name，忽略大小写。
function skillByRef(refText) {
  const key = String(refText || '').trim().toLowerCase();
  if (!key) return null;
  return skillList().find((s) => String(s.ref || '').toLowerCase() === key)
    || skillList().find((s) => String(s.name || '').toLowerCase() === key) || null;
}

// 识别文本里所有 <(^|\s)/ref> 且命中了已安装 skill 的引用。
function tokenizeSkillRefs(text) {
  const matches = [];
  const re = /(^|\s)\/([^\s/#]+)/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    const name = m[2];
    const skill = skillByRef(name);
    if (!skill) continue;
    const slashAt = m.index + m[1].length;
    matches.push({ start: slashAt, end: slashAt + 1 + name.length, text: '/' + name, skill });
  }
  return matches;
}

// 供镜像层/气泡：把文本转成带 <span class="skill-ref"> 高亮的 HTML。
function highlightSkillRefsHtml(text) {
  const tokens = tokenizeSkillRefs(text);
  if (!tokens.length) return escapeHtml(text);
  let out = ''; let pos = 0;
  for (const tok of tokens) {
    out += escapeHtml(text.slice(pos, tok.start));
    out += `<span class="skill-ref">${escapeHtml(tok.text)}</span>`;
    pos = tok.end;
  }
  out += escapeHtml(text.slice(pos));
  return out;
}

function renderInputMirror() {
  const mirror = $('#inputMirror');
  const input = $('#messageInput');
  if (!mirror || !input) return;
  const value = input.value;
  // 空内容时用一个零宽字符撑起镜像层；非空时只放原文本（不额外追加零宽字符，避免影响换行）。
  mirror.innerHTML = value ? highlightSkillRefsHtml(value) : '\u200b';
  mirror.scrollTop = input.scrollTop;
}

// 当前光标所在的那个 <(^|\s)/name…> token；无效时返回 null。
function currentSlashToken(value, cursor) {
  if (!value || cursor == null) return null;
  const isWS = (ch) => ch === undefined || /\s/.test(ch);
  let i = cursor;
  while (i > 0 && !isWS(value[i - 1])) i--;
  if (i >= cursor || value[i] !== '/') return null;
  if (i > 0 && !isWS(value[i - 1])) return null;
  let j = cursor;
  while (j < value.length && !isWS(value[j])) j++;
  if (cursor < i + 1 || cursor > j) return null;
  return { tokenStart: i, tokenEnd: j, typed: value.slice(i + 1, cursor) };
}

const popupState = { open: false, selectedIndex: 0, items: [], token: null };

function positionSkillPopup() {
  const popup = $('#skillPopup');
  const input = $('#messageInput');
  if (!popup || !input || popup.hidden) return;
  const rect = input.getBoundingClientRect();
  const ph = popup.offsetHeight;
  let top = rect.top - ph - 6;
  if (top < 8) top = rect.bottom + 6;
  popup.style.left = `${Math.max(8, rect.left)}px`;
  popup.style.width = `${rect.width}px`;
  popup.style.top = `${top}px`;
}

function showSkillPopup(items, selectedIndex, token) {
  const popup = $('#skillPopup');
  if (!popup) return;
  popupState.open = true; popupState.items = items; popupState.token = token; popupState.selectedIndex = selectedIndex;
  if (!items.length) {
    popup.innerHTML = '<div class="skill-popup-empty">没有匹配的 Skill</div>';
    popup.hidden = false;
    positionSkillPopup();
    return;
  }
  popup.innerHTML = items.map((s, i) => `
    <button type="button" role="option" class="skill-popup-item${i === selectedIndex ? ' selected' : ''}" data-skill-index="${i}">
      <div class="skill-popup-main"><b>${escapeHtml(s.name)}</b><em class="skill-size">${s.char_count ? ('~' + s.char_count) : ''}</em></div>
      <div class="skill-popup-sub"><span class="skill-popup-ref">/${escapeHtml(s.ref || s.name)}</span><small>${escapeHtml(s.description || '')}</small></div>
    </button>`).join('');
  popup.querySelector('.selected')?.scrollIntoView({ block: 'nearest' });
  popup.hidden = false;
  positionSkillPopup();
}

function hideSkillPopup() {
  popupState.open = false; popupState.items = []; popupState.token = null; popupState.selectedIndex = 0;
  const popup = $('#skillPopup');
  if (popup) popup.hidden = true;
}

function setSkillPopupSelection(index) {
  popupState.selectedIndex = index;
  const popup = $('#skillPopup');
  popup?.querySelectorAll('[data-skill-index]').forEach((el) => {
    el.classList.toggle('selected', Number(el.dataset.skillIndex) === index);
  });
  popup?.querySelector('.selected')?.scrollIntoView({ block: 'nearest' });
}

function updateSkillPopup() {
  const input = $('#messageInput');
  if (!input) return hideSkillPopup();
  const token = currentSlashToken(input.value, input.selectionStart);
  if (!token) return hideSkillPopup();
  const query = token.typed.toLowerCase();
  const items = skillList().filter((s) =>
    !query || `${s.ref || ''} ${s.name || ''} ${s.description || ''}`.toLowerCase().includes(query));
  items.sort((a, b) => {
    const ap = String(a.ref || '').toLowerCase().startsWith(query) ? 0 : 1;
    const bp = String(b.ref || '').toLowerCase().startsWith(query) ? 0 : 1;
    return ap - bp || String(a.ref || '').localeCompare(String(b.ref || ''));
  });
  showSkillPopup(items, 0, token);
}

function moveSkillPopupSelection(delta) {
  if (!popupState.open || !popupState.items.length) return;
  const n = popupState.items.length;
  setSkillPopupSelection((popupState.selectedIndex + delta + n) % n);
}

function commitSkillSelection(skill) {
  const input = $('#messageInput');
  const value = input.value;
  const cursor = input.selectionStart;
  const token = currentSlashToken(value, cursor);
  if (!token) { hideSkillPopup(); return; }
  const replacement = '/' + (skill.ref || skill.name) + ' ';
  const newValue = value.slice(0, token.tokenStart) + replacement + value.slice(token.tokenEnd);
  const newCursor = token.tokenStart + replacement.length;
  input.value = newValue;
  input.setSelectionRange(newCursor, newCursor);
  resizeTextarea();
  renderInputMirror();
  hideSkillPopup();
  input.focus();
}

// 在光标处插入一个 skill 引用（顶栏点击 / 预填复用）。
function insertSkillRefAtCursor(skill) {
  const input = $('#messageInput');
  if (!input) return;
  const refText = '/' + (skill.ref || skill.name);
  const cs = input.selectionStart ?? input.value.length;
  const ce = input.selectionEnd ?? input.value.length;
  const before = input.value.slice(0, cs);
  const after = input.value.slice(ce);
  const needsLeading = cs > 0 && !/\s/.test(input.value[cs - 1]);
  const insertion = (needsLeading ? ' ' : '') + refText + ' ';
  input.value = before + insertion + after;
  const newCursor = before.length + insertion.length;
  input.setSelectionRange(newCursor, newCursor);
  resizeTextarea();
  renderInputMirror();
}

// 新会话：把当前 Agent 预设 skill 以 /ref 引用预填到输入框（用户删掉即不引用，统一途径）。
function prefillPresetSkillsInComposer(conversation) {
  const input = $('#messageInput');
  if (!input) return;
  const agentId = String(conversation?.agent_id || '');
  const agent = (state.bootstrap?.agents || []).find((a) => a.id === agentId);
  const presetIds = new Set((agent?.skill_ids || []).map(String));
  if (!presetIds.size) return;
  const skills = skillList().filter((s) => presetIds.has(String(s.id)));
  if (!skills.length) return;
  input.value = skills.map((s) => '/' + (s.ref || s.name)).join(' ') + ' ';
  resizeTextarea();
  renderInputMirror();
  input.setSelectionRange(input.value.length, input.value.length);
  input.focus();
}

// 切换 Agent 后：把该 Agent 预设 Skill 以 /ref 追加到输入框末尾（已在框内的跳过，避免重复）。
function appendPresetSkillsToComposer(agentId) {
  const input = $('#messageInput');
  if (!input) return;
  const agent = (state.bootstrap?.agents || []).find((a) => String(a.id) === String(agentId));
  const presetIds = new Set((agent?.skill_ids || []).map(String));
  if (!presetIds.size) return;
  const skills = skillList().filter((s) => presetIds.has(String(s.id)));
  if (!skills.length) return;
  const existing = input.value.trimEnd();
  const have = new Set(tokenizeSkillRefs(existing).map((t) => String(t.skill.id)));
  const refs = skills.filter((s) => !have.has(String(s.id))).map((s) => '/' + (s.ref || s.name));
  if (!refs.length) return;
  input.value = existing ? existing + ' ' + refs.join(' ') : refs.join(' ');
  resizeTextarea();
  renderInputMirror();
  input.setSelectionRange(input.value.length, input.value.length);
  input.focus();
}

// 解析并返回本消息引用的 skill（去重）。
function parseSkillReferences(text) {
  const seen = new Set();
  const refs = [];
  for (const tok of tokenizeSkillRefs(text)) {
    if (seen.has(tok.skill.id)) continue;
    seen.add(tok.skill.id);
    refs.push(tok);
  }
  return refs;
}

// 把 /ref 引用从消息文本里剥离（发给模型用）；若剥空则保留原文（纯引用调用场景）。
function stripSkillReferences(text) {
  const tokens = tokenizeSkillRefs(text);
  if (!tokens.length) return text;
  let out = ''; let pos = 0;
  for (const tok of tokens) {
    out += text.slice(pos, tok.start);
    pos = tok.end;
  }
  out += text.slice(pos);
  const cleaned = out.replace(/\s+/g, ' ').trim();
  return cleaned || text;
}

// 用户气泡：优先显示 display_content（含 /ref），并对命中 skill 的引用高亮；保留 markdown。
function renderUserContent(text) {
  const tokens = tokenizeSkillRefs(text);
  if (!tokens.length) return markdown(text);
  let protectedText = ''; let pos = 0; let idx = 0;
  const mapping = [];
  for (const tok of tokens) {
    protectedText += text.slice(pos, tok.start);
    const ph = `@@SKILLREF${idx++}@@`;
    mapping.push({ ph, text: tok.text });
    protectedText += ph;
    pos = tok.end;
  }
  protectedText += text.slice(pos);
  let html = markdown(protectedText);
  for (const m of mapping) html = html.split(m.ph).join(`<span class="skill-ref">${escapeHtml(m.text)}</span>`);
  return html;
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
  document.addEventListener('contextmenu', (event) => {
    hideTextContextMenu();
    const editable = editableElement(event.target);
    if (editable) {
      event.preventDefault();
      showTextContextMenu(event, '', 'edit', editable);
      return;
    }
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.toString().trim()) return;
    const range = selection.getRangeAt(0);
    const startNode = range.startContainer.nodeType === Node.ELEMENT_NODE
      ? range.startContainer : range.startContainer.parentElement;
    const endNode = range.endContainer.nodeType === Node.ELEMENT_NODE
      ? range.endContainer : range.endContainer.parentElement;
    const messageBody = startNode?.closest('.message-body');
    if (!messageBody || endNode?.closest('.message-body') !== messageBody) return;
    event.preventDefault();
    showTextContextMenu(event, selection.toString(), 'selection', null);
  });
  document.addEventListener('pointerdown', (event) => {
    if (!event.target.closest?.('#textContextMenu')) hideTextContextMenu();
  });
  const textContextMenu = ensureContextMenu();
  textContextMenu.addEventListener('mousedown', (event) => {
    // 阻止菜单按钮抢走文本框焦点，保持选中状态可见。
    if (event.target.closest?.('[data-context-action]')) event.preventDefault();
  });
  textContextMenu.addEventListener('click', (event) => {
    const button = event.target.closest('[data-context-action]');
    if (button) runTextContextAction(button.dataset.contextAction);
  });
  textContextMenu.addEventListener('keydown', (event) => {
    const buttons = [...textContextMenu.querySelectorAll('button:not(:disabled)')];
    const index = buttons.indexOf(document.activeElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      hideTextContextMenu();
      contextMenuPreviousFocus?.focus?.({ preventScroll: true });
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const offset = event.key === 'ArrowDown' ? 1 : -1;
      buttons[(index + offset + buttons.length) % buttons.length]?.focus();
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      buttons[event.key === 'Home' ? 0 : buttons.length - 1]?.focus();
    }
  });
  window.addEventListener('blur', hideTextContextMenu);
  window.addEventListener('resize', hideTextContextMenu);
  window.addEventListener('scroll', hideTextContextMenu, true);
  // 大图右键 → 复制图片剪贴板（仅 pywebview 窗口自绘菜单；真实浏览器保留原生“复制图片”菜单）。
  document.addEventListener('contextmenu', (event) => {
    if (!isPywebview()) return;
    if (!event.target.closest?.('#imageLightboxImg')) return;
    event.preventDefault();
    showImageContextMenu(event);
  });
  document.addEventListener('pointerdown', (event) => {
    if (!event.target.closest?.('#imageContextMenu')) hideImageContextMenu();
  });
  const imageMenu = ensureImageContextMenu();
  imageMenu.addEventListener('contextmenu', (event) => event.preventDefault());
  imageMenu.addEventListener('click', (event) => {
    const button = event.target.closest('[data-image-context-action]');
    if (button) runImageContextAction(button.dataset.imageContextAction);
  });
  imageMenu.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { event.preventDefault(); hideImageContextMenu(); }
  });
  window.addEventListener('blur', hideImageContextMenu);
  window.addEventListener('resize', hideImageContextMenu);
  window.addEventListener('scroll', hideImageContextMenu, true);
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
  $('#newChatButton').addEventListener('click', () => createConversation('', '', true));
  $('#modelSelect').addEventListener('change', saveModelSelection);
  $('#unloadModel').addEventListener('click', unloadCurrentModel);
  $('#agentSelect').addEventListener('change', saveAgentSelection);
  $('#openSkills').addEventListener('click', () => $('#skillsDialog').showModal());
  $('#openTasks').addEventListener('click', () => $('#tasksDialog').showModal());
  // 显式刷新按钮：适配 EXE 内嵌 pywebview 无法使用 F5 的场景，EXE 与浏览器通用。
  $('#reloadPage')?.addEventListener('click', () => reloadPage());
  $('#clearTerminalTasks').addEventListener('click', clearTerminalTasks);
  $('#clearConversationMessages').addEventListener('click', clearConversationMessages);
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
  $('#saveMcpServer')?.addEventListener('click', saveMcpServer);
  $('#openSettings').addEventListener('click', () => $('#settingsDialog').showModal());
  $$('[data-close]').forEach((button) => button.addEventListener('click', () => $(`#${button.dataset.close}`).close()));
  $('#conversationSettingsForm').addEventListener('submit', saveConversationSettings);
  $('#skillSearch').addEventListener('input', (event) => renderSkills(event.target.value));
  $('#composerForm').addEventListener('submit', (event) => {
    event.preventDefault();
    if (state.chatRunId || state.abortController) cancelCurrentRun();
    else sendMessage();
  });
  $('#messageInput').addEventListener('input', () => { resizeTextarea(); renderInputMirror(); updateSkillPopup(); });
  $('#messageInput').addEventListener('select', updateSkillPopup);
  $('#messageInput').addEventListener('click', updateSkillPopup);
  $('#messageInput').addEventListener('focus', updateSkillPopup);
  $('#messageInput').addEventListener('scroll', () => { const mirror = $('#inputMirror'); if (mirror) mirror.scrollTop = $('#messageInput').scrollTop; positionSkillPopup(); });
  window.addEventListener('resize', positionSkillPopup);
  $('#messageInput').addEventListener('keydown', (event) => {
    if (popupState.open) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        moveSkillPopupSelection(event.key === 'ArrowDown' ? 1 : -1);
        return;
      }
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        const sel = popupState.items[popupState.selectedIndex];
        if (sel) commitSkillSelection(sel);
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        hideSkillPopup();
        return;
      }
    }
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (state.chatRunId || state.abortController) {
        if (event.ctrlKey || event.metaKey) guideAllQueuedMessages();
        else sendRunInterjection($('#messageInput').value);
      } else {
        sendMessage();
      }
    }
  });
  $('#skillPopup').addEventListener('mousedown', (event) => event.preventDefault());
  $('#skillPopup').addEventListener('click', (event) => {
    const button = event.target.closest('[data-skill-index]');
    if (!button) return;
    const sel = popupState.items[Number(button.dataset.skillIndex)];
    if (sel) commitSkillSelection(sel);
  });
  $('#skillPopup').addEventListener('mouseover', (event) => {
    const button = event.target.closest('[data-skill-index]');
    if (!button) return;
    const idx = Number(button.dataset.skillIndex);
    if (idx !== popupState.selectedIndex) setSkillPopupSelection(idx);
  });
  $('#skillList').addEventListener('click', (event) => {
    const button = event.target.closest('[data-skill-insert]');
    if (!button) return;
    const skill = skillList().find((s) => s.id === button.dataset.skillInsert);
    if (!skill) return;
    insertSkillRefAtCursor(skill);
    renderInputMirror();
    $('#skillsDialog').close();
    $('#messageInput').focus();
  });
  $('#attachButton').addEventListener('click', () => $('#fileInput').click());
  $('#lightweightModeToggle').addEventListener('change', toggleLightweightMode);
  $('#fileInput').addEventListener('change', (event) => { uploadFiles([...event.target.files]); event.target.value = ''; });
  const composerWrap = document.querySelector('.composer-wrap');
  if (composerWrap) {
    composerWrap.addEventListener('dragover', (event) => {
      const types = event.dataTransfer?.types || [];
      const hasFiles = types.includes('Files');
      const hasPath = types.includes('application/x-naiba-file-path');
      if (!hasFiles && !hasPath) return;
      event.preventDefault();
      composerWrap.classList.add('dragover');
    });
    composerWrap.addEventListener('dragleave', (event) => {
      if (event.relatedTarget && composerWrap.contains(event.relatedTarget)) return;
      composerWrap.classList.remove('dragover');
    });
    composerWrap.addEventListener('drop', (event) => {
      const droppedPath = event.dataTransfer?.getData('application/x-naiba-file-path')
        || event.dataTransfer?.getData('text/uri-list');
      if (!event.dataTransfer?.files?.length && !droppedPath) return;
      event.preventDefault();
      composerWrap.classList.remove('dragover');
      if (event.dataTransfer.files?.length) uploadFiles([...event.dataTransfer.files]);
      else {
        const path = String(droppedPath).split('\n').find((item) => item && !item.startsWith('#')) || '';
        if (path) {
          const name = path.split(/[\\/]/).pop() || 'image';
          state.pendingFiles.push({ name, path, size: 0 });
          renderPendingFiles();
        }
      }
    });
  }
  // 记录用户是否停留在底部：流式输出时只有跟随底部才自动滚动，上滑阅读则不抢滚动。
  $('#messages').addEventListener('scroll', () => {
    stickToBottom = isNearBottom();
  }, { passive: true });
  $('#messages').addEventListener('dragstart', (event) => {
    // 缩略图（带 data-large-url）由全局 dragstart 统一以"大图 URL"拖动，这里跳过避免重复设置。
    if (event.target.closest?.('[data-large-url]')) return;
    const image = event.target.closest?.('.attachment-image img, .media-image-link img');
    if (!image || !event.dataTransfer) return;
    const source = image.closest('a')?.href || image.src;
    event.dataTransfer.effectAllowed = 'copy';
    event.dataTransfer.setData('text/uri-list', source);
    event.dataTransfer.setData('text/plain', source);
    const cached = draggedFileCache.get(source);
    if (cached && event.dataTransfer.items?.add) {
      try { event.dataTransfer.items.add(cached); } catch (_error) { /* browser may reject cross-origin items */ }
    }
    if (cached) {
      try { event.dataTransfer.setData('DownloadURL', `${cached.type || 'application/octet-stream'}:${cached.name}:${source}`); } catch (_error) { /* optional Chrome hint */ }
    }
    const path = decodeURIComponent(new URL(source, location.href).searchParams.get('path') || '');
    if (path) event.dataTransfer.setData('application/x-naiba-file-path', path);
  });
  $('#deepReasoningButton').addEventListener('click', toggleDeepReasoning);
  $('#reasoningMenu')?.addEventListener('click', async (event) => {
    const effort = event.target.closest?.('[data-reasoning-effort]')?.dataset.reasoningEffort;
    if (!effort || !state.conversationId || state.chatRunId || state.abortController) return;
    const previous = state.reasoningEffort || 'off';
    state.reasoningEffort = effort;
    state.deepReasoningEnabled = effort !== 'off';
    updateDeepReasoningButton();
    try {
      await api(`/api/conversations/${state.conversationId}/settings`, {
        method: 'POST', body: { reasoning_effort: effort, deep_reasoning_enabled: state.deepReasoningEnabled },
      });
      $('#reasoningMenu').hidden = true;
      toast(effort === 'auto' ? '思考强度：跟随 API（自动）' : `思考强度：${effort}`);
    } catch (error) {
      state.reasoningEffort = previous;
      state.deepReasoningEnabled = previous !== 'off';
      updateDeepReasoningButton();
      toast(`思考设置保存失败：${error.message}`);
    }
  });
  $('#contextUsageButton').addEventListener('click', toggleContextUsagePopover);
  $('#contextUsagePopover').addEventListener('click', (event) => event.stopPropagation());
  window.addEventListener('resize', positionContextUsagePopover);
  window.addEventListener('scroll', positionContextUsagePopover, true);
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
      const text = copyButton.closest('.message-body').querySelector('.answer-content')?.textContent || '';
      try {
        await copyText(text.trim());
        toast('已复制回复');
      } catch (error) {
        toast(`复制失败：${error.message}`);
      }
      return;
    }
    const branchButton = event.target.closest('[data-branch-message]');
    if (branchButton) {
      branchMessage(branchButton.closest('.message-row'));
      return;
    }
    const editButton = event.target.closest('[data-edit-message]');
    if (editButton) {
      startEditMessage(editButton.closest('.message-row'));
      return;
    }
    const deleteButton = event.target.closest('[data-delete-message]');
    if (deleteButton) {
      deleteMessage(deleteButton.closest('.message-row'));
    }
  });
  $('#runGuidanceList').addEventListener('click', (event) => {
    const row = event.target.closest('.run-guidance-card');
    if (!row) return;
    if (event.target.closest('[data-guide-message]')) {
      guideMessage(row);
      return;
    }
    if (event.target.closest('[data-edit-message]')) {
      startEditMessage(row);
      return;
    }
    if (event.target.closest('[data-delete-message]')) deleteMessage(row);
  });
  $$('.starter-grid button').forEach((button) => button.addEventListener('click', () => {
    if (button.dataset.installSkill) startSkillInstall();
    else if (button.dataset.editSkill) startSkillEdit();
    else if (button.id === 'starterAddBtn') openStarterPromptDialog();
    else if (button.dataset.prompt != null) sendMessage(button.dataset.prompt);
  }));
  $('#saveStarterPrompt').addEventListener('click', saveStarterPrompt);
  $('#copyAddress').addEventListener('click', async () => {
    if (!state.bootstrap?.lan_enabled || !state.bootstrap?.lan_url) return;
    try {
      await copyText(state.bootstrap.lan_url);
      toast('手机访问地址已复制');
    } catch (error) {
      toast(`复制失败：${error.message}`);
    }
  });
  $('#enableLanAccess').addEventListener('click', enableLanAccess);
  $('#openSidebar').addEventListener('click', openSidebar);
  $('#closeSidebar').addEventListener('click', closeSidebar);
  $('#sidebarBackdrop').addEventListener('click', closeSidebar);
  $('#sidebarWorkspaceTree').addEventListener('click', onSidebarTreeClick);
  // 侧栏虚拟化：滚动时按窗口重绘可视行
  $('#sidebarWorkspaceTree').addEventListener('scroll', () => {
    if (sidebarScrollRaf) return;
    sidebarScrollRaf = requestAnimationFrame(() => {
      sidebarScrollRaf = 0;
      const tree = $('#sidebarWorkspaceTree');
      if (tree && sidebarRowCache.length) renderSidebarWindow(tree.scrollTop);
    });
  }, { passive: true });
  // 侧栏宽度可调：拖动 resizer，限制在 [170, 窗口30%]，并持久化
  const sidebarResizer = $('#sidebarResizer');
  if (sidebarResizer) {
    const clampSidebarW = (w) => Math.max(170, Math.min(Math.max(170, window.innerWidth * 0.3), w));
    sidebarResizer.addEventListener('mousedown', (event) => {
      event.preventDefault();
      sidebarResizer.classList.add('dragging');
      const shellLeft = (document.querySelector('.app-shell')?.getBoundingClientRect().left || 0);
      const onMove = (e) => {
        document.documentElement.style.setProperty('--sidebar-w', clampSidebarW(e.clientX - shellLeft) + 'px');
      };
      const onUp = () => {
        sidebarResizer.classList.remove('dragging');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        const w = document.documentElement.style.getPropertyValue('--sidebar-w');
        localStorage.setItem('naibaChatSidebarW', w);
        renderSidebar();
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }
  // 窗口大小变化时，把侧栏宽度压回窗口 30% 上限，并重绘虚拟列表
  const recalcSidebarW = () => {
    const cur = parseFloat(document.documentElement.style.getPropertyValue('--sidebar-w') || 0) || 272;
    const maxW = Math.max(170, window.innerWidth * 0.3);
    if (cur > maxW) {
      document.documentElement.style.setProperty('--sidebar-w', maxW + 'px');
      localStorage.setItem('naibaChatSidebarW', maxW + 'px');
    }
    renderSidebar();
  };
  window.addEventListener('resize', recalcSidebarW);
  $('#addWorkspace').addEventListener('click', createWorkspace);
  $('#workspaceSort').addEventListener('click', () => {
    state.workspaceSort = state.workspaceSort === 'updated' ? 'name' : 'updated';
    renderSidebar();
    toast(state.workspaceSort === 'name' ? '已按名称排序' : '已按时间排序');
  });
  $('#workspaceSearch').addEventListener('click', () => {
    const input = $('#workspaceSearchInput');
    if (!input) return;
    input.hidden = !input.hidden;
    if (!input.hidden) input.focus();
    else { input.value = ''; state.workspaceSearch = ''; renderSidebar(); }
  });
  $('#workspaceSearchInput').addEventListener('input', (event) => {
    state.workspaceSearch = event.target.value;
    renderSidebar();
  });
  $('#composerWorkspaceSelect').addEventListener('change', onComposerWorkspaceChange);
  $('#saveWorkspace').addEventListener('click', saveWorkspaceSettings);
  $('#browseWorkspace')?.addEventListener('click', () => pickWorkspace('workspaceDialogInput'));
  $('#browseWorkspaceSettings')?.addEventListener('click', () => pickWorkspace('workspaceDir'));
  $('#workspaceRefresh')?.addEventListener('click', () => loadWorkspaceTree(state.workspaceBrowsePath || ''));
  $('#workspaceUp')?.addEventListener('click', () => {
    const current = String(state.workspaceBrowsePath || '');
    const parent = current.replace(/[\\/][^\\/]+[\\/]?$/, '');
    if (parent && parent !== current) loadWorkspaceTree(parent);
  });
  $$('.settings-nav button').forEach((button) => button.addEventListener('click', () => switchSettingsTab(button.dataset.settingsTab)));
  $('#addProvider').addEventListener('click', addProvider);
  $('#providerSelect').addEventListener('change', (event) => {
    const provider = state.bootstrap.providers.find((item) => item.id === event.target.value);
    showProviderForm(provider || {});
  });
  $$('[data-provider-kind]').forEach((button) => button.addEventListener('click', () => {
    if (state.providerEditing || state.providerKindTab === button.dataset.providerKind) return;
    state.providerKindTab = button.dataset.providerKind;
    renderProviders();
  }));
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
  $('#providerModel').addEventListener('change', () => {
    toggleCustomModel();
    applyProviderModelCapabilities();
  });
  $('#providerSupportsImages').addEventListener('change', updateProviderVisionHint);
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
  $('#cleanImageCache')?.addEventListener('click', cleanImageCache);
  $('#imageUploadOriginal')?.addEventListener('change', renderImageCompressRow);
  $('#imageLightboxClose')?.addEventListener('click', closeImageLightbox);
  $('#imageLightbox')?.addEventListener('click', (event) => { if (event.target === event.currentTarget) closeImageLightbox(); });
  $('#saveToken').addEventListener('click', saveAccessToken);
  $('#checkUpdate').addEventListener('click', checkUpdate);
  $('#installUpdate').addEventListener('click', installUpdate);
  $('#updateVersionSelect').addEventListener('change', () => renderUpdateStatus(state.bootstrap.update || {}));
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
  $('#applyDataDir').addEventListener('click', applyAndMigrateDataDir);
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
    renderHiddenSkills(result.hidden_skills || []);
    const unhiddenTag = (result.unhidden && result.unhidden.length) ? ` · 已安装并取消隐藏` : '';
    setSkillImportStatus(`已安装 ${result.files} 个文件到 ${result.dir}${unhiddenTag}`, 'ok');
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
    renderHiddenSkills(result.hidden_skills || []);
    const unhiddenTag = (result.unhidden && result.unhidden.length) ? ' · 已安装并取消隐藏' : '';
    setSkillImportStatus(`已安装到 ${result.dir}${unhiddenTag}`, 'ok');
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
    renderHiddenSkills(result.hidden_skills || []);
    const unhiddenTag = (result.unhidden && result.unhidden.length) ? ' · 已安装并取消隐藏' : '';
    setSkillImportStatus(`已作为 Skill 安装到 ${result.dir}${unhiddenTag}`, 'ok');
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
    renderHiddenSkills(data.hidden_skills || []);
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
  if (state.bootstrap) {
    state.bootstrap.skills = lastInstalledSkills;
    const available = new Set(lastInstalledSkills.map((skill) => skill.id));
    state.selectedSkills = state.selectedSkills.filter((id) => available.has(id));
    localStorage.setItem('naibaChatSkillIds', JSON.stringify(state.selectedSkills));
    renderSkills($('#skillSearch')?.value || '');
    renderAgentSkillPicker();
  }
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
    renderHiddenSkills(result.hidden_skills || []);
    toast(result.hidden ? '已删除 Skill（原文件保留并隐藏）' : `已删除，回收位置：${result.recycled_to || '未知'}`);
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

function renderHiddenSkills(hiddenSkills) {
  const list = $('#hiddenSkillList');
  if (!list) return;
  const items = Array.isArray(hiddenSkills) ? hiddenSkills : [];
  const count = $('#hiddenSkillCount');
  if (count) count.textContent = String(items.length);
  list.innerHTML = '';
  if (!items.length) {
    list.innerHTML = '<small class="hint">无已隐藏的 Skill</small>';
    return;
  }
  items.forEach((skill) => {
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
    const unhide = document.createElement('button');
    unhide.className = 'skill-delete';
    unhide.type = 'button';
    unhide.title = '取消隐藏，恢复为可用的 Skill';
    unhide.textContent = '取消隐藏';
    unhide.addEventListener('click', () => unhideSkill(skill.id));
    item.append(unhide);
    list.append(item);
  });
}

async function unhideSkill(skillId) {
  if (!skillId) return;
  try {
    const result = await api('/api/skills/unhide', { method: 'POST', body: { skill_id: skillId } });
    if (state.bootstrap && result.skills) state.bootstrap.skills = result.skills;
    renderInstalledSkills(result.skills || []);
    renderHiddenSkills(result.hidden_skills || []);
    renderSkills($('#skillSearch')?.value || '');
    toast('已取消隐藏该 Skill');
  } catch (error) {
    toast(`取消隐藏失败：${error.message}`);
  }
}

function renderDataMigration() {
  const m = state.bootstrap?.data_migration || {};
  const configured = m.configured_data_dir || state.bootstrap?.settings?.resolved_data_dir || m.data_dir || '';
  if ($('#dataDir') && document.activeElement !== $('#dataDir')) $('#dataDir').value = configured;
  $('#migrationDbVersion').textContent = m.db_version != null ? String(m.db_version) : '-';
  $('#migrationDataDir').textContent = m.restart_required
    ? `${m.data_dir || '-'}（重启后切换到 ${configured}）`
    : (configured || m.data_dir || '-');
  const skillsDirs = Array.isArray(m.resolved_skills_dirs) ? m.resolved_skills_dirs : [];
  $('#migrationSkillsDir').textContent = skillsDirs.length ? skillsDirs.join('；') : '-';
  $('#migrationHealthy').textContent = m.healthy === true ? '✓ 健康' : (m.healthy === false ? '✗ 异常' : '-');
  $('#migrationApplied').textContent = Array.isArray(m.applied_versions)
    ? (m.applied_versions.length ? m.applied_versions.join(', ') : '无')
    : '-';
  $('#migrationBackup').textContent = m.backup_location || '-';
}

async function applyAndMigrateDataDir() {
  const value = $('#dataDir')?.value.trim() || '';
  if (!value) {
    $('#migrationMessage').textContent = '请先填写目标数据目录';
    return;
  }
  if (!confirm('将当前数据库、上传文件与 Skills 目录一并复制到新目录，并完成结构迁移。完成后需要重启，继续？')) return;
  const btn = $('#applyDataDir');
  const previousText = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '迁移中…'; }
  try {
    const result = await api('/api/migration/move-data', { method: 'POST', body: { data_dir: value } });
    if (!result.ok) {
      $('#migrationMessage').textContent = `迁移失败：${result.error || '未知错误'}`;
      return;
    }
    const target = result.target_data_dir || value;
    const skills = result.target_skills_dir || (result.resolved_skills_dirs && result.resolved_skills_dirs[0]) || '';
    $('#migrationMessage').textContent = `数据库与 Skills 已复制到新目录（数据：${target}${skills ? `；Skills：${skills}` : ''}），请完全退出并重新启动 NaibaChat 生效。`;
    state.bootstrap.data_migration = result;
    renderDataMigration();
  } catch (error) {
    $('#migrationMessage').textContent = `迁移失败：${error.message}`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = previousText; }
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

function switchSettingsTab(name) {
  $$('.settings-nav button').forEach((button) => button.classList.toggle('active', button.dataset.settingsTab === name));
  $$('[data-settings-panel]').forEach((panel) => { panel.hidden = panel.dataset.settingsPanel !== name; });
  if (name === 'agent') renderAgentManager();
  if (name === 'skills') loadInstalledSkills(false);
  if (name === 'connections') loadMcpServers();
  if (name === 'datamigration') loadDataMigrationHealth();
  if (name === 'updates') api('/api/update').then((status) => {
    state.bootstrap.update = status;
    renderUpdateStatus(status);
  }).catch((error) => toast(`读取更新状态失败：${error.message}`));
}

bindEvents();
initialize();
