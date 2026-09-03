// 验证：顶栏「文件 N」按钮仅在有已点开文件标签时显示（旧逻辑）
// 取代旧的 _probe_reopen.mjs（常驻入口版本已废弃），对应 8788 隔离实例（_iso_test2）。
// 1) 空会话/未点 chip → 按钮隐藏（不常驻）
// 2) 点击消息里的文件 chip → 面板打开、按钮在面板打开期间隐藏
// 3) 收起面板 → 按钮出现（带计数）
// 4) 关闭全部标签 → 按钮消失
import fs from 'node:fs';
const CDP = 'http://127.0.0.1:9222';
const URL = 'http://127.0.0.1:8788/?reopen=2';
const SHOTS = 'D:/naiba-chat/_iso_test2/shots';
fs.mkdirSync(SHOTS, { recursive: true });

async function getWs() {
  const list = await (await fetch(`${CDP}/json/list`)).json();
  const page = list.find((t) => t.type === 'page');
  if (!page) throw new Error('no page target');
  return page.webSocketDebuggerUrl;
}

async function main() {
  const ws = new WebSocket(await getWs());
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let seq = 0; const pending = new Map();
  ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
  const send = (method, params = {}) => new Promise((res) => { const id = ++seq; pending.set(id, res); ws.send(JSON.stringify({ id, method, params })); });
  const s = send.bind(null);
  await s('Page.enable'); await s('Runtime.enable');
  await s('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const shot = async (name) => {
    const r = await s('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(`${SHOTS}/${name}`, Buffer.from(r.result.data, 'base64'));
  };
  const ev = async (expression) => {
    const r = await s('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (r.result?.exceptionDetails) return { err: (r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text).slice(0, 300) };
    return r.result?.result?.value;
  };

  await s('Page.navigate', { url: URL });
  await wait(2600);
  await ev(`localStorage.removeItem('naibaChatSidebarCollapsed'); localStorage.removeItem('naibaChatFilePanelOpen'); true`);
  await wait(300);

  // 打开包含文件 chip 的会话（seed：第一/二条会话）
  await ev(`(() => { const rows = document.querySelectorAll('#sidebarWorkspaceTree .conversation-item'); if (!rows.length) return 'no-rows'; rows[0].click(); return 'ok'; })()`);
  await wait(2400);

  // 步骤1：空标签 → 按钮应隐藏（旧逻辑核心：不常驻）
  const st1 = await ev(`(() => {
    const b = document.getElementById('openFileTabs');
    if (!b) return { err: 'no button' };
    const br = b.getBoundingClientRect();
    return { visible: !b.hidden && br.width > 0, hasTabs: b.classList.contains('has-tabs'),
             panelOpen: document.getElementById('appShell').classList.contains('file-panel-open') };
  })()`);
  console.log('[1 empty]', JSON.stringify(st1));
  await shot('reopen2-empty-hidden.png');

  // 步骤2：点击消息里的文件 chip → 面板打开，按钮随面板打开而隐藏
  const st2 = await ev(`(() => {
    const chip = document.querySelector('.file-change-chip');
    if (!chip) return { err: 'no file chip in view' };
    chip.click();
    return 'clicked';
  })()`);
  console.log('[2 chip click]', JSON.stringify(st2));
  await wait(900);
  const st2b = await ev(`(() => {
    const b = document.getElementById('openFileTabs');
    const br = b.getBoundingClientRect();
    return { panelOpen: document.getElementById('appShell').classList.contains('file-panel-open'),
             btnVisible: !b.hidden && br.width > 0, tabCount: filePanelState ? filePanelState.tabs.length : -1,
             tabNames: filePanelState ? filePanelState.tabs.map(t => t.name) : [] };
  })()`);
  console.log('[2 after chip]', JSON.stringify(st2b));
  await shot('reopen2-panel-open.png');

  // 步骤3：收起面板 → 按钮应出现且带计数
  await ev(`closeFilePanel ? closeFilePanel() : null; true`);
  await wait(500);
  const st3 = await ev(`(() => {
    const b = document.getElementById('openFileTabs');
    const c = document.getElementById('fileTabsCount');
    const br = b.getBoundingClientRect();
    return { panelOpen: document.getElementById('appShell').classList.contains('file-panel-open'),
             btnVisible: !b.hidden && br.width > 0, countText: c ? c.textContent : null, countHidden: c ? c.hidden : null,
             title: b.title };
  })()`);
  console.log('[3 after close]', JSON.stringify(st3));
  await shot('reopen2-btn-with-tab.png');

  // 步骤4：点按钮重开面板（重开入口生效）
  await ev(`document.getElementById('openFileTabs').click(); true`);
  await wait(600);
  const st4 = await ev(`({ panelOpen: document.getElementById('appShell').classList.contains('file-panel-open'), tabActive: !!document.querySelector('#fileTabs .file-tab') })`);
  console.log('[4 reopen click]', JSON.stringify(st4));
  await shot('reopen2-reopened.png');

  // 步骤5：关闭全部标签 → 面板自动收起且按钮消失
  await ev(`(() => { if (typeof filePanelState !== 'undefined' && filePanelState.tabs.length) { [...filePanelState.tabs].forEach(t => removeFileTab ? removeFileTab(t.key) : null); } return 'ok'; })()`);
  await wait(500);
  const st5 = await ev(`(() => {
    const b = document.getElementById('openFileTabs');
    const br = b.getBoundingClientRect();
    return { panelOpen: document.getElementById('appShell').classList.contains('file-panel-open'),
             btnVisible: !b.hidden && br.width > 0, tabsLeft: filePanelState ? filePanelState.tabs.length : -1 };
  })()`);
  console.log('[5 all closed]', JSON.stringify(st5));
  await shot('reopen2-gone.png');

  ws.close(); process.exit(0);
}
main().catch((e) => { console.error('ERR', e); process.exit(1); });
