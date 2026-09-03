import fs from 'node:fs';
import path from 'node:path';

const CDP_PORT = process.env.CDP_PORT || '9333';
const BASE = process.env.BASE || 'http://127.0.0.1:8799';
const OUT = 'D:/naiba-chat/_iso_test3/shots';
fs.mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const list = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json();
const page = list.find((t) => t.type === 'page');
if (!page) throw new Error('no page target');
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => { ws.onopen = r; });

let seq = 0;
const pending = new Map();
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
};
const send = (method, params = {}) => new Promise((res) => {
  const id = ++seq;
  pending.set(id, res);
  ws.send(JSON.stringify({ id, method, params }));
});
async function evalJs(expression) {
  const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 400));
  return r.result?.result?.value;
}
async function shot(name) {
  const r = await send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(path.join(OUT, name), Buffer.from(r.result.data, 'base64'));
  console.log('shot ->', name);
}

await send('Page.enable');
await send('Runtime.enable');
await send('Emulation.setDeviceMetricsOverride', {
  width: 1180, height: 1800, deviceScaleFactor: 1, mobile: false,
});
await send('Page.navigate', { url: BASE });
await sleep(3500);
await evalJs('document.readyState');
async function scrollToScope() {
  await evalJs(`(() => { const el = document.querySelector('#agentToolScope'); if (el) el.scrollIntoView({block:'start'}); return 'ok'; })()`);
  await sleep(150);
}
async function scrollToPresets() {
  await evalJs(`(() => { const el = document.querySelector('#agentToolPresets'); if (el) el.scrollIntoView({block:'start'}); return 'ok'; })()`);
  await sleep(150);
}

// 关掉可能已打开的 dialog，再打开设置 → Agent 面板
await evalJs(`document.querySelectorAll('dialog[open]').forEach(d => d.close()); 'ok'`);
await evalJs(`document.querySelector('#settingsDialog').showModal(); 'ok'`);
await evalJs(`document.querySelectorAll('[data-settings-panel]').forEach(s => { s.hidden = s.dataset.settingsPanel !== 'agent'; }); 'ok'`);
await sleep(600);
await shot('01-agent-list.png');

// 新增 Agent：默认标准模式，分类全部折叠
await evalJs(`document.querySelector('#addAgent').click(); 'ok'`);
await sleep(1200);
await scrollToPresets();
await shot('02-new-collapsed.png');
console.log('  新建默认:', await evalJs(`document.querySelector('#agentToolPresetState').textContent + ' | ' + document.querySelector('#agentToolCount').textContent`));

// 点 ComfyUI 联动预设
await evalJs(`document.querySelector('[data-preset="comfyui"]').click(); 'ok'`);
await sleep(500);
await scrollToPresets();
await shot('03-preset-comfyui.png');
console.log('  点预设后:', await evalJs(`document.querySelector('#agentToolPresetState').textContent + ' | ' + document.querySelector('#agentToolCount').textContent`));

// 展开全部
await evalJs(`document.querySelector('#toggleAllToolGroups').click(); 'ok'`);
await sleep(400);
await scrollToScope();
await shot('04-expand-all.png');

// 点极简模式
await evalJs(`document.querySelector('[data-preset="minimal"]').click(); 'ok'`);
await sleep(400);
await scrollToPresets();
await shot('05-preset-minimal.png');
console.log('  极简后:', await evalJs(`document.querySelector('#agentToolPresetState').textContent + ' | ' + document.querySelector('#agentToolCount').textContent`));

// 取消表单
await evalJs(`document.querySelector('#cancelAgent').click(); 'ok'`);
await sleep(400);

// 编辑旧 Agent「1」
await evalJs(`document.querySelector('[data-agent-edit="1"]').click(); 'ok'`);
await sleep(1200);
await scrollToPresets();
await shot('06-legacy-agent-1.png');
console.log('  旧 Agent 1:', await evalJs(`document.querySelector('#agentToolPresetState').textContent + ' | ' + document.querySelector('#agentToolCount').textContent`));
console.log('  unknown 提示:', await evalJs(`(() => { const el = document.querySelector('#agentToolUnknownHint'); return el.hidden ? '(隐藏)' : el.textContent; })()`));
console.log('  未注册工具:', await evalJs(`JSON.stringify(state.agentFormUnknownTools)`));

await evalJs(`document.querySelector('#saveAgentForm').click(); 'ok'`);
await sleep(1500);
console.log('  保存后 err:', await evalJs(`document.querySelector('#agentError').textContent || '(无错误)'`));

// 编辑 general（不限制）
await evalJs(`document.querySelector('[data-agent-edit="general"]').click(); 'ok'`);
await sleep(1200);
await scrollToPresets();
await shot('07-legacy-unrestricted.png');
console.log('  general:', await evalJs(`document.querySelector('#agentToolPresetState').textContent + ' | ' + document.querySelector('#agentToolCount').textContent`));
await evalJs(`document.querySelector('#saveAgentForm').click(); 'ok'`);
await sleep(1500);
console.log('  保存后 err:', await evalJs(`document.querySelector('#agentError').textContent || '(无错误)'`));

ws.close();
console.log('DONE');
