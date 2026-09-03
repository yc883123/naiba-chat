// 2026-09-02 UI 回归 — 补 #6：左侧栏虚拟化滚动修复 + 手机端总结快照。
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
  await sleep(150);
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

const URL = 'http://127.0.0.1:8788/?reg=virt&t=' + Date.now();

console.log('--- A. 桌面：左侧栏虚拟化滚动修复验证 ---');
await setMetrics(1600, 1000, false);
await navigate(URL);
await waitFor("document.querySelectorAll('.conversation-item, .workspace-showmore').length > 0", 12000, 'sidebar rows');
await ev("localStorage.removeItem('naibaChatSidebarCollapsed'); localStorage.setItem('naibaChatSidebarW','240px'); document.documentElement.style.setProperty('--sidebar-w','240px')");
await ev("location.reload()");
await sleep(1800);
await waitFor("document.querySelectorAll('.conversation-item, .workspace-showmore').length > 0", 12000, 'after reload');

const expanded = await ev(`(async () => {
  for (let i = 0; i < 4; i++) {
    const btns = [...document.querySelectorAll('.workspace-showmore')];
    if (!btns.length) break;
    btns[0].click();
    await new Promise(r => setTimeout(r, 220));
  }
  return document.querySelectorAll('.conversation-item').length;
})()`);
console.log('展开后会话行数:', expanded);

const meta = await ev(`(() => {
  const tree = document.querySelector('#sidebarWorkspaceTree');
  const win = tree.querySelector('.sidebar-virtual-window');
  return {
    scrollHeight: tree.scrollHeight,
    clientHeight: tree.clientHeight,
    scrollTop: tree.scrollTop,
    maxScroll: tree.scrollHeight - tree.clientHeight,
    windowTop: win ? win.style.top : null,
    windowH: win ? win.parentElement.style.height : null,
    renderedRows: tree.querySelectorAll('.conversation-item, .workspace-showmore').length,
  };
})()`);
console.log('初始元数据:', meta);
await shot('virt-desktop-initial.png');

await ev(`(() => {
  const tree = document.querySelector('#sidebarWorkspaceTree');
  tree.scrollTop = 9999999;
  tree.dispatchEvent(new Event('scroll'));
})()`);
await sleep(350);
const afterOver = await ev(`(() => {
  const tree = document.querySelector('#sidebarWorkspaceTree');
  const win = tree.querySelector('.sidebar-virtual-window');
  return {
    scrollTop: tree.scrollTop,
    scrollHeight: tree.scrollHeight,
    clientHeight: tree.clientHeight,
    windowTop: win ? win.style.top : null,
    renderedRows: tree.querySelectorAll('.conversation-item, .workspace-showmore').length,
  };
})()`);
console.log('超界后:', afterOver);
if (afterOver.scrollTop > afterOver.scrollHeight - afterOver.clientHeight + 1) {
  console.error('!! scrollTop 越界未钳制');
} else {
  console.log('OK: scrollTop 已钳制到 maxScroll');
}
await shot('virt-desktop-overscroll-clamped.png');

await ev(`(() => {
  const tree = document.querySelector('#sidebarWorkspaceTree');
  tree.scrollTop = (tree.scrollHeight - tree.clientHeight) - 80;
  tree.dispatchEvent(new Event('scroll'));
})()`);
await sleep(250);

const beforeClick = await ev(`(() => {
  const tree = document.querySelector('#sidebarWorkspaceTree');
  return {
    renderedRows: tree.querySelectorAll('.conversation-item, .workspace-showmore').length,
    firstText: tree.querySelector('.conversation-item')?.textContent?.trim().slice(0, 30) || null,
    lastText: [...tree.querySelectorAll('.conversation-item')].slice(-1)[0]?.textContent?.trim().slice(0, 30) || null,
  };
})()`);
console.log('点击前:', beforeClick);
await shot('virt-desktop-bottom-preclick.png');

const clicked = await ev(`(() => {
  const tree = document.querySelector('#sidebarWorkspaceTree');
  const rows = [...tree.querySelectorAll('.conversation-item')];
  const last = rows[rows.length - 1];
  if (!last) return 'NO_LAST';
  last.click();
  return last.dataset.conversationId || last.getAttribute('data-conversation-id') || 'CLICKED';
})()`);
console.log('点击最后一行:', clicked);
await sleep(1200);
const afterClick = await ev(`(() => {
  const tree = document.querySelector('#sidebarWorkspaceTree');
  return {
    scrollTop: tree.scrollTop,
    scrollHeight: tree.scrollHeight,
    clientHeight: tree.clientHeight,
    renderedRows: tree.querySelectorAll('.conversation-item, .workspace-showmore').length,
    activeId: tree.querySelector('.conversation-item.active')?.dataset?.conversationId || null,
    msgCount: document.querySelectorAll('#messages .message-row').length,
  };
})()`);
console.log('点击后:', afterClick);
if (afterClick.activeId !== clicked) console.error('!! active 会话不匹配');
if (afterClick.scrollTop > afterClick.scrollHeight - afterClick.clientHeight + 1) console.error('!! 滚动条越界');
else console.log('OK: 点击末尾行后 scrollTop 仍在合法范围');
await shot('virt-desktop-bottom-afterclick.png');

await ev(`(() => {
  const tree = document.querySelector('#sidebarWorkspaceTree');
  tree.scrollTop = 0;
  tree.dispatchEvent(new Event('scroll'));
})()`);
await sleep(300);
await ev(`(() => {
  const tree = document.querySelector('#sidebarWorkspaceTree');
  const first = tree.querySelectorAll('.conversation-item')[0];
  if (first) first.click();
})()`);
await sleep(800);
const backTop = await ev(`(() => {
  const tree = document.querySelector('#sidebarWorkspaceTree');
  return {
    scrollTop: tree.scrollTop,
    scrollHeight: tree.scrollHeight,
    clientHeight: tree.clientHeight,
    activeId: tree.querySelector('.conversation-item.active')?.dataset?.conversationId || null,
  };
})()`);
console.log('回到顶部点击首行后:', backTop);
await shot('virt-desktop-top-afterclick.png');

console.log('--- B. 手机端：消息末尾总结仅文字、不可点 ---');
await setMetrics(375, 812, true);
await navigate(URL);
await waitFor("document.querySelectorAll('#messages .file-change-chip').length >= 1", 12000, 'mobile chip');
const mobile = await ev(`(() => {
  const chip = document.querySelector('#messages .file-change-chip');
  return {
    pointerEvents: chip ? getComputedStyle(chip).pointerEvents : null,
    background: chip ? getComputedStyle(chip).backgroundColor : null,
    border: chip ? getComputedStyle(chip).borderTopWidth : null,
    panelDisplay: document.querySelector('#filePanel') ? getComputedStyle(document.querySelector('#filePanel')).display : null,
    openTabsHidden: document.querySelector('#openFileTabs') ? document.querySelector('#openFileTabs').hidden : null,
    expandDisplay: document.querySelector('#expandSidebar') ? getComputedStyle(document.querySelector('#expandSidebar')).display : null,
    collapseDisplay: document.querySelector('#collapseSidebar') ? getComputedStyle(document.querySelector('#collapseSidebar')).display : null,
  };
})()`);
console.log('手机端状态:', mobile);
await shot('virt-mobile-summary.png');

console.log('DONE');
process.exit(0);
