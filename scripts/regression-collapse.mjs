// 2026-09-02 UI 回归：左右侧栏折叠 / 展开。
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
await sess('Network.enable');
await sess('Network.setCacheDisabled', { cacheDisabled: true });

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

const URL = 'http://127.0.0.1:8788/?reg=collapse&t=' + Date.now();
const shellCols = `getComputedStyle(document.querySelector('#appShell')).gridTemplateColumns`;

await setMetrics(1600, 1000, false);
await navigate(URL);
await waitFor(`document.querySelectorAll('.conversation-item').length >= 10`, 10000, 'sidebar rows');

console.log('--- 初始桌面布局 ---');
console.log('grid:', await ev(shellCols));
console.log('collapse btn visible:', await ev(`(() => { const b = document.querySelector('#collapseSidebar'); return !!b && getComputedStyle(b).display !== 'none'; })()`));
console.log('expand btn visible(应为false):', await ev(`(() => { const b = document.querySelector('#expandSidebar'); return !!b && getComputedStyle(b).display !== 'none'; })()`));

// 1. 收起左侧栏
await ev(`document.querySelector('#collapseSidebar').click()`);
await sleep(300);
console.log('--- 左侧栏收起 ---');
console.log('has class:', await ev(`document.querySelector('#appShell').classList.contains('sidebar-collapsed')`));
console.log('grid:', await ev(shellCols));
console.log('expand btn visible(应为true):', await ev(`(() => { const b = document.querySelector('#expandSidebar'); return !!b && getComputedStyle(b).display !== 'none'; })()`));
console.log('persisted:', await ev(`localStorage.getItem('naibaChatSidebarCollapsed')`));
await shot('collapse-left-folded.png');

// 2. 展开左侧栏
await ev(`document.querySelector('#expandSidebar').click()`);
await sleep(300);
console.log('--- 左侧栏展开 ---');
console.log('has class(应false):', await ev(`document.querySelector('#appShell').classList.contains('sidebar-collapsed')`));
console.log('grid:', await ev(shellCols));
console.log('persisted(应null):', await ev(`localStorage.getItem('naibaChatSidebarCollapsed')`));

// 3. 打开目标会话 -> 点 chip 打开右侧文件面板
const opened = await ev(`(() => {
  const row = [...document.querySelectorAll('.conversation-item')]
    .find(el => (el.textContent || '').includes('UI 回归：修改文件与右侧面板'));
  if (!row) return 'NO_ROW';
  row.click();
  return 'clicked';
})()`);
console.log('open conv:', opened);
await waitFor(`document.querySelectorAll('#messages .file-change-chip').length >= 1`, 8000, 'file chip');
const chipCount = await ev(`document.querySelectorAll('#messages .file-change-chip').length`);
console.log('chips:', chipCount);
await ev(`document.querySelector('#messages .file-change-chip').click()`);
await waitFor(`document.querySelector('#appShell').classList.contains('file-panel-open') && !!document.querySelector('#filePanelBody .file-view')`, 8000, 'panel view');
console.log('--- 右侧面板打开 ---');
console.log('file-panel-open:', await ev(`document.querySelector('#appShell').classList.contains('file-panel-open')`));
console.log('grid:', await ev(shellCols));
console.log('文件重开按钮 hidden(打开时应hidden):', await ev(`document.querySelector('#openFileTabs').hidden`));
await shot('collapse-right-open.png');

// 4. 收起右侧面板（保留标签）
await ev(`document.querySelector('#closeFilePanel').click()`);
await sleep(300);
console.log('--- 右侧面板收起 ---');
console.log('file-panel-open(应false):', await ev(`document.querySelector('#appShell').classList.contains('file-panel-open')`));
console.log('tabs 保留:', await ev(`window.filePanelState?.tabs?.length`));
console.log('grid:', await ev(shellCols));
console.log('文件重开按钮显示(应true):', await ev(`!document.querySelector('#openFileTabs').hidden`));
console.log('计数:', await ev(`document.querySelector('#fileTabsCount')?.textContent`));
await shot('collapse-right-folded.png');

// 5. 顶栏「文件 N」重新展开右侧面板
await ev(`document.querySelector('#openFileTabs').click()`);
await waitFor(`document.querySelector('#appShell').classList.contains('file-panel-open')`, 8000, 'reopen panel');
console.log('--- 重开右侧面板 ---');
console.log('file-panel-open(应true):', await ev(`document.querySelector('#appShell').classList.contains('file-panel-open')`));

// 6. 双栏都收：左侧收起 + 右侧打开并存
await ev(`document.querySelector('#collapseSidebar').click()`);
await sleep(300);
console.log('--- 左右同收（右开左收） ---');
console.log('sidebar-collapsed:', await ev(`document.querySelector('#appShell').classList.contains('sidebar-collapsed')`));
console.log('grid:', await ev(shellCols));
await shot('collapse-both-open-right.png');
await ev(`document.querySelector('#expandSidebar').click()`);
await sleep(200);

console.log('DONE');
process.exit(0);
