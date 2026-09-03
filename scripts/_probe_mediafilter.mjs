// 验证：多媒体（图片/视频/音频）不进入"本轮修改文件"chips，仍走原附件预览
import fs from 'node:fs';

const CDP = 'http://127.0.0.1:9222';

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
  const evaluate = async (expression) => {
    const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    return r.result.result.value;
  };
  await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: 'http://127.0.0.1:8788/?probe=mf' });
  await new Promise((r) => setTimeout(r, 3500));
  // 打开种子会话 19e395b5（含混合 files 的消息）
  await evaluate(`openConversation('19e395b5')`);
  await new Promise((r) => setTimeout(r, 2500));
  const report = await evaluate(`(() => {
    const chips = [...document.querySelectorAll('.file-change-chip')];
    const texts = chips.map((c) => c.textContent.trim());
    const files = [...document.querySelectorAll('.file-changes .file-change-name')].map((e) => e.textContent);
    const allText = document.querySelector('.messages')?.textContent || '';
    const mediaChips = chips.filter((c) => /img\.png|scene_01\.mp4|voice\.wav/i.test(c.textContent)).length;
    return JSON.stringify({ chipCount: chips.length, files, mediaChips, summaryShown: !!document.querySelector('.file-changes') });
  })()`);
  console.log('REPORT:', report);
  // 截图当前消息区
  await evaluate(`(() => {
    const m = [...document.querySelectorAll('.message')].find((el) => el.textContent.includes('code.py'));
    if (m) m.scrollIntoView({ block: 'center' });
  })()`);
  await new Promise((r) => setTimeout(r, 600));
  const shot = await send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync('D:/naiba-chat/_iso_test2/shots/mediafilter-chips.png', Buffer.from(shot.result.data, 'base64'));
  console.log('SHOT_SAVED');
  ws.close();
}
main().catch((e) => { console.error('ERR', e.message); process.exit(1); });
