// 补 3 张示意图：19 流式渲染 / 20 产物卡片 / 21 工具审批
// 用真实 CSS 类名注入 DOM，不引用任何真实图片/对话内容，纯示意图。
import fs from 'node:fs';
import path from 'node:path';
const OUT = 'd:/naiba-chat/docs/manual/images';

const res = await fetch('http://127.0.0.1:9222/json');
const target = (await res.json())[0];
const ws = new WebSocket(target.webSocketDebuggerUrl);
let seq = 0; const pending = new Map();
ws.addEventListener('message', (e) => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); p.resolve(m); } });
await new Promise(r => ws.addEventListener('open', r));
const send = (method, params = {}, sessionId) => { seq++; return new Promise(r => { pending.set(seq, { resolve: r }); ws.send(JSON.stringify({ id: seq, method, params, sessionId })); }); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const { result: { targetInfos } } = await send('Target.getTargets');
const page = targetInfos.find(t => t.type === 'page');
const { result: { sessionId } } = await send('Target.attachToTarget', { targetId: page.targetId, flatten: true });
const sess = (m, p = {}) => send(m, p, sessionId);
await sess('Page.enable'); await sess('Runtime.enable');

const ev = async (expr) => {
  const r = await sess('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.result?.exceptionDetails) console.error('EVAL ERR:', expr.slice(0, 100), r.result.exceptionDetails);
  return r.result?.result?.value;
};
const shot = async (file) => {
  const r = await sess('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(path.join(OUT, file), Buffer.from(r.result.data, 'base64'));
  console.log('shot ->', file, fs.statSync(path.join(OUT, file)).size, 'B');
};

// 把 #messages 重置成我们的 HTML，隐藏 empty-state
async function render(html) {
  await ev(`(() => {
    // 关闭所有打开的 dialog
    document.querySelectorAll('dialog[open]').forEach((d) => { try { d.close(); } catch(_){} });
    // 关闭图片灯箱与右键菜单
    const lb = document.querySelector('#imageLightbox'); if (lb) lb.hidden = true;
    const icm = document.querySelector('#imageContextMenu'); if (icm) icm.hidden = true;
    // 展开 main 区域避免被任何浮层遮
    const es = document.querySelector('#emptyState');
    if (es) es.hidden = true;
    const m = document.querySelector('#messages');
    m.innerHTML = ${JSON.stringify(html)};
    m.scrollTop = 0;
  })()`);
  await sleep(500);
}

// === 19 流式渲染：user 消息 + assistant 含思考块/工具块/正文交错/产物占位 ===
const html19 = `
  <article class="message-row user">
    <div class="message-body">
      <p>把 <code>D:\\海螺H3提示词工程\\素材4</code> 下前 3 张 png 改个名：加日期前缀，格式 20260902_原名。先列方案别动文件。</p>
    </div>
  </article>
  <article class="message-row assistant">
    <div class="message-avatar">AI</div>
    <div class="message-body">
      <details class="reasoning-block" open>
        <summary>思考：用户要求改文件名但先不动手，我需要先读目录看现有文件……</summary>
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
  "command": "Get-ChildItem 'D:\\\\海螺H3提示词工程\\\\素材4' -File -Filter *.png | Select-Object -First 3 | ForEach-Object { 'Rename-Item -LiteralPath \\\\'' + $_.FullName + '\\\\' -NewName 20260902_' + $_.Name }",
  "result": "将重命名 3 个文件，原文件无冲突。"
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
  </article>
`;
await render(html19);
await shot('19-chat-streaming.png');

// === 20 产物卡片：assistant 末尾附 6 张 CSS 渐变占位缩略图（纯示意图） ===
const grads = [
  'linear-gradient(135deg,#7a86b6,#3b4a6b)',
  'linear-gradient(135deg,#b68a7a,#6b3b3b)',
  'linear-gradient(135deg,#7ab68a,#3b6b4a)',
  'linear-gradient(135deg,#b6b27a,#6b673b)',
  'linear-gradient(135deg,#7ab6b2,#3b6b6a)',
  'linear-gradient(135deg,#a87ab6,#5b3b6b)'
];
const items = [
  ['cover_p1_1', 'jpg', '384×512'],
  ['cover_p1_2', 'png', '384×512'],
  ['cover_p1_3', 'png', '384×512'],
  ['cover_p1_4', 'png', '384×512'],
  ['cover_p1_5', 'png', '384×512'],
  ['cover_p1_6', 'png', '384×512'],
];
const mediaHtml = items.map((g, i) => `
  <span class="media-item">
    <span class="media-image thumbnail placeholder-thumb" data-i="${i}" style="background:${grads[i]};">
      <span class="placeholder-label">示意图</span>
      <span class="placeholder-name">${g[0]}.${g[1]}</span>
      <span class="placeholder-size">${g[2]}</span>
    </span>
    <button class="thumb-reuse" type="button" title="发送到输入框（复用此图）" aria-label="发送到输入框" data-reuse-source="placeholder" data-reuse-name="${g[0]}.${g[1]}" data-reuse-thumb="">↩</button>
  </span>
`).join('');

const html20 = `
  <article class="message-row user">
    <div class="message-body">
      <p>用 runninghub 出 6 张 <b>古风武侠</b> 风格的竖构图封面。</p>
    </div>
  </article>
  <article class="message-row assistant">
    <div class="message-avatar">AI</div>
    <div class="message-body">
      <details class="reasoning-block" open>
        <summary>思考：runninghub 技能有 420+ 端点，选文生图……</summary>
        <div class="reasoning-content"><p>用 <code>runninghub</code> 的文生图端点，6 张不同 seed，固定竖构图（384×512）。</p></div>
      </details>
      <details class="tool-run">
        <summary>已执行 · run_skill_script · 批量提交 6 个文生图任务</summary>
        <pre>{
  "skill": "runninghub",
  "endpoint": "text-to-image",
  "count": 6,
  "params": { "prompt": "古风武侠, 远景, 竖构图, 4K, 电影感", "size": "384x512" }
}</pre>
      </details>
      <div class="answer-content">
        <p>出图完成（<b>示意图</b>，真实产物保存在 <code>data/generated</code>）：</p>
      </div>
      <div class="media-grid">${mediaHtml}</div>
      <div class="usage-line">本轮 2,318 tokens · 输入 1,612 · 输出 706 · 缓存命中率 62.1% · 7 次请求</div>
      <div class="message-actions"><button data-copy-message>复制</button></div>
    </div>
  </article>
`;
await render(html20);
// 注入占位样式（用真实 class 但 webp src 不会加载；改用 inline 背景色）
await ev(`(() => {
  // 让 .placeholder-thumb 显示成"图片"的样子
  const css = \`
    .placeholder-thumb {
      display:flex; flex-direction:column; align-items:center; justify-content:center;
      width:100%; aspect-ratio:3/4; color:#fff; font-family:system-ui;
      box-shadow:0 1px 3px rgba(0,0,0,.15); border-radius:2px;
    }
    .placeholder-thumb .placeholder-label { font-size:10px; opacity:.7; letter-spacing:1px; }
    .placeholder-thumb .placeholder-name { font-size:13px; font-weight:600; margin-top:4px; }
    .placeholder-thumb .placeholder-size { font-size:10px; opacity:.7; margin-top:2px; }
    .media-grid { gap:8px; }
  \`;
  const s = document.createElement('style'); s.textContent = css; document.head.appendChild(s);
})()`);
await sleep(300);
await shot('20-chat-artifacts.png');

// === 21 工具审批弹窗：写一条消息 + 一个 .tool-confirm 卡片 ===
const html21 = `
  <article class="message-row user">
    <div class="message-body">
      <p>把 <code>C:\\重要文件</code> 下所有 txt 合并成一份。</p>
    </div>
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
  "command": "Get-ChildItem 'C:\\\\重要文件' -Filter *.txt | ForEach-Object { Get-Content \\\\$_ -Raw } | Set-Content 'C:\\\\重要文件\\\\merged.txt'",
  "risk": "工作区外写入 + 新建文件"
}</pre></div>
        </div>
        <div class="tool-confirm-actions">
          <button class="tool-confirm-btn tool-confirm-reject" onclick="void(0)">拒绝</button>
          <button class="tool-confirm-btn tool-confirm-approve" onclick="void(0)">允许执行</button>
        </div>
      </div>
    </div>
  </article>
`;
await render(html21);
await sleep(300);
await shot('21-tool-approval.png');

console.log('DONE');
process.exit(0);
