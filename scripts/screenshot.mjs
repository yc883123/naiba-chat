// Naiba Chat 说明书配图一键生成（2.1.0）
// 依赖：本地服务已启动（默认 http://127.0.0.1:8765，可用 NAIBA_PORT 覆盖）；Chrome --remote-debugging-port=9222 已启动。
// 用法：node scripts/screenshot.mjs
// 说明：标注「示意」的几张是新特性（分割线 / 刻度轨 / 附件 / 上下文提醒）用真实 CSS 类注入渲染，
//       其余全部是真实界面 + 真实交互 + 真实文件渲染。
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// 一律从本文件位置派生项目根，避免硬编码本机盘符路径（换机器/换目录无需改代码）。
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const OUT = path.join(ROOT, 'docs', 'manual', 'images');
const DEMO_DIR = path.join(ROOT, 'docs', 'manual');
const DEMO_TITLE = '说明书演示';
// 端口可能被占用而漂移（实测服务会自己换端口），用 NAIBA_PORT 覆盖
const APP = `http://127.0.0.1:${process.env.NAIBA_PORT || 8765}`;
const DEMO_FILE = path.join(OUT, '..', '_demo-产品说明.md');
fs.mkdirSync(OUT, { recursive: true });

// 演示用的 Markdown（文件面板需要真实文件才能渲染）；截图结束自动删除
const DEMO_MD = `> 这是说明书的**演示文件**，用于展示右侧文件面板的 Markdown 渲染效果。

# 短剧封面规范 v2

## 一、画幅与尺寸

| 用途 | 尺寸 | 比例 |
|---|---|---|
| 竖版封面 | 1080 × 1440 | 3:4 |
| 横版海报 | 1920 × 1080 | 16:9 |
| 分镜草图 | 768 × 768 | 1:1 |

## 二、提示词结构

按 \`主体 → 环境 → 光位 → 镜头 → 风格\` 五段式写：

\`\`\`text
秦西西, 红衣, 立于雪中石阶
远景, 古建飞檐, 雪
侧逆光, 冷调
85mm, 浅景深
电影感, 胶片颗粒
\`\`\`

## 三、命名规则

- 单元号 + 镜号 + 版本：\`05_012_v3.png\`
- 备份文件加 \`_backup\` 后缀，批量出图前自动复制。

## 四、交付检查

1. [x] 尺寸符合画幅要求
2. [x] 人物面部无畸变
3. [ ] 完成终版调色
`;
fs.writeFileSync(DEMO_FILE, DEMO_MD, 'utf-8');

// 必须自己新建一个标签页：直接复用已存在的 page target 时，若它不是前台标签，
// 渲染器会被后台化，captureScreenshot 会超时或返回空白帧（实测：多张图完全一致）。
const verRes = await fetch('http://127.0.0.1:9222/json/version');
const verJson = await verRes.json();
const ws = new WebSocket(verJson.webSocketDebuggerUrl);
let seq = 0;
const pending = new Map();
ws.addEventListener('message', (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); p.resolve(m); }
});
await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', j); });

const rawSend = (method, params = {}, sessionId) => {
  seq++;
  return new Promise((resolve, reject) => {
    pending.set(seq, { resolve, reject });
    const msg = { id: seq, method, params };
    if (sessionId) msg.sessionId = sessionId;
    ws.send(JSON.stringify(msg));
  });
};
// 无头 Chrome 的 Page.captureScreenshot 偶发不回包（渲染器被 hover/过渡动画占住），
// 这里给每条命令加超时，截图命令由 shot() 重试，避免整轮卡死。
function withTimeout(promise, ms, label) {
  let timer;
  return Promise.race([
    promise.finally(() => clearTimeout(timer)),
    new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('TIMEOUT ' + label)), ms); }),
  ]);
}
const send = (method, params = {}, sessionId) =>
  withTimeout(rawSend(method, params, sessionId), 20000, method);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// 关掉残留的同源标签，避免它们抢前台（新建的标签就是唯一活跃页）
try {
  const { result: { targetInfos } } = await send('Target.getTargets');
  for (const t of targetInfos.filter(x => x.type === 'page')) {
    await send('Target.closeTarget', { targetId: t.targetId }).catch(() => {});
  }
} catch (_) {}
const { result: { targetId } } = await send('Target.createTarget', { url: 'about:blank' });
const { result: { sessionId } } = await send('Target.attachToTarget', { targetId, flatten: true });
const sess = (method, params = {}) => send(method, params, sessionId);
const closeTab = () => send('Target.closeTarget', { targetId }).catch(() => {});

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
  let r;
  try {
    r = await withTimeout(sess('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true }), 15000, 'evaluate');
  } catch (error) {
    console.log('  ! evaluate timeout：', expr.slice(0, 60));
    return null;
  }
  if (r.result?.exceptionDetails) console.error('EVAL ERR:', expr.slice(0, 120), r.result.exceptionDetails);
  return r.result?.result?.value;
}
// 局部裁剪截图（用于「输入区模型选择器」这类细节特写）
async function shotClip(file, clip) {
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      const r = await withTimeout(
        sess('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false, clip }),
        15000, 'captureScreenshot ' + file);
      fs.writeFileSync(path.join(OUT, file), Buffer.from(r.result.data, 'base64'));
      console.log('  ✓', file, '(裁剪)', (fs.statSync(path.join(OUT, file)).size / 1024).toFixed(0), 'KB');
      return;
    } catch (error) {
      console.log('  !', file, 'attempt', attempt, error.message);
      await sleep(1200);
    }
  }
  console.log('  ✗', file, '裁剪截图失败');
}
// 按给定矩形裁剪（矩形由页面 JS 现算，滚不到就退整张）
async function shotClip2(file, box) {
  if (!box || box.width < 40 || box.height < 30) { await shot(file); return; }
  if (box.y < 0) box = { ...box, y: 0 };
  await shotClip(file, { x: box.x, y: box.y, width: box.width, height: box.height, scale: 2 });
}
async function shot(file) {
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      const r = await withTimeout(
        sess('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false }),
        15000, 'captureScreenshot ' + file);
      fs.writeFileSync(path.join(OUT, file), Buffer.from(r.result.data, 'base64'));
      console.log('  ✓', file, (fs.statSync(path.join(OUT, file)).size / 1024).toFixed(0), 'KB');
      return;
    } catch (error) {
      console.log('  !', file, 'attempt', attempt, error.message);
      await sleep(1200);
    }
  }
  console.log('  ✗', file, '截图失败（3 次均未返回）');
}
// 关闭所有浮层，回到干净的主界面
async function clean() {
  await ev(`(() => {
    document.querySelectorAll('dialog[open]').forEach(d => { try { d.close(); } catch(_){} });
    ['#imageLightbox','#imageContextMenu','#skillPopup','#filePopup','#reasoningMenu',
     '#contextUsagePopover','#quickMessagePanel'].forEach(sel => {
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

// ---------- 0. 准备演示会话（用于文件面板 / @ 引用 / 消息渲染截图） ----------
console.log('[0] 准备演示会话');
await metrics(1600, 1000);
await navigate(APP);
const demoConv = await ev(`(async () => {
  const r = await fetch('/api/conversations', { method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ title: '${DEMO_TITLE}', workspace_dir: ${JSON.stringify(DEMO_DIR)} }) });
  const d = await r.json().catch(() => ({}));
  return d.id || d.conversation_id || '';
})()`);
console.log('  demo conversation:', demoConv || '(创建失败，文件面板截图可能不完整)');

// ---------- 1. 主界面 ----------
console.log('[1] 主界面');
await navigate(APP);
await shot('01-main-empty.png');

// 侧栏折叠
// 2.1.1：输入区「模型」选择器特写（顶栏选 API、输入区选模型，按会话记忆）
// 等模型目录拉取完，再裁底部输入区（下拉里有真实模型名）
await sleep(1200);
const modelOpts = await ev(`(() => {
  const s = document.querySelector('#composerModelSelect');
  return s ? Array.from(s.options).map(o => o.textContent.trim()).join(' | ') : 'null';
})()`);
console.log('  composer model options ->', modelOpts);
const composerBox = await ev(`(() => {
  const m = document.querySelector('.composer-meta');
  const w = document.querySelector('.composer-wrap');
  if (!m || !w) return null;
  const r = w.getBoundingClientRect();
  return { x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(m.getBoundingClientRect().bottom - r.top) };
})()`);
console.log('  composer box ->', JSON.stringify(composerBox));
if (composerBox && composerBox.height > 40) {
  await shotClip('32-model-selector.png', {
    x: composerBox.x, y: composerBox.y,
    width: composerBox.width, height: composerBox.height,
    scale: 2,
  });
}

await ev(`document.querySelector('#collapseSidebar')?.click()`);
await sleep(700);
await shot('02-sidebar-collapsed.png');
await ev(`document.querySelector('#expandSidebar')?.click()`);
await sleep(700);

// ---------- 2. 文件面板 ----------
console.log('[2] 文件面板');
await navigate(APP);
const opened = await ev(`(() => {
  const items = [...document.querySelectorAll('.conversation-item')];
  const hit = items.find(i => (i.textContent || '').includes('${DEMO_TITLE}'));
  if (hit) { hit.click(); return 'clicked:' + items.length; }
  return 'notfound:' + items.length;
})()`);
console.log('  open demo conversation ->', opened);
await sleep(1500);

// 注入一条带「本轮修改文件」的助手消息（chip 指向真实文件，可点击）
const chips = [
  { op: 'write', file: path.join(DEMO_DIR, '_demo-产品说明.md'), name: '_demo-产品说明.md' },
  { op: 'edit', file: path.join(DEMO_DIR, 'build_html.py'), name: 'build_html.py' },
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
  document.querySelectorAll('dialog[open]').forEach(d => { try { d.close(); } catch(_){} });
  const es = document.querySelector('#emptyState'); if (es) es.hidden = true;
  document.querySelector('#messages').innerHTML = ${JSON.stringify(demoMsg)};
})()`);
await sleep(600);
await shot('21-chat-filechanges.png');

// 点击第一个 chip（Markdown）→ 打开右侧文件面板（展示 Markdown 渲染）
await ev(`document.querySelector('.file-change-chip[data-file-op="write"]')?.click()`);
await sleep(1400);
await shot('03-file-panel.png');

// 再点第二个 chip（Python 文本），两个标签都打开过
await ev(`document.querySelector('.file-change-chip[data-file-op="edit"]')?.click()`);
await sleep(1200);

// 收起面板 → 顶栏出现「文件」按钮（两个标签都被记住）
await ev(`document.querySelector('#closeFilePanel')?.click()`);
await sleep(800);
await shot('04-file-panel-closed.png');

// ---------- 2.5 消息操作区：编辑 · 分支 / 复制 · 重新生成 · 新会话 ----------
// 真实会话需要真模型才能产出回复，这里用渲染器 04-messages.js 里逐字照搬的同一套 markup
// 注入，按钮、顺序、title 与线上完全一致。
console.log('[2.5] 消息操作区');
await clean();
await ev([
  "(() => {",
  "  const es = document.querySelector('#emptyState'); if (es) es.hidden = true;",
  "  const m = document.querySelector('#messages'); if (!m) return;",
  "  m.innerHTML = '';",
  "  const user = document.createElement('article');",
  "  user.className = 'message-row user';",
  "  user.innerHTML = '<div class=\"message-body\"><p>把 build_html.py 里打印样式那段注释翻译成中文</p>'",
  "    + '<div class=\"message-actions\">'",
  "    + '<button data-edit-message title=\"编辑这条提问并从这里重新发送（其后的消息会被删除）\">编辑</button>'",
  "    + '<button data-branch-message title=\"从这条消息分支到新会话继续\">分支</button>'",
  "    + '</div></div>';",
  "  const ai = document.createElement('article');",
  "  ai.className = 'message-row assistant';",
  "  ai.innerHTML = '<div class=\"message-avatar\">AI</div>'",
  "    + '<div class=\"message-card\"><div class=\"message-body\">'",
  "    + '<div class=\"answer-content\"><p>已经改好了。打印目录那一节现在写的是「屏幕版目录在左侧栏，打印版改由正文顶部的 .print-toc 承担」。</p></div>'",
  "    + '<div class=\"message-actions\">'",
  "    + '<button data-copy-message>复制</button>'",
  "    + '<button data-regenerate-message=\"demoMsg\" title=\"用同一条提问重新生成这条回复（前面的对话历史保持不变）\">重新生成</button>'",
  "    + '<button data-session-start-after=\"demoMsg\" title=\"在这条回复之后划一条分割线：此线以上的消息不再进入模型上下文（下方消息仍在上下文中，聊天记录全部保留）\">新会话</button>'",
  "    + '</div></div></div>';",
  "  m.append(user, ai);",
  "})()",
].join('\n'));
await sleep(600);
const actionsBox = await ev(`(() => {
  const m = document.querySelector('#messages');
  if (!m) return null;
  const r = m.getBoundingClientRect();
  const h = Math.min(Math.round(m.scrollHeight), 460);
  return { x: Math.max(0, Math.round(r.left)), y: Math.max(0, Math.round(r.top)), width: Math.round(r.width), height: h };
})()`);
if (actionsBox && actionsBox.height > 60) {
  await shotClip('35-message-actions.png', { ...actionsBox, scale: 2 });
} else {
  await shot('35-message-actions.png');
}

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

// ---------- 3.5 Skill 删除对话框 ----------
// 注意：删除对话框需要「已加载 Skill」列表里有条目才点得出来，而当前环境里已加载 Skill 为 0
// （没有可点的「删除」按钮），所以这里只在确实有 Skill 时才截；回收目录本身已经在
// 11-settings-skills.png 里露出（该页有「回收目录」区块），不另造示意图。
console.log('[3.5] Skill 删除对话框（可选）');
await settingsTab('skills');
let skillDel = 0;
for (let i = 0; i < 10; i += 1) {
  skillDel = await ev(`document.querySelectorAll('#installedSkillList .skill-delete').length`) || 0;
  if (skillDel > 0) break;
  await sleep(500);
}
console.log('  installedSkillList 可删条目 =', skillDel);
if (skillDel > 0) {
  await ev(`(() => { const b = document.querySelector('#installedSkillList .skill-delete'); if (b) b.click(); })()`);
  await sleep(900);
  const deleteOpen = await ev(`(() => { const d = document.querySelector('#skillDeleteDialog'); return !!(d && d.open); })()`);
  console.log('  skill delete dialog open ->', deleteOpen);
  if (deleteOpen) await shot('39-skill-delete-dialog.png');
} else {
  console.log('  跳过：没有已加载 Skill，删除对话框无真实入口');
}
await clean();

// ---------- 4. Agent 编辑弹层（卡片化 → 点卡片进表单） ----------
console.log('[4] Agent 编辑弹层');
await settingsTab('agent');
await ev(`(() => {
  const card = document.querySelector('#agentCards [data-agent-card]');
  if (card) card.click();
})()`);
await sleep(900);
await shot('20-agent-edit.png');
// 工具集两态：切到「工具集」页，展示只读预设卡 + 我的工具集 + 添加卡
await ev(`(() => {
  const tab = document.querySelector('.agent-tabs button[data-agent-tab="tools"]');
  if (tab) tab.click();
})()`);
await sleep(700);
await shot('20b-agent-tools.png');
await clean();

// ---------- 5. 各弹窗 ----------
console.log('[5] 弹窗');
await openDialog('#skillsDialog');
await shot('14-skill-refs.png');
await clean();

await openDialog('#tasksDialog');
await ev(`(() => {
  const list = document.querySelector('#taskList');
  if (!list) return;
  // 逐字对齐 08-conversations.js 里 #taskList 的真实渲染：
  // 只有 activeTaskStatuses（queued/running/waiting/cancelling）才带「停止」按钮。
  const active = new Set(['queued', 'running', 'waiting', 'cancelling']);
  const item = (title, meta, status, cls, detail) => \`
    <div class="task-item">
      <div class="task-title">\${title}</div>
      <div class="task-meta">\${meta}</div>
      <div class="task-detail">\${detail || ''}</div>
      <div class="task-actions">
        <span class="task-status \${cls}">\${status}</span>
        \${active.has(cls) ? '<button type="button" class="task-cancel" data-task-cancel="demo">停止</button>' : ''}
      </div>
    </div>\`;
  list.innerHTML = [
    item('批量生成 12 张 1080×1440 竖版封面（RunningHub 文生图）', '后台 · 通用 Agent · 短剧封面批量 · 已运行 4 分 12 秒', '运行中', 'running', ''),
    item('用 cover_v2.json 跑 ComfyUI 工作流（12 个 seed）', '后台 · 短剧 Agent · 说明书演示 · 用时 2 分 08 秒', '已完成', 'completed', ''),
    item('整理 D:\\\\素材4 下的工作流并加 _backup 备份', '前台 · 通用 Agent · 说明书演示 · 用时 36 秒', '失败', 'failed', '失败：ComfyUI 未启动（127.0.0.1:8188 连接被拒）'),
  ].join('');
})()`);
await sleep(500);
await shot('15-tasks-dialog.png');
await clean();

await openDialog('#skillImportDialog');
await shot('16-skill-import.png');
await clean();

await openDialog('#workspaceDialog');
await shot('17-workspace-dialog.png');
await clean();

// 上下文用量提醒弹窗（2.1.0 新增）
await ev(`(() => {
  const d = document.querySelector('#contextWarningDialog');
  const detail = document.querySelector('#contextWarningDetail');
  if (detail) detail.textContent = '上下文用量已达 82%（40,960 / 50,000 tokens）';
  const cont = document.querySelector('#contextWarningContinue'); if (cont) cont.hidden = false;
  if (d && !d.open) d.showModal();
})()`);
await sleep(600);
await shot('31-context-warning.png');
await clean();

// ---------- 6. 输入区：思考强度 / 上下文 / 快捷消息 / @ 引用 / 附件 ----------
console.log('[6] 输入区');
await navigate(APP);
await ev(`(() => {
  const items = [...document.querySelectorAll('.conversation-item')];
  const hit = items.find(i => (i.textContent || '').includes('${DEMO_TITLE}'));
  if (hit) hit.click();
})()`);
await sleep(1500);

// 深度思考菜单（直接展开，不 click 以免切档）
await ev(`(() => { const m = document.querySelector('#reasoningMenu'); if (m) m.hidden = false; })()`);
await sleep(400);
await shot('18-reasoning-menu.png');
await clean();

// 上下文用量
await ev(`document.querySelector('#contextUsageButton')?.click()`);
await sleep(700);
await shot('19-context-usage.png');
await clean();

// 审批模式上拉框（2.2.0：平铺四段 → 按钮 + 上方弹出列表）
await ev(`document.querySelector('#permissionModeButton')?.click()`);
await sleep(600);
const permOpen = await ev(`(() => {
  const m = document.querySelector('#permissionModeMenu');
  return m ? { hidden: m.hidden, items: m.querySelectorAll('[data-permission-mode]').length } : null;
})()`);
console.log('  permission menu ->', JSON.stringify(permOpen));
await shotClip2('33-permission-menu.png', await ev(`(() => {
  const w = document.querySelector('.composer-wrap');
  const m = document.querySelector('#permissionModeMenu');
  if (!w || !m) return null;
  const wr = w.getBoundingClientRect();
  const mr = m.getBoundingClientRect();
  const top = Math.min(wr.top, mr.top) - 12;
  return { x: Math.round(wr.left), y: Math.max(0, Math.round(top)),
           width: Math.round(wr.width), height: Math.round(wr.bottom - top + 12) };
})()`));
await clean();

// 快捷消息面板（2.1.0：内置交接报告预设）
await ev(`(() => { const b = document.querySelector('#quickMessageButton'); if (b) b.click(); })()`);
await sleep(700);
await shot('27-quick-messages.png');

// @ 引用工作区文件（输入 @ 触发真实目录浏览弹层）
// 先铺一条用户消息当背景，再输入 @（空态背景太抢眼）
await clean();
await ev(`(() => {
  const es = document.querySelector('#emptyState'); if (es) es.hidden = true;
  document.querySelector('#messages').innerHTML = \`
  <article class="message-row user">
    <div class="message-body"><p>把工作区里那份封面规范读一下，按它检查刚出的图。</p></div>
  </article>\`;
  const i = document.querySelector('#messageInput');
  if (!i) return;
  i.focus();
  i.value = '@';
  i.setSelectionRange(1, 1);
  i.dispatchEvent(new Event('input', { bubbles: true }));
})()`);
await sleep(1800);
const filePopupOk = await ev(`(() => {
  const p = document.querySelector('#filePopup');
  if (!p || p.hidden) return 'hidden';
  return 'open:' + p.querySelectorAll('.file-popup-item').length;
})()`);
console.log('  file popup ->', filePopupOk);
await shot('28-file-ref-popup.png');
await clean();

// 待发送附件列表（2.1.0：输入框上方竖排卡片）
await ev(`(() => {
  const box = document.querySelector('#pendingFiles');
  if (!box) return;
  const grads = ['linear-gradient(135deg,#7a86b6,#3b4a6b)','linear-gradient(135deg,#b68a7a,#6b3b3b)','linear-gradient(135deg,#7ab68a,#3b6b4a)'];
  const rows = [
    { name: 'reference_sheet_角色参考图_v3.png', img: 0, size: '1.8 MB' },
    { name: 'cover_v2_api_workflow.json', img: -1, size: '36 KB' },
    { name: '2026年Q3分镜脚本_终稿（含修改批注）.docx', img: 1, size: '412 KB' },
  ];
  box.innerHTML = rows.map((r, i) => {
    const thumb = r.img >= 0
      ? \`<img class="pending-thumb" src="data:image/svg+xml,\${encodeURIComponent('<svg xmlns=&quot;http://www.w3.org/2000/svg&quot; width=&quot;64&quot; height=&quot;64&quot;><rect width=&quot;64&quot; height=&quot;64&quot; fill=&quot;hsl(' + (200 + i * 40) + ',30%,55%)&quot;/></svg>')}" alt="">\`
      : '<span class="pending-thumb pending-thumb-file" aria-hidden="true"></span>';
    return \`<div class="pending-item">\${thumb}<span class="pending-name" title="\${r.name}">\${r.name}</span><button type="button" class="pending-remove" title="移除" aria-label="移除"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"></path></svg></button></div>\`;
  }).join('');
  box.hidden = false;
})()`);
await sleep(600);
await shot('30-attachments.png');
await clean();

// ---------- 7. 新会话分割线 + 对话刻度轨（示意，真实 CSS 类） ----------
console.log('[7] 分割线与刻度轨');
await ev(`(() => {
  const es = document.querySelector('#emptyState'); if (es) es.hidden = true;
  // 清掉上一步演示的待发送附件
  const box = document.querySelector('#pendingFiles');
  if (box) { box.innerHTML = ''; box.hidden = true; }
  const turn = (n, user, reply) => \`
  <article class="message-row user" data-message-id="u\${n}">
    <div class="message-body"><p>\${user}</p></div>
  </article>
  <article class="message-row assistant" data-message-id="a\${n}">
    <div class="message-avatar">AI</div>
    <div class="message-body">
      <div class="answer-content"><p>\${reply}</p></div>
      <div class="usage-line">本轮 1,2\${n}0 tokens · 缓存命中率 7\${n}.4% · 1 次请求</div>
      <div class="message-actions"><button data-copy-message>复制</button><button type="button" title="在此之后划分割线">新会话</button></div>
    </div>
  </article>\`;
  const divider = \`
  <article class="message-row session-divider" data-message-id="a1" data-session-divider="a1">
    <div class="session-divider-bar" title="此线以上的消息不再进入模型上下文；下方消息仍保留在上下文中（聊天记录全部保留）">
      <span class="session-divider-line" aria-hidden="true"></span>
      <span class="session-divider-label">新会话 · 手动 · 2026/9/10 10:24:31</span>
      <span class="session-divider-line" aria-hidden="true"></span>
      <button type="button" class="session-divider-cancel" title="撤销这条分割线：此线以上的消息重新进入模型上下文">撤销</button>
    </div>
    <div class="session-divider-hint">此线以上不再进入模型上下文</div>
  </article>\`;
  document.querySelector('#messages').innerHTML =
    turn(1, '把 D:\\\\素材4 下前 3 张 png 加日期前缀改名，先列方案别动文件。', '找到 3 个匹配文件，方案已列出，等你确认后再执行。')
    + divider
    + turn(2, '确认，执行改名。', '已改完 3 个文件：cover_01.png → 20260902_cover_01.png，另外两个同步完成。')
    + turn(3, '再出 6 张古风竖构图封面，尺寸 1080×1440。', '已提交 6 个文生图任务，产出在 data/generated 下，聊天里可以直接预览。')
    + turn(4, '把第 3 张的 seed 固定下来再跑一版。', '已固定 seed=20260902 重跑，这一版人物面部更稳定。');
})()`);
await sleep(600);
// 刻度轨由 state.messages 驱动，注入 DOM 不会触发它；直接按真实类名铺横条并立刻截图
await ev(`(() => {
  const rail = document.querySelector('#turnRail');
  if (!rail) return;
  rail.hidden = false;
  rail.replaceChildren();
  for (let i = 0; i < 10; i += 1) {
    const tick = document.createElement('button');
    tick.type = 'button';
    tick.className = 'turn-tick' + (i === 4 ? ' active' : '');
    tick.dataset.turnIndex = String(i);
    tick.setAttribute('aria-label', '第 ' + (i + 1) + ' 轮对话');
    const line = document.createElement('span');
    line.className = 'turn-tick-line';
    tick.append(line);
    rail.append(tick);
  }
})()`);
await sleep(300);
await shot('29-session-divider.png');

// ---------- 8. 对话示意图（CSS 占位，不引用真实产物） ----------
console.log('[8] 对话示意图');
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

// ---------- 9. 移动端 ----------
console.log('[9] 移动端');
await metrics(430, 932, true);
await navigate(APP);
await shot('25-mobile-main.png');

// 手机端文件面板「全屏抽屉」（2.2.0 功能对等：此前整体隐藏）
await ev(`(() => {
  const items = [...document.querySelectorAll('.conversation-item')];
  const hit = items.find(i => (i.textContent || '').includes('${DEMO_TITLE}'));
  if (hit) hit.click();
})()`);
await sleep(1500);
await ev(`(() => {
  const es = document.querySelector('#emptyState'); if (es) es.hidden = true;
  const chip = '<button type="button" class="file-change-chip" data-file-op="edit" data-open-file="${path.join(DEMO_DIR, 'build_html.py').replace(/\\/g, '\\\\')}" title="编辑：build_html.py"><span class="file-change-op">改</span><span class="file-change-name">build_html.py</span></button>';
  document.querySelector('#messages').innerHTML = \`
  <article class="message-row user">
    <div class="message-body"><p>手机上也能看改了哪些文件吗？</p></div>
  </article>
  <article class="message-row assistant">
    <div class="message-body">
      <div class="answer-content"><p>能。点下面的文件名，右侧文件面板会以<strong>全屏抽屉</strong>打开。</p></div>
      <div class="file-changes">
        <div class="file-changes-label">本轮修改文件（编辑 1）</div>
        <div class="file-changes-list">\${chip}</div>
      </div>
    </div>
  </article>\`;
})()`);
await sleep(500);
await ev(`document.querySelector('.file-change-chip[data-file-op="edit"]')?.click()`);
await sleep(1500);
await shot('34-mobile-file-drawer.png');
await ev(`document.querySelector('#closeFilePanel')?.click()`);
await sleep(700);

// 手机端顶栏折叠（收起整条操作区，把高度还给会话区）
await ev(`(() => { const b = document.querySelector('#toggleTopbarCompact'); if (b) b.click(); else return 'no toggle'; })()`);
await sleep(900);
const compactBox = await ev(`(() => {
  const t = document.querySelector('.topbar');
  if (!t) return null;
  const r = t.getBoundingClientRect();
  // 只截顶栏（外加收起后那条可点的细条），别把会话正文带进来抢镜。
  return { x: 0, y: 0, width: Math.round(window.innerWidth), height: Math.round(r.height) + 44 };
})()`);
console.log('  topbar compact ->', JSON.stringify(compactBox));
if (compactBox && compactBox.height > 80) await shotClip('38-mobile-topbar-compact.png', { ...compactBox, scale: 2 });
else await shot('38-mobile-topbar-compact.png');
await ev(`document.querySelector('#toggleTopbarCompact')?.click()`);
await sleep(500);

await openDialog('#settingsDialog');
await settingsTab('models');
await shot('26-mobile-settings.png');
await clean();

// ---------- 10. 清理 ----------
console.log('[10] 清理演示会话与演示文件');
await metrics(1600, 1000);
if (demoConv) {
  const del = await ev(`fetch('/api/conversations/${demoConv}', { method: 'DELETE' }).then(r => r.status).catch(e => 'err')`);
  console.log('  delete demo conversation ->', del);
}
try { fs.unlinkSync(DEMO_FILE); console.log('  removed demo markdown'); } catch (_) {}
await closeTab();

console.log('DONE');
process.exit(0);
