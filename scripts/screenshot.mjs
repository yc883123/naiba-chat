// Naiba Chat 说明书配图一键生成（1.6.9+）
// 依赖：本地服务 http://127.0.0.1:8765 已启动；Chrome --remote-debugging-port=9222 已启动。
// 用法：node scripts/screenshot.mjs
// 说明：除 3 张「对话示意图」为 CSS 占位注入外，其余全部是真实界面 + 真实渲染。
import fs from 'node:fs';
import path from 'node:path';

const OUT = 'd:/naiba-chat/docs/manual/images';
const DEMO_DIR = 'D:\\naiba-chat\\docs\\manual';
const DEMO_TITLE = '说明书演示';
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
    pending.set(seq, { resolve, reject });
    const msg = { id: seq, method, params };
    if (sessionId) msg.sessionId = sessionId;
    ws.send(JSON.stringify(msg));
  });
};
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

const { result: { targetInfos } } = await send('Target.getTargets');
const page = targetInfos.find(t => t.type === 'page');
const { result: { sessionId } } = await send('Target.attachToTarget', { targetId: page.targetId, flatten: true });
const sess = (method, params = {}) => send(method, params, sessionId);

await sess('Page.enable');
await sess('Runtime.enable');

async function metrics(width, height, mobile = false) {
  await sess('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile });
}
async function navigate(url) {
  const loadPromise = new Promise(resolve => {
    const onMsg = (e) => {
      const m = JSON.parse(e.data);
      if (m.method === 'Page.loadEventFired' && (!m.sessionId || m.sessionId === sessionId)) {
        ws.removeEventListener('message', onMsg);
        resolve();
      }
    };
    ws.addEventListener('message', onMsg);
    setTimeout(resolve, 8000);
  });
  await sess('Page.navigate', { url });
  await loadPromise;
  await sleep(1500);
}
async function ev(expr) {
  const r = await sess('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) console.error('EVAL ERR:', expr.slice(0, 120), r.result.exceptionDetails);
  return r.result?.result?.value;
}
async function shot(file) {
  const r = await sess('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(path.join(OUT, file), Buffer.from(r.result.data, 'base64'));
  console.log('  ✓', file, (fs.statSync(path.join(OUT, file)).size / 1024).toFixed(0), 'KB');
}
// 关闭所有浮层，回到干净的主界面
async function clean() {
  await ev(`(() => {
    document.querySelectorAll('dialog[open]').forEach(d => { try { d.close(); } catch(_){} });
    ['#imageLightbox','#imageContextMenu','#skillPopup','#reasoningMenu','#contextUsagePopover'].forEach(sel => {
      const el = document.querySelector(sel); if (el) el.hidden = true;
    });
  })()`);
  await sleep(300);
}
async function openDialog(id) {
  await clean();
  await ev(`(() => { const d = document.querySelector('${id}'); if (d && !d.open) d.showModal(); })()`);
  await sleep(600);
}
async function settingsTab(name) {
  await ev(`(() => {
    document.querySelectorAll('[data-settings-tab]').forEach(b => b.classList.toggle('active', b.dataset.settingsTab === '${name}'));
    document.querySelectorAll('[data-settings-panel]').forEach(s => { s.hidden = s.dataset.settingsPanel !== '${name}'; });
    const c = document.querySelector('.settings-content'); if (c) c.scrollTop = 0;
  })()`);
  await sleep(500);
}

// ---------- 0. 准备演示会话（用于文件面板 / 消息渲染截图） ----------
console.log('[0] 准备演示会话');
await metrics(1600, 1000);
await navigate('http://127.0.0.1:8765');
const demoConv = await ev(`(async () => {
  const r = await fetch('/api/conversations', { method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ title: '${DEMO_TITLE}', workspace_dir: ${JSON.stringify(DEMO_DIR)} }) });
  const d = await r.json().catch(() => ({}));
  return d.id || d.conversation_id || '';
})()`);
console.log('  demo conversation:', demoConv || '(创建失败，文件面板截图可能不完整)');

// ---------- 1. 主界面 ----------
console.log('[1] 主界面');
await navigate('http://127.0.0.1:8765');
await shot('01-main-empty.png');

// 侧栏折叠（1.6.9 新）
await ev(`document.querySelector('#collapseSidebar')?.click()`);
await sleep(700);
await shot('02-sidebar-collapsed.png');
await ev(`document.querySelector('#expandSidebar')?.click()`);
await sleep(700);

// ---------- 2. 文件面板（1.6.9 新） ----------
console.log('[2] 文件面板');
await navigate('http://127.0.0.1:8765');
const opened = await ev(`(() => {
  const items = [...document.querySelectorAll('#sidebarWorkspaceTree .conversation-item')];
  const hit = items.find(i => (i.textContent || '').includes('${DEMO_TITLE}'));
  if (hit) { hit.click(); return 'clicked:' + items.length; }
  return 'notfound:' + items.length;
})()`);
console.log('  open demo conversation ->', opened);
await sleep(1200);

// 注入一条带「本轮修改文件」的助手消息（chip 指向真实文件，可点击）
const chips = [
  { op: 'write', file: 'D:\\naiba-chat\\docs\\manual\\_demo-产品说明.md', name: '_demo-产品说明.md' },
  { op: 'edit', file: 'D:\\naiba-chat\\docs\\manual\\build_html.py', name: 'build_html.py' },
];
const chipHtml = chips.map(c => `<button type="button" class="file-change-chip" data-file-op="${c.op}" data-open-file="${c.file.replace(/\\/g, '\\\\')}" title="${c.op === 'edit' ? '编辑' : '新建'}：${c.name}"><span class="file-change-op">${c.op === 'edit' ? '改' : '新'}</span><span class="file-change-name">${c.name}</span></button>`).join('');
const demoMsg = `
  <article class="message-row user">
    <div class="message-body"><p>帮我把这份封面规范整理成 Markdown，顺便修一下渲染脚本里的标题层级。</p></div>
  </article>
  <article class="message-row assistant">
    <div class="message-avatar">AI</div>
    <div class="message-body">
      <details class="tool-run">
        <summary>已执行 · write_file · 写入 _demo-产品说明.md</summary>
        <pre>{ "path": "_demo-产品说明.md", "bytes": 812 }</pre>
      </details>
      <div class="answer-content">
        <p>整理好了，三处改动：</p>
        <ul>
          <li>新建 <code>_demo-产品说明.md</code>，把画幅、提示词结构、命名规则、交付检查编成四节</li>
          <li>修改 <code>build_html.py</code> 的标题层级，目录结构恢复正常</li>
        </ul>
        <p>点文件名可以在右侧直接看内容、改完存回磁盘。</p>
      </div>
      <div class="file-changes">
        <div class="file-changes-label">本轮修改文件（新建 1 · 编辑 1）</div>
        <div class="file-changes-list">${chipHtml}</div>
      </div>
      <div class="usage-line">本轮 1,486 tokens · 输入 1,032 · 输出 454 · 缓存命中率 71.2% · 3 次请求</div>
      <div class="message-actions"><button data-copy-message>复制</button></div>
    </div>
  </article>`;
await ev(`(() => {
  const es = document.querySelector('#emptyState'); if (es) es.hidden = true;
  document.querySelector('#messages').innerHTML = ${JSON.stringify(demoMsg)};
})()`);
await sleep(600);
await shot('21-chat-filechanges.png');

// 点击第一个 chip（Markdown）→ 打开右侧文件面板（展示 Markdown 渲染）
await ev(`document.querySelector('.file-change-chip[data-file-op="write"]')?.click()`);
await sleep(1200);
await shot('03-file-panel.png');

// 再点第二个 chip（Python 文本），先截一张两个标签但都打开过的状态
await ev(`document.querySelector('.file-change-chip[data-file-op="edit"]')?.click()`);
await sleep(1200);

// 收起面板 → 顶栏出现「文件 N」按钮（两个标签都被记住）
await ev(`document.querySelector('#closeFilePanel')?.click()`);
await sleep(800);
await shot('04-file-panel-closed.png');

// ---------- 3. 设置 9 个 tab ----------
console.log('[3] 设置面板');
const tabs = [
  ['models', '05-settings-models.png'],
  ['agent', '06-settings-agent.png'],
  ['runtime', '07-settings-runtime.png'],
  ['connections', '08-settings-connections.png'],
  ['vision', '09-settings-vision.png'],
  ['search', '10-settings-search.png'],
  ['skills', '11-settings-skills.png'],
  ['datamigration', '12-settings-datamigration.png'],
  ['updates', '13-settings-updates.png'],
];
await openDialog('#settingsDialog');
for (const [tab, file] of tabs) {
  await settingsTab(tab);
  await shot(file);
}
await clean();

// ---------- 4. 各弹窗 ----------
console.log('[4] 弹窗');
await openDialog('#skillsDialog');
await shot('14-skill-refs.png');
await clean();

await openDialog('#tasksDialog');
await shot('15-tasks-dialog.png');
await clean();

await openDialog('#skillImportDialog');
await shot('16-skill-import.png');
await clean();

await openDialog('#workspaceDialog');
await shot('17-workspace-dialog.png');
await clean();

// 深度思考菜单
await ev(`(() => { const m = document.querySelector('#reasoningMenu'); if (m) m.hidden = false; })()`);
await sleep(400);
await shot('18-reasoning-menu.png');
await clean();

// 上下文用量
await ev(`document.querySelector('#contextUsageButton')?.click()`);
await sleep(600);
await shot('19-context-usage.png');
await clean();

// 对话设置（侧栏会话行 ⚙）
await ev(`(() => {
  const items = [...document.querySelectorAll('#sidebarWorkspaceTree .conversation-item')];
  for (const it of items) {
    const btn = it.querySelector('.conversation-settings');
    if (btn) { btn.style.opacity = '1'; btn.click(); return; }
  }
})()`);
await sleep(900);
await shot('20-conversation-settings.png');
await clean();

// ---------- 5. 对话示意图（CSS 占位，不引用真实产物） ----------
console.log('[5] 对话示意图');
async function renderMsgs(html) {
  await ev(`(() => {
    document.querySelectorAll('dialog[open]').forEach(d => { try { d.close(); } catch(_){} });
    const es = document.querySelector('#emptyState'); if (es) es.hidden = true;
    document.querySelector('#messages').innerHTML = ${JSON.stringify(html)};
    document.querySelector('#messages').scrollTop = 0;
  })()`);
  await sleep(500);
}

const htmlStreaming = `
  <article class="message-row user">
    <div class="message-body">
      <p>把 <code>D:\\海螺H3提示词工程\\素材4</code> 下前 3 张 png 改个名：加日期前缀，格式 20260902_原名。先列方案别动文件。</p>
    </div>
  </article>
  <article class="message-row assistant">
    <div class="message-avatar">AI</div>
    <div class="message-body">
      <details class="reasoning-block" open>
        <summary>思考：用户要求改文件名但先不动手……</summary>
        <div class="reasoning-content"><p>用户要求改文件名但先不动手，我需要先读目录看现有文件，确认命名规则后再列方案。</p></div>
      </details>
      <div class="tool-stack">
        <details class="tool-run">
          <summary>已执行 · list_directory · 列出目录</summary>
          <pre>{
  "path": "D:\\\\海螺H3提示词工程\\\\素材4",
  "entries": [
    { "name": "cover_01.png" },
    { "name": "cover_02.jpg" },
    { "name": "character_03.png" }
  ]
}</pre>
        </details>
        <details class="tool-run">
          <summary>已执行 · pwsh · 生成改名预览</summary>
          <pre>{
  "command": "Get-ChildItem 'D:\\\\海螺H3提示词工程\\\\素材4' -File -Filter *.png | Select-Object -First 3",
  "result": "匹配 3 个文件，无命名冲突。"
}</pre>
        </details>
      </div>
      <div class="answer-content">
        <p>找到 3 个匹配文件，方案如下（<b>不会真的改</b>）：</p>
        <ul>
          <li><code>cover_01.png</code> → <code>20260902_cover_01.png</code></li>
          <li><code>cover_02.jpg</code> → <code>20260902_cover_02.jpg</code></li>
          <li><code>character_03.png</code> → <code>20260902_character_03.png</code></li>
        </ul>
        <p>确认无误回我一声，我就执行。</p>
      </div>
      <div class="usage-line">本轮 1,247 tokens · 输入 892 · 输出 355 · 缓存命中率 78.4% · 1 次请求</div>
      <div class="message-actions"><button data-copy-message>复制</button></div>
    </div>
  </article>`;
await renderMsgs(htmlStreaming);
await shot('22-chat-streaming.png');

const grads = [
  'linear-gradient(135deg,#7a86b6,#3b4a6b)', 'linear-gradient(135deg,#b68a7a,#6b3b3b)',
  'linear-gradient(135deg,#7ab68a,#3b6b4a)', 'linear-gradient(135deg,#b6b27a,#6b673b)',
  'linear-gradient(135deg,#7ab6b2,#3b6b6a)', 'linear-gradient(135deg,#a87ab6,#5b3b6b)'
];
const mediaItems = ['cover_p1_1', 'cover_p1_2', 'cover_p1_3', 'cover_p1_4', 'cover_p1_5', 'cover_p1_6']
  .map((n, i) => `
  <span class="media-item">
    <span class="media-image thumbnail placeholder-thumb" style="background:${grads[i]}">
      <span class="placeholder-label">示意图</span>
      <span class="placeholder-name">${n}.png</span>
      <span class="placeholder-size">1080×1440</span>
    </span>
    <button class="thumb-reuse" type="button" title="发送到输入框（复用此图）" aria-label="发送到输入框">↩</button>
  </span>`).join('');
const htmlArtifacts = `
  <article class="message-row user">
    <div class="message-body"><p>用 runninghub 出 6 张 <b>古风武侠</b> 风格的竖构图封面。</p></div>
  </article>
  <article class="message-row assistant">
    <div class="message-avatar">AI</div>
    <div class="message-body">
      <details class="reasoning-block" open>
        <summary>思考：runninghub 技能有 420+ 端点，选文生图……</summary>
        <div class="reasoning-content"><p>用 <code>runninghub</code> 的文生图端点，6 张不同 seed，固定竖构图（1080×1440）。</p></div>
      </details>
      <details class="tool-run">
        <summary>已执行 · run_skill_script · 批量提交 6 个文生图任务</summary>
        <pre>{
  "skill": "runninghub",
  "endpoint": "text-to-image",
  "count": 6,
  "params": { "prompt": "古风武侠, 远景, 竖构图, 4K, 电影感", "size": "1080x1440" }
}</pre>
      </details>
      <div class="answer-content"><p>出图完成（<b>示意图</b>，真实产物保存在 <code>data/generated</code>）：</p></div>
      <div class="media-grid">${mediaItems}</div>
      <div class="usage-line">本轮 2,318 tokens · 输入 1,612 · 输出 706 · 缓存命中率 62.1% · 7 次请求</div>
      <div class="message-actions"><button data-copy-message>复制</button></div>
    </div>
  </article>`;
await renderMsgs(htmlArtifacts);
await ev(`(() => {
  const css = \`.placeholder-thumb{display:flex;flex-direction:column;align-items:center;justify-content:center;width:100%;aspect-ratio:3/4;color:#fff;font-family:system-ui;box-shadow:0 1px 3px rgba(0,0,0,.15);border-radius:2px}
  .placeholder-thumb .placeholder-label{font-size:10px;opacity:.7;letter-spacing:1px}
  .placeholder-thumb .placeholder-name{font-size:13px;font-weight:600;margin-top:4px}
  .placeholder-thumb .placeholder-size{font-size:10px;opacity:.7;margin-top:2px}\`;
  const s = document.createElement('style'); s.textContent = css; document.head.appendChild(s);
})()`);
await sleep(400);
await shot('23-chat-artifacts.png');

const htmlApproval = `
  <article class="message-row user">
    <div class="message-body"><p>把 <code>C:\\重要文件</code> 下所有 txt 合并成一份。</p></div>
  </article>
  <article class="message-row assistant">
    <div class="message-avatar">AI</div>
    <div class="message-body">
      <div class="answer-content">
        <p>要合并 <code>C:\\重要文件</code> 下的文件。这是个<strong>工作区外</strong>的写入操作，需要你点头。</p>
      </div>
      <div class="tool-confirm" data-confirm-id="cf_demo_01">
        <div class="tool-confirm-header">
          <span class="tool-confirm-icon">⚠️</span>
          <span class="tool-confirm-title">需要确认</span>
        </div>
        <div class="tool-confirm-body">
          <div class="tool-confirm-tool">工具：pwsh</div>
          <div class="tool-confirm-desc">在 <code>C:\\重要文件</code>（工作区外）执行写操作，生成合并文件</div>
          <div class="tool-confirm-args"><pre>{
  "command": "Get-ChildItem 'C:\\\\重要文件' -Filter *.txt | ForEach-Object { Get-Content $_ -Raw } | Set-Content 'C:\\\\重要文件\\\\merged.txt'",
  "risk": "工作区外写入 + 新建文件"
}</pre></div>
        </div>
        <div class="tool-confirm-actions">
          <button class="tool-confirm-btn tool-confirm-reject">拒绝</button>
          <button class="tool-confirm-btn tool-confirm-approve">允许执行</button>
        </div>
      </div>
    </div>
  </article>`;
await renderMsgs(htmlApproval);
await shot('24-tool-approval.png');

// ---------- 6. 移动端 ----------
console.log('[6] 移动端');
await metrics(430, 932, true);
await navigate('http://127.0.0.1:8765');
await shot('25-mobile-main.png');
await openDialog('#settingsDialog');
await settingsTab('models');
await shot('26-mobile-settings.png');
await clean();

// ---------- 7. 清理 ----------
console.log('[7] 清理演示会话');
await metrics(1600, 1000);
if (demoConv) {
  const del = await ev(`fetch('/api/conversations/${demoConv}', { method: 'DELETE' }).then(r => r.status).catch(e => 'err')`);
  console.log('  delete demo conversation ->', del);
}

console.log('DONE');
process.exit(0);
