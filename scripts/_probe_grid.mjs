// 一次性探针：实测左右侧栏折叠/展开时中间栏（chat-main）计算宽度
const CDP = 'http://127.0.0.1:9222';

async function getWs() {
  const list = await (await fetch(`${CDP}/json/list`)).json();
  const page = list.find((t) => t.type === 'page');
  if (!page) throw new Error('no page target');
  return page.webSocketDebuggerUrl;
}

async function connect() {
  const wsUrl = await getWs();
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let seq = 0; const pending = new Map();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  };
  const send = (method, params = {}) => new Promise((res) => {
    const id = ++seq; pending.set(id, res); ws.send(JSON.stringify({ id, method, params }));
  });
  return { ws, send };
}

async function main() {
  const { ws, send } = await connect();
  const s = send.bind(null);
  await s('Page.enable');
  await s('Runtime.enable');
  // 强制桌面视口（清掉上个回归脚本残留的移动端 override）
  await s('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  const url = 'http://127.0.0.1:8788/?probe=1';
  await s('Page.navigate', { url });
  await wait(2500);

  async function evalJs(expr) {
    const r = await s('Runtime.evaluate', { expression: expr, returnByValue: true });
    if (r.result?.exceptionDetails) return { err: JSON.stringify(r.result.exceptionDetails) };
    return r.result?.result?.value;
  }

  const snapshot = async (label) => {
    const v = await evalJs(`(() => {
      const shell = document.getElementById('appShell');
      const sb = document.getElementById('sidebar');
      const cm = document.querySelector('.chat-main');
      const fp = document.getElementById('filePanel');
      const g = shell ? getComputedStyle(shell).gridTemplateColumns : 'no-shell';
      const rect = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); return { x: Math.round(r.x), w: Math.round(r.width) }; };
      return { grid: g, sidebar: rect(sb), main: rect(cm), filePanel: rect(fp),
               shellCollapsed: shell?.classList.contains('sidebar-collapsed'),
               fileOpen: shell?.classList.contains('file-panel-open'),
               sidebarW: getComputedStyle(document.documentElement).getPropertyValue('--sidebar-w').trim(),
               viewport: window.innerWidth };
    })()`);
    console.log(`\n[${label}]`, JSON.stringify(v, null, 1));
  };

  // 1) 初始（可能带持久化折叠状态 → 先展开）
  await evalJs(`document.getElementById('appShell')?.classList.remove('sidebar-collapsed'); localStorage.removeItem('naibaChatSidebarCollapsed');`);
  await wait(400);
  await snapshot('initial (both expanded)');

  // 2) 折叠左栏
  await evalJs(`document.getElementById('appShell')?.classList.add('sidebar-collapsed');`);
  await wait(400);
  await snapshot('left COLLAPSED');

  // 3) 展开左栏
  await evalJs(`document.getElementById('appShell')?.classList.remove('sidebar-collapsed');`);
  await wait(400);
  await snapshot('left expanded again');

  // 4) 打开右面板
  await evalJs(`(function(){ try { window.__openFp && window.__openFp(); } catch(e){} })(); true`);
  await evalJs(`document.getElementById('appShell')?.classList.add('file-panel-open'); document.getElementById('filePanel') && (document.getElementById('filePanel').style.display='flex');`);
  await wait(400);
  await snapshot('file panel OPEN');

  // 5) 左折叠 + 右开
  await evalJs(`document.getElementById('appShell')?.classList.add('sidebar-collapsed');`);
  await wait(400);
  await snapshot('left COLLAPSED + right open');

  // 6) 左展开 + 右开
  await evalJs(`document.getElementById('appShell')?.classList.remove('sidebar-collapsed');`);
  await wait(400);
  await snapshot('left expanded + right open');

  // 7) 关右面板
  await evalJs(`document.getElementById('appShell')?.classList.remove('file-panel-open');`);
  await wait(400);
  await snapshot('file panel CLOSED');

  ws.close();
  process.exit(0);
}

main().catch((e) => { console.error('ERR', e); process.exit(1); });
