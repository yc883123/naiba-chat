// ============================================================
// 10-upload.js —— 拆分自 public/app.js 第 4673-4709 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

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
    return `<span class="file-chip">${file.uploading ? '上传中 · ' : ''}${image}${escapeHtml(file.name)}<button data-remove-file="${index}" title="移除" aria-label="移除"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"></path></svg></button></span>`;
  }).join('');
}

