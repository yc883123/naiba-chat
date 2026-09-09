// 在真实运行实例 8765 上截取当前 UI 作为证据（复用 9222 CDP 通道）
const CDP = 'http://127.0.0.1:9222';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// 从本文件位置派生项目根（禁止硬编码本机盘符路径）。
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SHOTS = path.join(ROOT, '_iso_test', 'shots');

async function main() {
  const list = await (await fetch(`${CDP}/json/list`)).json();
  const page = list.find((t) => t.type === 'page');
  if (!page) throw new Error('no page target');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r) => (ws.onopen = r));
  let id = 0;
  const pending = new Map();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  };
  const send = (method, params = {}) => new Promise((res) => {
    const mid = ++id; pending.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params }));
  });
  await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: 'http://127.0.0.1:8765/?fresh=shot' });
  await new Promise((r) => setTimeout(r, 4000));
  const shot = await send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(path.join(SHOTS, 'live-8765-topbar.png'), Buffer.from(shot.result.data, 'base64'));
  const info = await send('Runtime.evaluate', { expression: `JSON.stringify({ url: location.href, hasExpand: !!document.getElementById('expandSidebar'), hasFileBtn: !!document.getElementById('openFileTabs'), hasCollapse: !!document.getElementById('collapseSidebar'), convRows: document.querySelectorAll('.conversation-row, .conv-row, .conversation-item').length })`, returnByValue: true });
  console.log('PAGE:', info.result.result.value);
  console.log('SHOT_SAVED');
  ws.close();
}
main().catch((e) => { console.error('ERR', e.message); process.exit(1); });
