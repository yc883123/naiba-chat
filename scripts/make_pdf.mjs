// 用已启动的无头 Chrome（CDP :9222）把 docs/manual/index.html 打成 A4 PDF。
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const HTML = path.join(ROOT, 'docs', 'manual', 'index.html');
const OUT = path.join(ROOT, 'docs', 'manual', 'Naiba-Chat-手册-2.1.1.pdf');

const res = await fetch('http://127.0.0.1:9222/json/version');
const ver = await res.json();
const ws = new WebSocket(ver.webSocketDebuggerUrl);
let seq = 0; const pending = new Map();
ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
const rawSend = (method, params = {}, sessionId) => { seq++; const msg = { id: seq, method, params }; if (sessionId) msg.sessionId = sessionId; const p = new Promise((resolve) => pending.set(seq, resolve)); ws.send(JSON.stringify(msg)); return p; };
const withTimeout = (p, ms, label) => Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error('TIMEOUT ' + label)), ms))]);
const send = (m, p = {}, s) => withTimeout(rawSend(m, p, s), 90000, m);
// 新开一个标签页做打印，不干扰已有页面；打完关闭
const { result: { targetId } } = await send('Target.createTarget', { url: 'about:blank' });
const { result: { sessionId } } = await send('Target.attachToTarget', { targetId, flatten: true });
const sess = (m, p = {}) => send(m, p, sessionId);
try {
  await sess('Page.enable');
  await sess('Emulation.setDeviceMetricsOverride', { width: 1200, height: 1600, deviceScaleFactor: 1, mobile: false });
  await sess('Page.navigate', { url: 'file:///' + HTML.replace(/\\/g, '/') });
  await new Promise((r) => setTimeout(r, 5000));
  const { result } = await sess('Page.printToPDF', {
    printBackground: true,
    paperWidth: 8.27, paperHeight: 11.69,   // A4
    marginTop: 0.5, marginBottom: 0.5, marginLeft: 0.4, marginRight: 0.4,
    scale: 0.85,
  });
  fs.writeFileSync(OUT, Buffer.from(result.data, 'base64'));
  console.log('wrote', OUT, (fs.statSync(OUT).size / 1024 / 1024).toFixed(2), 'MB');
} finally {
  await send('Target.closeTarget', { targetId }).catch(() => {});
}
process.exit(0);
