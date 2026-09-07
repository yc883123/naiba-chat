// ============================================================
// 03-media.js —— 拆分自 public/app.js 第 712-1211 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, api, draggedFileCache, escapeHtml, state, toast } from "./01-core.js";
import { markdown } from "./02-markdown.js";
import { selectedProvider } from "./07-models-agents.js";
import { renderPendingFiles } from "./10-upload.js";
export function fileUrl(source) {
  const value = String(source || '');
  if (/^https?:\/\//i.test(value) && !/^https?:\/\/(?:127\.0\.0\.1|localhost):8188\//i.test(value)) return value;
  return `/api/file?token=${encodeURIComponent(state.token)}&path=${encodeURIComponent(value)}`;
}

export function attachmentThumbPath(attachment) {
  if (attachment.thumb_path) return attachment.thumb_path;
  const p = String(attachment.path || attachment.source || '');
  if (!p || /^https?:\/\//i.test(p)) return '';
  const dot = p.lastIndexOf('.');
  return (dot > 0 ? p.slice(0, dot) : p) + '_thumb.webp';
}

export function attachmentThumbUrl(attachment) {
  const thumb = attachmentThumbPath(attachment);
  const source = attachment.path || attachment.source || '';
  return thumb ? fileUrl(thumb) : fileUrl(source);
}

export function openImageLightbox(largeUrl) {
  const img = $('#imageLightboxImg');
  const box = $('#imageLightbox');
  if (!img || !box || !largeUrl) return;
  if (!/^(\/api\/file|https?:\/\/)/i.test(largeUrl)) return;
  img.onerror = () => closeImageLightbox();
  img.src = largeUrl;
  box.hidden = false;
}

export function closeImageLightbox() {
  const box = $('#imageLightbox');
  if (box) box.hidden = true;
  const img = $('#imageLightboxImg');
  if (img) img.removeAttribute('src');
}

// ---- 大图右键 → 复制图片到剪贴板 ----
// pywebview（WebView2）默认关闭了浏览器右键菜单（AreDefaultContextMenusEnabled 仅 debug 开启），
// 因此在 pywebview 窗口内自绘一个轻量菜单；真实浏览器保留其原生“复制图片”。
export function isPywebview() {
  return Boolean(window.pywebview && window.pywebview.api);
}

export function ensureImageContextMenu() {
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

export function hideImageContextMenu() {
  const menu = $('#imageContextMenu');
  if (menu) menu.hidden = true;
}

export function showImageContextMenu(event) {
  const menu = ensureImageContextMenu();
  menu.hidden = false;
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  menu.style.left = `${Math.max(6, Math.min(event.clientX, window.innerWidth - width - 6))}px`;
  menu.style.top = `${Math.max(6, Math.min(event.clientY, window.innerHeight - height - 6))}px`;
  // 不自动聚焦按钮，避免图片失焦影响后续复制路径。
}

export function bytesToBase64(bytes) {
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

// 取当前大图的字节。优先同源 fetch（/api/file 由其自身服务，必然可读）；
// 跨源或 fetch 失败时回退 canvas 转 PNG（跨源且未开 CORS 的图会被污染并抛错）。
export async function imageBytesFrom(img) {
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

export async function copyLightboxImage() {
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

export async function runImageContextAction(action) {
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

export function mediaMarkup(attachments = []) {
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

export function toolRunMarkup(run = {}) {
  return `<details class="tool-run">
    <summary>${run.success ? '已执行' : '执行失败'} · ${escapeHtml(run.tool)}${run.reason ? ` · ${escapeHtml(run.reason)}` : ''}</summary>
    <pre>${escapeHtml(JSON.stringify(run.arguments || {}, null, 2))}\n\n${escapeHtml(run.result || '')}</pre>
  </details>`;
}

export function toolMarkup(runs = []) {
  if (!runs.length) return '';
  return `<div class="tool-stack">${runs.map((run) => toolRunMarkup(run)).join('')}</div>`;
}

export function activityMarkup(activity = []) {
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

export function reasoningMarkup(reasoning, finalOpen = false) {
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

export function formatDateTime(ms) {
  const date = new Date(Number(ms) || 0);
  if (!date.getTime()) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function usageRequestLine(item) {
  const input = Math.max(0, Number(item.input_tokens || 0));
  const output = Math.max(0, Number(item.output_tokens || 0));
  const cached = Math.max(0, Number(item.cached_tokens || 0));
  const total = Math.max(0, Number(item.total_tokens || 0)) || input + output;
  const rate = input ? (cached / input * 100).toFixed(1) : '0.0';
  const ms = Number(item.request_ms || 0);
  return `<div class="usage-request-line">第 ${Number(item.index || 0)} 次请求：输入 ${input.toLocaleString()} · 输出 ${output.toLocaleString()} · 总 ${total.toLocaleString()} · 命中率 ${rate}%（命中 ${cached.toLocaleString()} / 重算 ${Math.max(0, input - cached).toLocaleString()}）${ms > 0 ? ` · 耗时 ${(ms / 1000).toFixed(1)}s` : ''}</div>`;
}

export function usageMarkup(usage, createdAt = null) {
  if (!usage || typeof usage !== 'object') return '';
  const input = Number(usage.input_tokens || 0);
  const output = Number(usage.output_tokens || 0);
  const cached = Number(usage.cached_tokens || 0);
  const total = Number(usage.total_tokens || input + output);
  const performance = usage.performance || {};
  const vision = performance.vision || usage.lanes?.vision || {};
  const visualMs = Number(vision.total_ms || vision.diagnostics?.total_ms || 0);
  const visionCacheHit = Boolean(vision.cache_hit);
  if (!input && !output && !visualMs && !visionCacheHit) {
    return '<div class="usage-line">Token / 缓存命中率：供应商未返回</div>';
  }
  const rate = input ? Number(usage.cache_hit_rate ?? (cached / input * 100)).toFixed(1) : '0.0';
  const requests = Math.max(1, Number(usage.requests || 1));
  const miss = Math.max(0, Number(usage.uncached_tokens ?? (input - cached)));
  const details = Array.isArray(usage.requests_detail) ? usage.requests_detail : [];
  const detailsHtml = details.length
    ? `<button class="usage-toggle-btn" type="button" data-usage-toggle aria-expanded="false" aria-label="查看逐次请求明细">请求明细 <span class="usage-toggle-arrow">▸</span></button><div class="usage-requests" hidden>${details.map((item) => usageRequestLine(item)).join('')}</div>`
    : '';
  const tokenLine = (input || output)
    ? `<div class="usage-line" title="本轮 ${requests} 次模型请求">本轮 ${total.toLocaleString()} tokens · 输入 ${input.toLocaleString()} · 输出 ${output.toLocaleString()} · 缓存命中率 ${rate}%（命中 ${cached.toLocaleString()} / 重算 ${miss.toLocaleString()}）${detailsHtml}</div>`
    : '';
  const durationMs = Number(performance.total_ms || 0);
  const when = formatDateTime(createdAt);
  const durationLine = (durationMs > 0 || requests > 1)
    ? `<div class="usage-line usage-duration">本轮总耗时 ${(durationMs / 1000).toFixed(1)}s，共 ${requests} 次请求${when ? `。${when}` : ''}</div>`
    : '';
  // 只保留视觉 lane（聊天"lane 耗时"是最后一次请求的诊断值，与"本轮总耗时"重复且易误导，已移除）。
  const laneLine = (visualMs || visionCacheHit)
    ? `<div class="usage-line usage-performance">${visionCacheHit ? '视觉缓存命中' : ''}${(visionCacheHit && visualMs) ? ' · ' : ''}${visualMs ? `视觉 ${(visualMs / 1000).toFixed(1)}s` : ''}</div>`
    : '';
  const warnings = Array.isArray(performance.warnings) ? performance.warnings : [];
  const warningLine = warnings.map((item) => `<div class="usage-warning">${escapeHtml(item)}</div>`).join('');
  return `${tokenLine}${durationLine}${laneLine}${warningLine}`;
}

export function updateContextUsage(messages = null, message = null) {
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

export function renderContextUsage() {
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

export function updateContextComposerLock(busy = false) {
  const atCeiling = Boolean(state.contextAtCeiling);
  const input = $('#messageInput');
  const sendBtn = $('#sendButton');
  // Always lock the input at the ceiling so the user cannot draft a new turn.
  if (input) {
    input.disabled = atCeiling;
    input.placeholder = atCeiling
      ? '上下文已满，请新建对话后继续'
      : (busy ? '回复进行中…' : '输入消息');
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

export function positionContextUsagePopover() {
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

export function toggleContextUsagePopover(event) {
  event.stopPropagation();
  const popover = $('#contextUsagePopover');
  const button = $('#contextUsageButton');
  const open = popover.hidden;
  if (open && popover.parentElement !== document.body) document.body.appendChild(popover);
  popover.hidden = !open;
  button.setAttribute('aria-expanded', String(open));
  if (open) positionContextUsagePopover();
}

export function closeContextUsagePopover() {
  const popover = $('#contextUsagePopover');
  const button = $('#contextUsageButton');
  if (!popover || popover.hidden) return;
  popover.hidden = true;
  button?.setAttribute('aria-expanded', 'false');
}

export function skillMarkup(skills = []) {
  if (!Array.isArray(skills) || !skills.length) return '';
  const parts = [];
  const user = skills.filter((s) => s?.source !== 'auto');
  const auto = skills.filter((s) => s?.source === 'auto');
  if (user.length) parts.push(`已启用 Skill：${user.map((s) => escapeHtml(s?.name || s)).join('、')}`);
  if (auto.length) parts.push(`已自动匹配 Skill：${auto.map((s) => escapeHtml(s?.name || s)).join('、')}`);
  return parts.length ? `<div class="skill-usage">${parts.join('<br>')}</div>` : '';
}

export function sourcesMarkup(sources = []) {
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

// 消息末尾「本轮修改的文件」总结。桌面端文件名可点 → 打开右侧文件面板；
// 手机端（≤760px）由 CSS + openFilePanel 双重把关，仅展示、不可点。
export const FILE_CHIP_MEDIA_EXTS = new Set(['.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.svg', '.mp4', '.webm', '.mov', '.m4v', '.ogv', '.wav', '.mp3', '.m4a', '.ogg', '.flac']);

export function fileChangesSummaryMarkup(files = []) {
  if (!Array.isArray(files) || !files.length) return '';
  let edited = 0;
  let created = 0;
  for (const f of files) {
    if (!f || !f.path) continue;
    if (f.op === 'edit') edited += 1;
    else created += 1;
  }
  if (!edited && !created) return '';
  const chips = files.map((f) => {
    if (!f || !f.path) return '';
    const raw = String(f.path);
    // 图片/视频/音频等多媒体产物走消息内原有附件卡片预览，不冒充"修改文件"chip
    //（兜底：历史消息 metadata.files 里可能已混入媒体路径）。
    const lower = raw.toLowerCase();
    const dot = lower.lastIndexOf('.');
    if (dot > 0 && FILE_CHIP_MEDIA_EXTS.has(lower.slice(dot))) return '';
    const name = escapeHtml(f.name || raw.replace(/\\/g, '/').split('/').pop() || raw);
    const isEdit = f.op === 'edit';
    return `<button type="button" class="file-change-chip" data-file-op="${isEdit ? 'edit' : 'write'}" data-open-file="${escapeHtml(raw)}" title="${isEdit ? '编辑' : '新建'}：${escapeHtml(raw)}"><span class="file-change-op">${isEdit ? '改' : '新'}</span><span class="file-change-name">${name}</span></button>`;
  }).filter(Boolean).join('');
  if (!chips) return '';
  const opNote = [];
  if (created) opNote.push(`新建 ${created}`);
  if (edited) opNote.push(`编辑 ${edited}`);
  return `<div class="file-changes"><div class="file-changes-label">本轮修改文件${opNote.length ? `（${opNote.join(' · ')}）` : ''}</div><div class="file-changes-list">${chips}</div></div>`;
}

export function toolAvailabilityMarkup(tools = []) {
  if (!Array.isArray(tools) || !tools.length) return '';
  const items = tools.map((tool) => {
    const name = typeof tool === 'string' ? tool : tool?.name;
    if (!name) return '';
    const description = typeof tool === 'string' ? '' : String(tool?.description || '');
    return `<li><code>${escapeHtml(name)}</code>${description ? ` <span>${escapeHtml(description)}</span>` : ''}</li>`;
  }).filter(Boolean).join('');
  return items ? `<details class="tool-availability"><summary>Available tools (${tools.length})</summary><ul>${items}</ul></details>` : '';
}

