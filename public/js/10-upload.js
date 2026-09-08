// ============================================================
// 10-upload.js —— 拆分自 public/app.js 第 4673-4709 行（阶段 5.1 按域拆分，跨文件引用零改动）
// 2026-09 上传系统优化：XHR multipart 流式上传（进度/取消/并发池/前置校验）。
// ============================================================

import { $, escapeHtml, state, toast } from "./01-core.js";
import { attachmentThumbUrl, fileUrl, updateSendButtonState } from "./03-media.js";

// 与服务端 UPLOAD_MAX_BYTES 一致的前置校验上限（超限直接拦截，不发起请求）。
export const UPLOAD_MAX_BYTES = 80 * 1024 * 1024;
// 并发上传上限：多文件同时传完更快，又不会打满连接。
const UPLOAD_CONCURRENCY = 3;

export function uploadFiles(files) {
  const valid = [];
  for (const file of files) {
    if (file.size > UPLOAD_MAX_BYTES) {
      toast(`「${file.name}」超过 80MB，已跳过`);
      continue;
    }
    valid.push(file);
  }
  if (!valid.length) return;
  // 并发池：每次完成一个立即补位，保持最多 UPLOAD_CONCURRENCY 个在传。
  let index = 0;
  const runNext = () => {
    if (index >= valid.length) return;
    const file = valid[index++];
    uploadOne(file).finally(runNext);
  };
  const startCount = Math.min(UPLOAD_CONCURRENCY, valid.length);
  for (let i = 0; i < startCount; i++) runNext();
}

function uploadOne(file) {
  const chip = { name: file.name, uploading: true, progress: 0 };
  state.pendingFiles.push(chip);
  renderPendingFiles();
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    // 取消句柄：chip 移除时由 renderPendingFiles 的移除按钮调用。
    chip.cancel = () => {
      try { xhr.abort(); } catch (_) { /* 已结束 */ }
    };
    xhr.open('POST', '/api/uploads');
    if (state.token) xhr.setRequestHeader('Authorization', `Bearer ${state.token}`);
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      chip.progress = Math.round((event.loaded / event.total) * 100);
      renderPendingFiles();
    };
    xhr.onload = () => {
      chip.cancel = null;
      let payload = {};
      try { payload = JSON.parse(xhr.responseText || '{}'); } catch (_) { /* 非 JSON 错误体 */ }
      if (xhr.status >= 200 && xhr.status < 300 && payload.path) {
        Object.assign(chip, payload, { uploading: false, progress: 100 });
      } else {
        state.pendingFiles = state.pendingFiles.filter((item) => item !== chip);
        toast(`上传失败：${payload.error || `HTTP ${xhr.status}`}`);
      }
      renderPendingFiles();
      resolve();
    };
    xhr.onerror = () => {
      chip.cancel = null;
      state.pendingFiles = state.pendingFiles.filter((item) => item !== chip);
      toast(`上传失败：网络错误`);
      renderPendingFiles();
      resolve();
    };
    xhr.onabort = () => {
      chip.cancel = null;
      resolve();
    };
    const form = new FormData();
    form.append('file', file, file.name);
    xhr.send(form);
  });
}

export function readAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

export function renderPendingFiles() {
  $('#pendingFiles').innerHTML = state.pendingFiles.map((file, index) => {
    const isImage = !!(file.path || file.thumb_path) && /\.(png|jpe?g|webp|gif)$/i.test(file.path || file.name || '');
    // 上传中/无 path 时不渲染缩略图（旧逻辑会请求空路径 /api/file?path= → 404 破图）。
    const thumbUrl = file.path ? attachmentThumbUrl(file) : '';
    const image = (isImage && thumbUrl)
      ? `<img class="thumbnail" src="${escapeHtml(thumbUrl)}" alt="" draggable="false" data-large-url="${escapeHtml(fileUrl(file.path))}">`
      : '';
    const status = file.uploading
      ? `上传中${file.progress > 0 ? ` · ${file.progress}%` : ''} · `
      : '';
    return `<span class="file-chip">${status}${image}${escapeHtml(file.name)}<button data-remove-file="${index}" title="移除" aria-label="移除"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg></button></span>`;
  }).join('');
  // 待发送附件增减直接决定"能否发送"（纯附件轮次合法）：单点刷新发送按钮状态。
  updateSendButtonState();
}
