// 补一张 16 对话设置：找第一个 conversation-item 的 ⚙ 按钮并点击。
import fs from 'node:fs';
const res = await fetch('http://127.0.0.1:9222/json');
const target = (await res.json())[0];
const ws = new WebSocket(target.webSocketDebuggerUrl);
let seq = 0; const pending = new Map();
ws.addEventListener('message', (e) => { const m=JSON.parse(e.data); if(m.id && pending.has(m.id)){const p=pending.get(m.id); pending.delete(m.id); p.resolve(m);} });
await new Promise(r => ws.addEventListener('open', r));
const send = (method, params={}, sessionId) => { seq++; return new Promise(r => { pending.set(seq, {resolve:r}); ws.send(JSON.stringify({id:seq, method, params, sessionId})); }); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const { result: { targetInfos } } = await send('Target.getTargets');
const page = targetInfos.find(t => t.type === 'page');
const { result: { sessionId } } = await send('Target.attachToTarget', { targetId: page.targetId, flatten: true });
const sess = (m, p={}) => send(m, p, sessionId);
await sess('Page.enable');
await sess('Runtime.enable');

// 先 reload 确保 fresh
await sess('Page.reload', { ignoreCache: true });
await sleep(2500);
// 找到第一个 conversation-item 并点 .conversation-settings
const ok = await sess('Runtime.evaluate', { expression: `
(function(){
  const items = document.querySelectorAll('#sidebarWorkspaceTree .conversation-item');
  if (!items.length) return 'no items';
  // 找第一个有 ⚙ 按钮的
  for (const it of items) {
    const btn = it.querySelector('.conversation-settings');
    if (btn) {
      btn.style.opacity = '1'; // 强制显示
      btn.click();
      return {id: it.dataset.conversationId, count: items.length};
    }
  }
  return 'no button';
})()
`, returnByValue: true });
console.log('click result:', ok.result?.result?.value);
await sleep(700);
const r = await sess('Page.captureScreenshot', { format: 'png' });
fs.writeFileSync('d:/naiba-chat/docs/manual/images/16-conversation-settings.png', Buffer.from(r.result.data, 'base64'));
console.log('shot 16 OK');
process.exit(0);
