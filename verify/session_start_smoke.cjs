// 「新会话」分割线冒烟（由 verify/session_start_smoke.py 编排，源码 server 8798）。
// 覆盖：AI 回复末尾「复制」右侧的入口 → 任意位置画线（含中间位置）→ 分割线紧贴该条回复下方
//       → 允许多条 → 刷新仍在 → 逐条撤销 → 消息一条不删 → 零页面错误。
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8798';
const TITLE = process.env.NAIBA_SESSION_TITLE || '新会话边界冒烟';
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

async function messagesOf(conversationId) {
  const data = await apiJson(`/api/conversations/${encodeURIComponent(conversationId)}`);
  return data.messages || [];
}

async function domSnapshot(page) {
  return page.evaluate(() => {
    const rows = [...document.querySelectorAll('#messages .message-row[data-message-id]')];
    return {
      rows: rows.length,
      dividers: rows.filter((row) => row.classList.contains('session-divider')).length,
      labels: rows.filter((row) => row.classList.contains('session-divider'))
        .map((row) => row.querySelector('.session-divider-label')?.textContent.trim() || ''),
      // 每条分割线的前一个兄弟节点必须是被标记的那条 AI 回复
      dividerAnchors: rows.filter((row) => row.classList.contains('session-divider'))
        .map((row) => row.previousElementSibling?.querySelector('.answer-content')?.textContent.trim().slice(0, 12) || ''),
      buttons: document.querySelectorAll('#messages [data-session-start-after]').length,
      copyBeforeSession: [...document.querySelectorAll('#messages .message-actions')]
        .every((bar) => {
          const buttons = [...bar.querySelectorAll('button')];
          return buttons.length < 2 || buttons[0].textContent.trim() === '复制';
        }),
    };
  });
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });
  page.on('dialog', (dialog) => dialog.accept());

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { timeout: 20000 });
    await page.click(`#sidebarWorkspaceTree .conversation-item:has-text("${TITLE}") .conversation-open`);
    await page.waitForSelector('#messages .message-row[data-message-id]', { timeout: 20000 });
    await page.waitForTimeout(600);

    const conversationId = await page.evaluate((title) => {
      const item = [...document.querySelectorAll('#sidebarWorkspaceTree .conversation-item')]
        .find((el) => el.textContent.includes(title));
      return String(item?.dataset.conversationId || '');
    }, TITLE);
    check('拿到会话 ID（用于后端核对）', Boolean(conversationId), conversationId);

    const initial = await domSnapshot(page);
    check('初始：4 条消息、无分割线、每条 AI 回复都有「新会话」入口',
      initial.rows === 4 && initial.dividers === 0 && initial.buttons === 2, JSON.stringify(initial));
    check('「新会话」排在「复制」右侧', initial.copyBeforeSession === true, JSON.stringify(initial));

    // ① 在**第一条** AI 回复后画线（中间位置）
    await page.click('#messages .message-row:has-text("第一答") [data-session-start-after]');
    await page.waitForTimeout(900);
    const one = await domSnapshot(page);
    check('画线后：1 条分割线、消息仍是 4 条',
      one.rows === 5 && one.dividers === 1, JSON.stringify(one));
    check('分割线紧贴被点的那条 AI 回复下方',
      one.dividerAnchors.length === 1 && one.dividerAnchors[0].startsWith('第一答'),
      JSON.stringify(one.dividerAnchors));
    check('分割线文案标明方向', one.labels[0].startsWith('新会话 · 手动'), JSON.stringify(one.labels));

    let messages = await messagesOf(conversationId);
    check('后端不新增行、不删消息（仍是 4 条）', messages.length === 4,
      JSON.stringify(messages.map((m) => m.role)));
    check('标记落在第一条 AI 回复的 metadata 上（不是新行）',
      Boolean(messages[1].metadata?.session_start)
      && String(messages[1].metadata.session_start.source) === 'manual'
      && !messages[1].metadata.session_start.handoff_path,
      JSON.stringify(messages[1].metadata || {}));
    check('其余消息没有被标记', messages.filter((m) => m.metadata?.session_start).length === 1,
      JSON.stringify(messages.map((m) => Boolean(m.metadata?.session_start))));

    // ② 再在第二条 AI 回复后画线 → 允许两条
    await page.click('#messages .message-row:has-text("第二答") [data-session-start-after]');
    await page.waitForTimeout(900);
    const two = await domSnapshot(page);
    check('允许多条分割线（2 条，消息 4 条）',
      two.rows === 6 && two.dividers === 2, JSON.stringify(two));
    check('两条分割线各自贴着自己的锚点',
      two.dividerAnchors.length === 2
      && two.dividerAnchors[0].startsWith('第一答')
      && two.dividerAnchors[1].startsWith('第二答'), JSON.stringify(two.dividerAnchors));
    messages = await messagesOf(conversationId);
    check('后端两条标记并存', messages.filter((m) => m.metadata?.session_start).length === 2,
      JSON.stringify(messages.map((m) => Boolean(m.metadata?.session_start))));

    // ③ 刷新后仍在
    await page.reload({ waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messages .message-row[data-message-id]', { timeout: 20000 });
    await page.waitForTimeout(800);
    const reloaded = await domSnapshot(page);
    check('刷新后两条分割线仍在（已持久化）',
      reloaded.dividers === 2 && reloaded.rows === 6, JSON.stringify(reloaded));

    // ④ 撤销第一条 → 只剩第二条（分割线自身不含锚点正文，按顺序取第一条）
    await page.locator('#messages .message-row.session-divider [data-cancel-session-start]').first().click();
    await page.waitForTimeout(900);
    const afterFirstCancel = await domSnapshot(page);
    check('撤销第一条后只剩 1 条分割线',
      afterFirstCancel.dividers === 1 && afterFirstCancel.rows === 5, JSON.stringify(afterFirstCancel));
    check('剩下的是第二条的线', afterFirstCancel.dividerAnchors[0].startsWith('第二答'),
      JSON.stringify(afterFirstCancel.dividerAnchors));

    // ⑤ 撤销第二条 → 全部恢复
    await page.click('#messages [data-cancel-session-start]');
    await page.waitForTimeout(900);
    const cleared = await domSnapshot(page);
    check('全部撤销后回到 4 条消息、0 条分割线',
      cleared.rows === 4 && cleared.dividers === 0, JSON.stringify(cleared));
    messages = await messagesOf(conversationId);
    check('后端标记全部清除且消息完整',
      messages.length === 4 && messages.every((m) => !m.metadata?.session_start),
      JSON.stringify(messages.map((m) => [m.role, Boolean(m.metadata?.session_start)])));

    // ⑥ 模型主动重置（source=tool）：分割线标注来源 + 交接文档 + 「填入种子消息」
    const SEED_TITLE = process.env.NAIBA_SESSION_SEED_TITLE || '新会话种子冒烟';
    await page.click(`#sidebarWorkspaceTree .conversation-item:has-text("${SEED_TITLE}") .conversation-open`);
    await page.waitForSelector('#messages .message-row[data-message-id]', { timeout: 20000 });
    await page.waitForTimeout(800);
    const toolDividers = await page.evaluate(() => [...document.querySelectorAll('#messages .message-row.session-divider')]
      .map((row) => ({
        label: row.querySelector('.session-divider-label')?.textContent.trim() || '',
        hint: row.querySelector('.session-divider-hint')?.textContent.trim() || '',
        hasSeed: Boolean(row.querySelector('[data-fill-reset-seed]')),
      })));
    check('模型重置的分割线标出来源与交接文档',
      toolDividers.length === 2
      && toolDividers[0].label.includes('模型重置')
      && toolDividers[0].hint.includes('交接-带任务.md')
      && toolDividers[1].hint.includes('交接-无任务.md'),
      JSON.stringify(toolDividers));
    check('两条分割线都带「填入种子消息」',
      toolDividers.every((row) => row.hasSeed === true), JSON.stringify(toolDividers));

    // 有后台任务：种子消息含任务清单
    await page.locator('#messages .message-row.session-divider [data-fill-reset-seed]').first().click();
    await page.waitForTimeout(400);
    const seedWithTasks = await page.inputValue('#messageInput');
    check('种子消息带交接文档路径',
      seedWithTasks.includes('交接-带任务.md') && seedWithTasks.includes('请先读取该交接文档再继续'),
      JSON.stringify(seedWithTasks));
    check('种子消息带后台任务提醒（数量 + 清单）',
      seedWithTasks.includes('当前仍有 2 个任务在运行')
      && seedWithTasks.includes('job_1') && seedWithTasks.includes('渲染第 3 批')
      && seedWithTasks.includes('job_2'), JSON.stringify(seedWithTasks));
    check('填入后发送按钮可用（未自动发送）',
      await page.evaluate(() => !document.querySelector('#sendButton')?.disabled),
      String(await page.evaluate(() => document.querySelector('#sendButton')?.disabled)));

    // 无后台任务：含占位符的整行自动去掉
    await page.locator('#messages .message-row.session-divider [data-fill-reset-seed]').last().click();
    await page.waitForTimeout(400);
    const seedWithoutTasks = await page.inputValue('#messageInput');
    check('无后台任务时去掉任务提醒整行',
      seedWithoutTasks.includes('交接-无任务.md')
      && !seedWithoutTasks.includes('[后台任务]')
      && !seedWithoutTasks.includes('{task'), JSON.stringify(seedWithoutTasks));
    await page.fill('#messageInput', '');

    check('零页面错误 / console.error', pageErrors.length === 0, JSON.stringify(pageErrors.slice(0, 3)));
  } catch (error) {
    check('冒烟脚本自身异常', false, String(error && error.message ? error.message : error));
  } finally {
    await browser.close();
  }

  console.log(failures.length ? `\nFAILED ${failures.length} 项：${failures.join(' | ')}` : '\nALL PASS');
  process.exit(failures.length ? 1 : 0);
})();
