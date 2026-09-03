// 2026-09-02 UI 回归：右侧文件面板 + 消息末尾"修改文件"总结 + 侧栏虚拟化滚动修复。
// 依赖：headless Chrome --remote-debugging-port=9222 + 隔离实例 http://127.0.0.1:8788
import fs from 'node:fs';
import path from 'node:path';

const OUT = 'd:/naiba-chat/_iso_test2/shots';
fs.mkdirSync(OUT, { recursive: true });

const res = await fetch('http://127.0.0.1:9222/json');
const targets = await res.json();
const target = targets.find(t => t.type === 'page') || targets[0];
const ws = new WebSocket(target.webSocketDebuggerUrl);
let seq = 0;
const pending = new Map();
ws.addEventListener('message', (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); p.resolve(m); }
});
await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', j); });
const send = (method, params = {}, sessionId) => {
  seq++;
  return new Promise((resolve, reject) => {
    const id = seq;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
};
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const { result: { targetInfos } } = await send('Target.getTargets');
const page = targetInfos.find(t => t.type === 'page');
const { result: { sessionId } } = await send('Target.attachToTarget', { targetId: page.targetId, flatten: true });
const sess = (method, params = {}) => send(method, params, sessionId);

await sess('Page.enable');
await sess('Runtime.enable');

async function setMetrics(w, h, mobile = false) {
  await sess('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile });
  await sleep(120);
}
async function navigate(url) {
  const loadPromise = new Promise(resolve => {
    const onMsg = (e) => {
      const m = JSON.parse(e.data);
      if (m.method === 'Page.loadEventFired' && (!m.sessionId || m.sessionId === sessionId)) { ws.removeEventListener('message', onMsg); resolve(); }
    };
    ws.addEventListener('message', onMsg);
    setTimeout(resolve, 9000);
  });
  await sess('Page.navigate', { url });
  await loadPromise;
  await sleep(1500);
}
async function ev(expr) {
  const r = await sess('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) console.error('EVAL ERR:', expr.slice(0, 110), JSON.stringify(r.result.exceptionDetails).slice(0, 220));
  return r.result?.result?.value;
}
async function waitFor(expr, timeout = 8000, label = expr) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    try { if (await ev(expr)) return true; } catch { /* ignore */ }
    await sleep(150);
  }
  console.error('TIMEOUT waiting', label);
  return false;
}
async function shot(file) {
  const r = await sess('Page.captureScreenshot', { format: 'png' });
  const out = path.join(OUT, file);
  fs.writeFileSync(out, Buffer.from(r.result.data, 'base64'));
  console.log('shot ->', file, fs.statSync(out).size, 'B');
}

const URL = 'http://127.0.0.1:8788/';

// ============ A. 桌面：消息总结 + 右侧文件面板 ============
await setMetrics(1600, 1000, false);
await navigate(URL);
await waitFor(`document.querySelectorAll('.conversation-item').length >= 47`, 10000, 'sidebar 47 rows');
console.log('sidebar rows:', await ev(`document.querySelectorAll('.conversation-item').length`));

// 打开目标会话
const opened = await ev(`(() => {
  const row = [...document.querySelectorAll('.conversation-item')]
    .find(el => (el.textContent || '').includes('UI 回归：修改文件与右侧面板'));
  if (!row) return 'NO_ROW';
  row.querySelector('.conversation-title, .conversation-name')?.click?.();
  row.click();
  return 'clicked';
})()`);
console.log('open conv:', opened);
await waitFor(`document.querySelectorAll('#messages .message-row').length >= 4`, 8000, 'messages');
await ev(`document.querySelector('#messages').scrollTop = document.querySelector('#messages').scrollHeight; window.dispatchEvent(new Event('scroll'))`);
await sleep(600);
console.log('file-changes blocks:', await ev(`document.querySelectorAll('.file-changes').length`));
console.log('chips:', await ev(`[...document.querySelectorAll('.file-change-chip')].map(b => b.dataset.openFile).join(',')`));
await shot('01-summary-desktop.png');

// 点击 note.md chip → 面板打开 + 富文本预览
await ev(`(() => {
  const chip = [...document.querySelectorAll('.file-change-chip')].find(b => b.dataset.openFile === 'note.md');
  chip?.scrollIntoView({ block: 'center' });
  chip?.click();
})()`);
await waitFor(`document.querySelectorAll('#filePanel .file-tab').length >= 1`, 6000, 'file tab');
await sleep(900);
console.log('panel tabs:', await ev(`[...document.querySelectorAll('#fileTabs .file-tab')].map(t => t.textContent.trim()).join('|')`));
console.log('panel open:', await ev(`document.querySelector('#appShell').classList.contains('file-panel-open')`));
await shot('02-panel-markdown-preview.png');

// 再点 code.py chip → 多 tab
await ev(`[...document.querySelectorAll('.file-change-chip')].find(b => b.dataset.openFile === 'code.py')?.click()`);
await waitFor(`document.querySelectorAll('#fileTabs .file-tab').length >= 2`, 5000, 'two tabs');
await sleep(600);
await shot('03-panel-two-tabs-code.png');

// 打开 rel.txt tab 并进入编辑
await ev(`(() => {
  const tab = [...document.querySelectorAll('#fileTabs .file-tab')].find(t => (t.textContent || '').includes('rel.txt'));
  tab?.click();
})()`);
await waitFor(`document.querySelector('#filePanelBody .file-view-text, #filePanelBody textarea')`, 5000, 'rel preview');
await ev(`document.querySelector('[data-file-edit]')?.click()`);
await waitFor(`document.querySelector('.file-edit-textarea')`, 5000, 'edit textarea');
await sleep(300);
await shot('04-panel-edit-mode.png');

// 修改并保存
await ev(`(() => {
  const ta = document.querySelector('.file-edit-textarea');
  if (!ta) return 'NO_TA';
  ta.value = ta.value + '\\n追加行 L3_NEW';
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  return ta.value.length;
})()`);
await sleep(200);
await ev(`document.querySelector('[data-file-save]')?.click()`);
await sleep(1200);
console.log('dirty dot gone:', await ev(`!document.querySelector('#fileTabs .file-tab.file-tab-dirty')`));
await shot('05-panel-saved.png');

// ============ B. 手机：仅总结文字、不可点、无右侧面板 ============
await setMetrics(390, 844, true);
await navigate(URL);
await waitFor(`document.querySelectorAll('#messages .message-row').length >= 4 || document.querySelectorAll('.conversation-item').length > 0`, 9000, 'mobile conv');
// 手机上侧栏默认隐藏，直接等默认会话（第一条即目标）加载完消息
await waitFor(`document.querySelectorAll('#messages .file-changes').length >= 2`, 8000, 'mobile file-changes');
await ev(`document.querySelector('#messages').scrollTop = document.querySelector('#messages').scrollHeight`);
await sleep(500);
console.log('mobile panel visible:', await ev(`getComputedStyle(document.querySelector('#filePanel')).display`));
// 点击 chip 应无效果
const clickResult = await ev(`(() => {
  const chip = document.querySelector('.file-change-chip');
  chip?.click();
  return new Promise(r => setTimeout(() => r(document.querySelector('#appShell').classList.contains('file-panel-open')), 300));
})()`);
console.log('mobile chip click opens panel:', clickResult);
await shot('06-mobile-summary-only.png');

// ============ C. 桌面：侧栏虚拟化滚动回归 ============
await setMetrics(1600, 1000, false);
await navigate(URL);
await waitFor(`document.querySelectorAll('.conversation-item').length >= 40`, 10000, 'rows again');
const v = await ev(`(() => {
  const tree = document.querySelector('#sidebarWorkspaceTree');
  tree.scrollTop = 9999999;                 // 模拟展开后旧 scrollTop 越界
  tree.dispatchEvent(new Event('scroll'));  // rAF 渲染窗口
  return new Promise(res => setTimeout(() => {
    const maxScroll = Math.max(0, tree.scrollHeight - tree.clientHeight);
    const rows = [...tree.querySelectorAll('.conversation-item')];
    res({ scrollTop: tree.scrollTop, maxScroll, rendered: rows.length,
          firstText: rows[0]?.textContent.trim().slice(0, 24) || '' });
  }, 350));
})()`);
console.log('over-scroll clamp:', JSON.stringify(v));
// 点击最末一行 → 应正常打开会话且列表仍在视口内
const bottomClick = await ev(`(() => {
  const rows = [...document.querySelectorAll('#sidebarWorkspaceTree .conversation-item')];
  const last = rows[rows.length - 1];
  last?.click();
  return new Promise(res => setTimeout(() => {
    const t = document.querySelector('#sidebarWorkspaceTree');
    const active = t.querySelector('.conversation-item.active');
    res({ ok: !!active, activeText: active?.textContent.trim().slice(0, 30) || '',
          rowsNow: t.querySelectorAll('.conversation-item').length });
  }, 500));
})()`);
console.log('bottom row click:', JSON.stringify(bottomClick));
await shot('07-sidebar-virtual-bottom.png');
// 回滚到顶部应完整
await ev(`(() => {
  const tree = document.querySelector('#sidebarWorkspaceTree');
  tree.scrollTop = 0;
  tree.dispatchEvent(new Event('scroll'));
})()`);
await sleep(500);
await shot('08-sidebar-virtual-top.png');
console.log('top row text:', await ev(`document.querySelector('#sidebarWorkspaceTree .conversation-item')?.textContent.trim().slice(0, 30)`));

console.log('REGRESSION_DONE');
ws.close();
process.exit(0);
