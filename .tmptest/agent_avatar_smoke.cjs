// Agent 自定义头像冒烟（源码 server，端口 8790）。
// 覆盖：卡片与聊天气泡显示自定义头像（中心裁切）/ 头像接口返回 image/webp /
//       弹层「自定义头像」选图 → 预览 → 保存后换图（内容哈希改名）/ 换图后旧文件删除 /
//       无头像时回退「AI」圆标 / 零页面错误。
// 前置：`python .tmptest/seed_agent_avatar_chat.py` 播种（跑完 --restore 还原）。
// 运行：$env:NODE_PATH="<node_modules 目录>"; node .tmptest\agent_avatar_smoke.cjs
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8790';
const AGENT_ID = 'agent_avatar_smoke';
const CONV_TITLE = '头像冒烟会话';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function apiJson(path, options = {}) {
  const init = { headers: { 'Content-Type': 'application/json' }, ...options };
  if (init.body && typeof init.body !== 'string') init.body = JSON.stringify(init.body);
  const response = await fetch(`${BASE}${path}`, init);
  return response.json().catch(() => ({}));
}

async function agentFromBootstrap(id) {
  const bootstrap = await apiJson('/api/bootstrap');
  return (bootstrap.agents || []).find((agent) => agent.id === id) || null;
}

// 侧栏虚拟列表：目标会话不一定在 DOM 里，先回顶部再逐步下滚。
async function ensureVisible(page, title) {
  await page.evaluate(() => { const tree = document.querySelector('#sidebarWorkspaceTree'); if (tree) tree.scrollTop = 0; });
  await page.waitForTimeout(250);
  for (let i = 0; i < 80; i++) {
    const state = await page.evaluate((needle) => {
      const tree = document.querySelector('#sidebarWorkspaceTree');
      if (!tree) return 'missing';
      const item = [...tree.querySelectorAll('.conversation-item')]
        .find((el) => (el.querySelector('.conversation-open')?.textContent || '').includes(needle));
      if (item) { item.scrollIntoView({ block: 'center' }); return 'found'; }
      const step = Math.max(120, tree.clientHeight * 0.8);
      if (tree.scrollTop + tree.clientHeight >= tree.scrollHeight - 1) return 'end';
      tree.scrollTop = Math.min(tree.scrollTop + step, tree.scrollHeight);
      return 'scroll';
    }, title);
    if (state === 'found') { await page.waitForTimeout(200); return true; }
    if (state === 'end' || state === 'missing') return false;
    await page.waitForTimeout(140);
  }
  return false;
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  const notFound = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });
  page.on('response', (res) => { if (res.status() === 404) notFound.push(res.url()); });
  page.on('dialog', (dialog) => dialog.accept());

  try {
    const seeded = await agentFromBootstrap(AGENT_ID);
    check('播种的带头像 Agent 在列表里', Boolean(seeded), JSON.stringify(seeded));
    const firstAvatar = String(seeded?.avatar || '');
    check('Agent 已带 avatar 文件名', /^agent_avatar_smoke_[0-9a-f]{12}\.webp$/.test(firstAvatar), firstAvatar);

    // 头像接口：200 + image/webp
    const avatarResponse = await fetch(`${BASE}/api/agents/avatar/${encodeURIComponent(firstAvatar)}`);
    const avatarType = avatarResponse.headers.get('content-type') || '';
    const avatarBytes = (await avatarResponse.arrayBuffer()).byteLength;
    check('头像接口返回 image/webp 且有内容',
      avatarResponse.status === 200 && avatarType.startsWith('image/webp') && avatarBytes > 0,
      JSON.stringify({ status: avatarResponse.status, type: avatarType, bytes: avatarBytes }));

    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(1000);

    // ---- 聊天气泡头像：会话绑定该 Agent → 助手消息头像换成图片 ----
    const visible = await ensureVisible(page, CONV_TITLE);
    check('侧栏里能找到播种的会话', visible === true, CONV_TITLE);
    await page.click(`#sidebarWorkspaceTree .conversation-open:has-text("${CONV_TITLE}")`);
    await page.waitForSelector('#messages .message-row.assistant', { timeout: 20000 });
    await page.waitForTimeout(800);
    const chatAvatar = await page.evaluate(() => {
      const row = document.querySelector('#messages .message-row.assistant');
      const img = row?.querySelector('img.message-avatar');
      const fallback = row?.querySelector('div.message-avatar');
      const rect = img ? img.getBoundingClientRect() : null;
      return {
        hasImg: Boolean(img),
        src: img ? img.getAttribute('src') : '',
        naturalWidth: img ? img.naturalWidth : 0,
        size: rect ? { w: Math.round(rect.width), h: Math.round(rect.height) } : null,
        objectFit: img ? getComputedStyle(img).objectFit : '',
        fallbackText: fallback ? fallback.textContent.trim() : '',
      };
    });
    check('助手消息头像已替换为自定义图片',
      chatAvatar.hasImg === true && chatAvatar.src.includes(firstAvatar),
      JSON.stringify(chatAvatar));
    check('头像图片真的加载成功且为正方形 30px',
      chatAvatar.naturalWidth > 0 && chatAvatar.size?.w === 30 && chatAvatar.size?.h === 30,
      JSON.stringify(chatAvatar));
    check('头像按中心裁切填充（object-fit: cover）',
      chatAvatar.objectFit === 'cover', chatAvatar.objectFit);

    // ---- 设置页：卡片显示头像 + 「自定义头像」换图 ----
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('.settings-nav button[data-settings-tab="agent"]');
    await page.waitForSelector('#agentCards .agent-card', { timeout: 10000 });
    const cardAvatar = await page.evaluate((id) => {
      const card = document.querySelector(`[data-agent-card="${id}"]`);
      const img = card?.querySelector('.agent-card-avatar');
      return {
        hasImg: Boolean(img),
        src: img ? img.getAttribute('src') : '',
        naturalWidth: img ? img.naturalWidth : 0,
      };
    }, AGENT_ID);
    check('Agent 卡片显示头像缩略图',
      cardAvatar.hasImg === true && cardAvatar.src.includes(firstAvatar) && cardAvatar.naturalWidth > 0,
      JSON.stringify(cardAvatar));

    await page.click(`[data-agent-card="${AGENT_ID}"] .agent-card-name`);
    await page.waitForSelector('#agentDialog[open]', { timeout: 10000 });
    await page.waitForTimeout(500);
    const previewBefore = await page.evaluate(() => {
      const img = document.querySelector('#agentAvatarPreview');
      return { hidden: img?.hidden, src: img?.getAttribute('src') || '' };
    });
    check('弹层里显示当前头像预览',
      previewBefore.hidden === false && previewBefore.src.includes(firstAvatar),
      JSON.stringify(previewBefore));

    // 用页面 canvas 现造一张真实 PNG（手写 base64 容易造出坏图 → 后端 400，属脚本自身问题）
    const dataUrl = await page.evaluate(() => {
      const canvas = document.createElement('canvas');
      canvas.width = 240;
      canvas.height = 80;
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = '#2ea3ff';
      ctx.fillRect(0, 0, 240, 80);
      ctx.fillStyle = '#ffd400';
      ctx.fillRect(60, 20, 120, 40);
      return canvas.toDataURL('image/png');
    });
    const avatarBuffer = Buffer.from(String(dataUrl).split(',')[1], 'base64');
    await page.setInputFiles('#agentAvatarFileInput', {
      name: 'avatar_smoke_new.png', mimeType: 'image/png', buffer: avatarBuffer,
    });
    await page.waitForTimeout(500);
    const previewAfter = await page.evaluate(() => {
      const img = document.querySelector('#agentAvatarPreview');
      return { hidden: img?.hidden, src: img?.getAttribute('src') || '', title: img?.title || '' };
    });
    check('选图后立即本地预览（未上传）',
      previewAfter.hidden === false && previewAfter.src.startsWith('blob:'),
      JSON.stringify(previewAfter));

    await page.click('#saveAgentForm');
    await page.waitForTimeout(2500);
    const updated = await agentFromBootstrap(AGENT_ID);
    const secondAvatar = String(updated?.avatar || '');
    check('保存后头像已换新（内容哈希改名）',
      secondAvatar && secondAvatar !== firstAvatar,
      JSON.stringify({ firstAvatar, secondAvatar }));
    const oldResponse = await fetch(`${BASE}/api/agents/avatar/${encodeURIComponent(firstAvatar)}`);
    check('换图后旧头像文件已删除（404）', oldResponse.status === 404, String(oldResponse.status));
    const newResponse = await fetch(`${BASE}/api/agents/avatar/${encodeURIComponent(secondAvatar)}`);
    check('新头像可读取', newResponse.status === 200, String(newResponse.status));

    await page.waitForTimeout(600);
    const cardAfter = await page.evaluate((id) => {
      const img = document.querySelector(`[data-agent-card="${id}"] .agent-card-avatar`);
      return { src: img ? img.getAttribute('src') : '', naturalWidth: img ? img.naturalWidth : 0 };
    }, AGENT_ID);
    check('卡片头像刷新为新图',
      cardAfter.src.includes(secondAvatar) && cardAfter.naturalWidth > 0,
      JSON.stringify(cardAfter));

    // ---- 无头像的 Agent 仍回退「AI」圆标 ----
    const conversationList = await apiJson('/api/conversations');
    const conversation = (conversationList.conversations || []).find((item) => item.title === CONV_TITLE);
    const plainAgent = (await apiJson('/api/bootstrap')).agents.find((agent) => !agent.avatar && !agent.built_in);
    if (conversation && plainAgent) {
      await apiJson(`/api/conversations/${encodeURIComponent(conversation.id)}/settings`, {
        method: 'POST', body: { agent_id: plainAgent.id },
      });
      await page.reload({ waitUntil: 'load', timeout: 20000 });
      await page.waitForSelector('#messageInput', { timeout: 20000 });
      await ensureVisible(page, CONV_TITLE);
      await page.click(`#sidebarWorkspaceTree .conversation-open:has-text("${CONV_TITLE}")`);
      await page.waitForSelector('#messages .message-row.assistant', { timeout: 20000 });
      await page.waitForTimeout(600);
      const fallback = await page.evaluate(() => {
        const row = document.querySelector('#messages .message-row.assistant');
        return {
          hasImg: Boolean(row?.querySelector('img.message-avatar')),
          text: row?.querySelector('div.message-avatar')?.textContent.trim() || '',
        };
      });
      check('无头像的 Agent 回退到「AI」圆标',
        fallback.hasImg === false && fallback.text === 'AI', JSON.stringify(fallback));
      await apiJson(`/api/conversations/${encodeURIComponent(conversation.id)}/settings`, {
        method: 'POST', body: { agent_id: AGENT_ID },
      });
    } else {
      check('无头像的 Agent 回退到「AI」圆标', false, '缺少可切换的普通 Agent 或会话');
    }
    check('零 pageerror / console.error', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
    check('零 404 资源', notFound.length === 0, notFound.slice(0, 3).join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, String(error && error.stack ? error.stack.split('\n')[0] : error));
  } finally {
    await browser.close();
  }

  console.log();
  console.log(`Agent 头像冒烟结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exitCode = failures.length ? 1 : 0;
})();
