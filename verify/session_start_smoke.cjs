// 「新会话开始」边界冒烟（由 verify/session_start_smoke.py 编排，源码 server 8798）。
// 覆盖：入口按钮 → 确认 → 分隔条就地渲染 → 后端落 role=session 标记（旧消息一条不删）
//       → 撤销 → 分隔条与标记一起消失 → 零页面错误。
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
  return page.evaluate(() => ({
    rows: document.querySelectorAll('#messages .message-row[data-message-id]').length,
    dividers: document.querySelectorAll('#messages .message-row.session-divider').length,
    label: document.querySelector('#messages .session-divider-label')?.textContent.trim() || '',
    hasCancel: Boolean(document.querySelector('#messages [data-cancel-session-start]')),
    dividerIsLast: (() => {
      const rows = [...document.querySelectorAll('#messages .message-row[data-message-id]')];
      return rows.length > 0 && rows[rows.length - 1].classList.contains('session-divider');
    })(),
  }));
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
    check('打开播种会话（4 条消息、无分隔条）',
      (await domSnapshot(page)).rows === 4 && (await domSnapshot(page)).dividers === 0,
      JSON.stringify(await domSnapshot(page)));

    // ① 点「新会话」→ 分隔条就地出现
    check('输入区有「新会话」入口', await page.evaluate(() => Boolean(document.querySelector('#newSessionButton'))));
    await page.click('#newSessionButton');
    await page.waitForTimeout(900);
    const afterAdd = await domSnapshot(page);
    check('点按钮后出现分隔条且位于末尾',
      afterAdd.dividers === 1 && afterAdd.dividerIsLast, JSON.stringify(afterAdd));
    check('分隔条文案标明来源', afterAdd.label.startsWith('新会话开始 · 手动重置'), afterAdd.label);
    check('分隔条带「撤销」入口', afterAdd.hasCancel === true, JSON.stringify(afterAdd));
    check('旧消息一条不删（4 + 分隔条 = 5 行）', afterAdd.rows === 5, JSON.stringify(afterAdd));

    // ② 后端确实落了 role=session 标记
    const messages = await messagesOf(conversationId);
    const marker = messages[messages.length - 1] || {};
    check('后端落库 role=session 标记', marker.role === 'session', JSON.stringify(marker));
    check('标记带 session_start 元数据（source=manual）',
      String(marker.metadata?.session_start?.source || '') === 'manual'
      && Boolean(marker.metadata?.session_start?.at),
      JSON.stringify(marker.metadata || {}));
    check('标记无正文（不占模型内容）', String(marker.content || '') === '', JSON.stringify(marker.content));
    check('旧消息仍是前 4 条且角色顺序不变',
      JSON.stringify(messages.slice(0, 4).map((m) => m.role)) === JSON.stringify(['user', 'assistant', 'user', 'assistant']),
      JSON.stringify(messages.map((m) => m.role)));

    // ③ 刷新页面：分隔条仍在（持久化，不是内存态）
    await page.reload({ waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messages .message-row[data-message-id]', { timeout: 20000 });
    await page.waitForTimeout(800);
    const afterReload = await domSnapshot(page);
    check('刷新后分隔条仍在（已持久化）', afterReload.dividers === 1 && afterReload.rows === 5,
      JSON.stringify(afterReload));

    // ④ 撤销 → 分隔条与标记一起消失
    await page.click('#messages [data-cancel-session-start]');
    await page.waitForTimeout(900);
    const afterCancel = await domSnapshot(page);
    check('撤销后分隔条消失、消息仍是 4 条', afterCancel.dividers === 0 && afterCancel.rows === 4,
      JSON.stringify(afterCancel));
    const remaining = await messagesOf(conversationId);
    check('撤销后后端不再有 session 标记',
      remaining.length === 4 && remaining.every((m) => m.role !== 'session'),
      JSON.stringify(remaining.map((m) => m.role)));

    check('零页面错误 / console.error', pageErrors.length === 0, JSON.stringify(pageErrors.slice(0, 3)));
  } catch (error) {
    check('冒烟脚本自身异常', false, String(error && error.message ? error.message : error));
  } finally {
    await browser.close();
  }

  console.log(failures.length ? `\nFAILED ${failures.length} 项：${failures.join(' | ')}` : '\nALL PASS');
  process.exit(failures.length ? 1 : 0);
})();
