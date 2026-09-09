// 分支对话继承「首轮上下文」折叠卡冒烟（由 branch_first_turn_smoke.py 起独立源码 server 后调用）。
// 覆盖：原会话顶部显示折叠卡 → 点第二条用户消息的「分支」→ 新会话顶部同样显示，
//       内容与源会话一致、历史为分支点之前的消息、零页面错误。
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8795';
const SYSTEM_TEXT = process.env.NAIBA_FIRST_TURN_TEXT || '';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function cardSnapshot(page) {
  return page.evaluate(() => {
    const card = document.querySelector('#messages .first-turn-card');
    return {
      exists: Boolean(card),
      summary: card ? (card.querySelector('summary')?.textContent.trim() || '') : '',
      text: card ? (card.textContent || '') : '',
      rows: document.querySelectorAll('#messages .message-row').length,
      branchButtons: document.querySelectorAll('#messages [data-branch-message]').length,
    };
  });
}

async function waitForCard(page, timeout = 15000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const snap = await cardSnapshot(page);
    if (snap.exists) return snap;
    await page.waitForTimeout(250);
  }
  return cardSnapshot(page);
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
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    const before = await waitForCard(page);
    check('原会话顶部显示首轮上下文折叠卡', before.exists === true, JSON.stringify(before));
    check('折叠卡含系统提示词原文', before.text.includes(SYSTEM_TEXT),
      JSON.stringify({ summary: before.summary, head: before.text.slice(0, 160) }));
    check('分支按钮只在用户消息上（2 条）', before.branchButtons === 2, JSON.stringify(before));
    check('原会话 4 条消息', before.rows === 4, String(before.rows));

    // 点第二条用户消息的「分支」
    await page.locator('#messages [data-branch-message]').nth(1).click();
    await page.waitForTimeout(1200);
    const after = await waitForCard(page);
    check('分支后新会话顶部同样显示折叠卡', after.exists === true, JSON.stringify(after));
    check('新会话折叠卡内容与源会话一致', after.text.includes(SYSTEM_TEXT),
      JSON.stringify({ summary: after.summary, head: after.text.slice(0, 160) }));
    check('新会话历史为分支点之前（2 条消息）', after.rows === 2, String(after.rows));
    check('新会话分支按钮 1 个（只有复制的用户消息）', after.branchButtons === 1, String(after.branchButtons));
    check('零页面错误 / console.error', pageErrors.length === 0, JSON.stringify(pageErrors.slice(0, 3)));
  } catch (error) {
    check('冒烟脚本自身异常', false, String(error && error.message ? error.message : error));
  } finally {
    await browser.close();
  }

  console.log(failures.length ? `\nFAILED ${failures.length} 项：${failures.join(' | ')}` : '\nALL PASS');
  process.exit(failures.length ? 1 : 0);
})();
