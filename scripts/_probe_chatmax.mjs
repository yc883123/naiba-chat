// 一次性验证：折叠左栏前后，中间消息内容列是否真实变宽（桌面 1440）
import fs from 'node:fs';
const CDP = 'http://127.0.0.1:9222';
const SHOTS = 'D:/naiba-chat/_iso_test2/shots';

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

  await s('Page.navigate', { url: 'http://127.0.0.1:8788/?w=1' });
  await wait(2600);

  const ev = async (expression) => {
    const r = await s('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (r.result?.exceptionDetails) return { err: (r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text).slice(0, 300) };
    return r.result?.result?.value;
  };

  // 清掉持久化折叠/会话状态，恢复默认
  await ev(`localStorage.removeItem('naibaChatSidebarCollapsed'); true`);
  await wait(300);

  // 打开一个会话：点击侧栏第一条
  const clicked = await ev(`(() => {
    const rows = document.querySelectorAll('#sidebarWorkspaceTree .conversation-item');
    if (!rows.length) return 'no-rows';
    rows[0].click(); return 'clicked';
  })()`);
  await wait(2200);

  // 注入一条长文本助手消息（仅 DOM，模拟排版效果）
  const injected = await ev(`(() => {
    const msgs = document.getElementById('messages');
    if (!msgs) return 'no-messages';
    const longText = '这是一条用来测量内容列宽度的长文本消息，'.repeat(20)
      + '本段用于模拟真实助手回复中较宽的 markdown 排版效果，以便肉眼对比左右侧栏开合时中间栏的宽度变化。';
    const wrap = document.createElement('div');
    wrap.className = 'message-row';
    wrap.innerHTML = '<div class="message-avatar">AI</div><div class="message-body"><div class="answer-content"><p>' + longText + '</p><p>第二条段落：验证换行与留白是否跟随容器宽度变化。'.repeat(8) + '</p></div></div>';
    msgs.appendChild(wrap);
    wrap.scrollIntoView({ block: 'center' });
    return 'injected';
  })()`);
  await wait(600);

  const measure = `(() => {
    const m = document.querySelector('.messages');
    if (!m) return null;
    const cs = getComputedStyle(m);
    const rows = [...document.querySelectorAll('.message-row')];
    const last = rows.length ? rows[rows.length-1].querySelector('.message-body') : null;
    const r = last ? last.getBoundingClientRect() : null;
    return {
      messagesW: m.clientWidth,
      padL: Math.round(parseFloat(cs.paddingLeft)),
      padR: Math.round(parseFloat(cs.paddingRight)),
      contentW: Math.round(m.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)),
      lastBodyW: r ? Math.round(r.width) : null,
      collapsed: document.getElementById('appShell').classList.contains('sidebar-collapsed')
    };
  })()`;

  const t0 = await ev(measure);
  await s('Page.captureScreenshot', { format: 'png' }).then((r) => fs.writeFileSync(`${SHOTS}/chatmax-expanded.png`, Buffer.from(r.result.data, "base64")));
  console.log('[expanded]', JSON.stringify(t0));

  // 折叠左栏（真实按钮路径：直接调用页面函数更贴近产品行为）
  await ev(`localStorage.setItem('naibaChatSidebarCollapsed','1'); document.getElementById('appShell').classList.add('sidebar-collapsed'); renderSidebar && renderSidebar(); true`);
  await wait(700);
  const t1 = await ev(measure);
  await s('Page.captureScreenshot', { format: 'png' }).then((r) => fs.writeFileSync(`${SHOTS}/chatmax-collapsed.png`, Buffer.from(r.result.data, "base64")));
  console.log('[collapsed]', JSON.stringify(t1));

  ws.close(); process.exit(0);
}
main().catch((e) => { console.error('ERR', e); process.exit(1); });
