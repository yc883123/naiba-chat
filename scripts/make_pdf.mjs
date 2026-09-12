// 把 docs/manual/index.html 打印成 A4 PDF（走 CDP）。
//
// 关键结论（2026-09-12，踩了很多次，别退回去）：
//   * `chrome --print-to-pdf` 在本机已变成「静默失败」：进程 0.08s 退出、不产出文件、无任何输出。
//     不要再把它当主通道。
//   * CDP 的 `Page.printToPDF` 默认把整个 PDF 当 **base64 塞进一条 CDP 消息**返回。本手册约 5 MB，
//     一旦超过某个量级，这个调用会**无限挂起**（实测 300s 不返回，且会把整个渲染器带死，后续
//     连 Page.navigate 都超时）。与页面里的打印 CSS 无关，纯粹是回传路径的问题。
//     分界线实测：46 个 <img> 里前 25 张能过、32 张起必挂。
//   * 正解：`transferMode: 'ReturnAsStream'` + `IO.read` 分块取。实测全量 46 张图 6.7s 完成。
//
// 用法：node scripts/make_pdf.mjs
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const HTML = path.join(ROOT, 'docs', 'manual', 'index.html');

// 版本号从 README 标题派生（与 build_html.py 同口径），避免 PDF 文件名停在旧版本。
const README = path.join(ROOT, 'docs', 'manual', 'README.md');
const m = fs.readFileSync(README, 'utf-8').match(/^#\s+.*?（(\d+\.\d+\.\d+)\s+Beta）/m);
const VERSION = process.env.NAIBA_PDF_VERSION || (m ? m[1] : '');
if (!VERSION) throw new Error('无法从 docs/manual/README.md 标题解析版本号');
const OUT = path.join(ROOT, 'docs', 'manual', `Naiba-Chat-手册-${VERSION}.pdf`);
const URL = `file:///${HTML.replace(/\\/g, '/')}`;

const CHROME = process.env.NAIBA_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PORT = Number(process.env.NAIBA_CDP_PORT || 9223);
const PROFILE = path.join(os.tmpdir(), `naiba-pdf-${Date.now()}`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const chrome = spawn(CHROME, [
  '--headless=new', '--no-sandbox', '--disable-gpu',
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${PROFILE.replace(/\\/g, '/')}`,
  '--no-first-run', '--no-default-browser-check',
], { stdio: 'ignore' });

let ws;
let ok = false;
try {
  let ver = null;
  for (let i = 0; i < 40 && !ver; i += 1) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (r.ok) ver = await r.json();
    } catch (_) { /* 还没起来 */ }
    if (!ver) await sleep(500);
  }
  if (!ver) throw new Error('CDP 未就绪');

  ws = new WebSocket(ver.webSocketDebuggerUrl);
  let seq = 0;
  const pending = new Map();
  ws.addEventListener('message', (e) => {
    const msg = JSON.parse(e.data);
    if (msg.id && pending.has(msg.id)) { const p = pending.get(msg.id); pending.delete(msg.id); p(msg); }
  });
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });

  const send = (method, params = {}, sessionId, timeoutMs = 20000) => {
    seq += 1;
    const msg = { id: seq, method, params };
    if (sessionId) msg.sessionId = sessionId;
    return Promise.race([
      new Promise((res) => { pending.set(seq, res); ws.send(JSON.stringify(msg)); }),
      new Promise((_, rej) => setTimeout(() => rej(new Error(`TIMEOUT ${method}`)), timeoutMs)),
    ]);
  };

  const { result: { targetId } } = await send('Target.createTarget', { url: 'about:blank' }, undefined, 30000);
  const { result: { sessionId } } = await send('Target.attachToTarget', { targetId, flatten: true }, undefined, 30000);
  await send('Page.enable', {}, sessionId, 30000);
  await send('Page.navigate', { url: URL }, sessionId, 60000);
  await sleep(4000); // 等图片解码完，否则 PDF 里会缺图

  const printed = await send('Page.printToPDF', {
    printBackground: true,
    paperWidth: 8.27, paperHeight: 11.69,          // A4
    marginTop: 0.4, marginBottom: 0.4, marginLeft: 0.4, marginRight: 0.4,
    scale: 0.85,
    transferMode: 'ReturnAsStream',                // ← 关键：绕开大 base64 回传导致的挂起
  }, sessionId, 180000);

  const stream = printed?.result?.stream;
  if (!stream) throw new Error('printToPDF 未返回流：' + JSON.stringify(printed).slice(0, 300));

  const chunks = [];
  for (let i = 0; i < 10000; i += 1) {
    const r = await send('IO.read', { handle: stream, size: 1 << 20 }, sessionId, 60000);
    const d = r?.result;
    if (!d) throw new Error('IO.read 无结果：' + JSON.stringify(r).slice(0, 200));
    if (d.data) chunks.push(Buffer.from(d.data, d.base64Encoded ? 'base64' : 'utf8'));
    if (d.eof) break;
  }
  await send('IO.close', { handle: stream }, sessionId, 20000).catch(() => {});

  const buf = Buffer.concat(chunks);
  if (buf.length < 10000) throw new Error('PDF 过小，疑似失败：' + buf.length + ' 字节');
  fs.writeFileSync(OUT, buf);
  ok = true;
  console.log('wrote', OUT, (buf.length / 1024 / 1024).toFixed(2), 'MB');
} finally {
  try { ws?.close(); } catch (_) {}
  try { chrome.kill(); } catch (_) {}
  await sleep(600);
  try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch (_) {}
}

if (!ok) throw new Error('PDF 未生成：' + OUT);
process.exit(0);
