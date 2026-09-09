// 开始页预设可编辑/可删除 + 思考按钮交互冒烟（源码 server，端口 8799）。
// 运行：$env:NODE_PATH="<node_modules>"; node verify\starter_reasoning_smoke.cjs
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8799';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function apiJson(pathname, options = {}) {
  const response = await fetch(`${BASE}${pathname}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  return response.json().catch(() => ({}));
}

(async () => {
  // 建一个空会话并让它成为"最近会话"（页面加载后自动打开 → 显示开始页卡片）
  const created = await apiJson('/api/conversations', { method: 'POST', body: JSON.stringify({ title: 'UI 冒烟：开始页与思考按钮' }) });
  const conversationId = String(created.id || '');
  check('空会话已创建', Boolean(conversationId), JSON.stringify(created).slice(0, 120));

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });

  const cardState = (title = '列出可用工具') => page.evaluate((wanted) => {
    const cards = [...document.querySelectorAll('.starter-grid .custom-starter')];
    const target = cards.find((card) => (card.querySelector('.starter-title')?.textContent || '').includes(wanted));
    if (!target) return null;
    const edit = target.querySelector('.starter-edit');
    const del = target.querySelector('.starter-del');
    return {
      title: target.querySelector('.starter-title')?.textContent || '',
      desc: target.querySelector('.starter-desc')?.textContent || '',
      hasEdit: Boolean(edit),
      hasDelete: Boolean(del),
      editDisplay: edit ? getComputedStyle(edit).display : '',
      delDisplay: del ? getComputedStyle(del).display : '',
      total: cards.length,
    };
  }, title);
  const reasoningState = () => page.evaluate(() => {
    const menu = document.querySelector('#reasoningMenu');
    const active = menu ? [...menu.querySelectorAll('[data-reasoning-effort]')].filter((b) => b.classList.contains('is-active')).map((b) => b.dataset.reasoningEffort) : [];
    const label = document.querySelector('#reasoningLabel');
    return {
      menuHidden: menu ? menu.hidden : null,
      label: label ? label.textContent.trim() : '',
      // 等级文字必须在输入框外的 meta 行里（与「就绪」同一行），不能在 .composer 内
      labelInMeta: Boolean(label && label.closest('.composer-meta')),
      labelInComposer: Boolean(label && label.closest('.composer')),
      active,
      ariaExpanded: document.querySelector('#deepReasoningButton')?.getAttribute('aria-expanded'),
    };
  });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('.starter-grid .custom-starter', { timeout: 20000 });
    await page.waitForTimeout(800);

    // ---- A. 开始页预设：可编辑 / 可删除 / 可恢复 ----
    const CARD = '.starter-grid .custom-starter:has(.starter-title:text-is("列出可用工具"))';
    let card = await cardState();
    check('内置预设卡片已渲染', Boolean(card), JSON.stringify(card));
    check('内置预设带副标题', card && card.desc === '查看当前能力', JSON.stringify(card));
    check('内置预设带编辑/删除按钮', Boolean(card && card.hasEdit && card.hasDelete), JSON.stringify(card));

    await page.hover(CARD);
    await page.waitForTimeout(300);
    card = await cardState();
    check('悬停后编辑/删除按钮可见', card && card.editDisplay !== 'none' && card.delDisplay !== 'none', JSON.stringify(card));

    // 编辑内置预设：改名后卡片应更新，且副标题保留
    await page.click(`${CARD} .starter-edit`);
    await page.waitForSelector('#starterPromptDialog[open]', { timeout: 5000 });
    const dialogTitle = await page.inputValue('#starterPromptTitle');
    check('编辑弹窗回填内置预设标题', dialogTitle === '列出可用工具', dialogTitle);
    await page.fill('#starterPromptTitle', '列出我的工具');
    await page.click('#saveStarterPrompt');
    await page.waitForTimeout(800);
    card = await cardState('列出我的工具');
    check('编辑内置预设后标题更新', Boolean(card) && card.title === '列出我的工具', JSON.stringify(card));
    check('编辑后副标题保留', Boolean(card) && card.desc === '查看当前能力', JSON.stringify(card));

    // 删除内置预设 → 卡片消失 + 出现「恢复默认预设」
    const beforeDelete = (await cardState('列出我的工具'))?.total || 0;
    const RENAMED = '.starter-grid .custom-starter:has(.starter-title:text-is("列出我的工具"))';
    await page.hover(RENAMED);
    await page.waitForTimeout(200);
    await page.click(`${RENAMED} .starter-del`);
    await page.waitForTimeout(800);
    card = await cardState('列出我的工具');
    check('删除内置预设后卡片消失', card === null, JSON.stringify(card));
    const restoreVisible = await page.evaluate(() => {
      const btn = document.querySelector('#starterRestoreBtn');
      return { hidden: btn ? btn.hidden : null, total: document.querySelectorAll('.starter-grid .custom-starter').length };
    });
    check('删除后出现「恢复默认预设」', restoreVisible.hidden === false, JSON.stringify(restoreVisible));
    check('删除后卡片数减一', restoreVisible.total === beforeDelete - 1, `${restoreVisible.total} vs ${beforeDelete - 1}`);

    // 恢复默认预设 → 卡片回来（标题为默认名）+ 按钮收起
    await page.click('#starterRestoreBtn');
    await page.waitForTimeout(1000);
    card = await cardState('列出可用工具');
    check('恢复默认预设后卡片回来', Boolean(card) && card.title === '列出可用工具', JSON.stringify(card));
    const restoreAfter = await page.evaluate(() => document.querySelector('#starterRestoreBtn')?.hidden);
    check('恢复后按钮收起', restoreAfter === true, String(restoreAfter));

    // ---- B. 思考按钮：点击只开合列表，不动等级；列表高亮当前等级；等级文字在输入框外的 meta 行 ----
    let reasoning = await reasoningState();
    check('meta 行显示当前思考强度', reasoning.label === '思考 自动', JSON.stringify(reasoning));
    check('等级文字在输入框外（.composer-meta 内）', reasoning.labelInMeta === true, JSON.stringify(reasoning));
    check('等级文字不在输入框内（.composer）', reasoning.labelInComposer === false, JSON.stringify(reasoning));

    await page.click('#deepReasoningButton');
    await page.waitForTimeout(300);
    reasoning = await reasoningState();
    check('点击 → 菜单打开', reasoning.menuHidden === false, JSON.stringify(reasoning));
    check('菜单高亮当前等级（自动）', reasoning.active.length === 1 && reasoning.active[0] === 'auto', JSON.stringify(reasoning));
    check('aria-expanded=true', reasoning.ariaExpanded === 'true', JSON.stringify(reasoning));

    // 再点一次：只收起，等级不变（用户实测 bug：原来会顺带切档）
    await page.click('#deepReasoningButton');
    await page.waitForTimeout(300);
    reasoning = await reasoningState();
    check('再点 → 菜单收起', reasoning.menuHidden === true, JSON.stringify(reasoning));
    check('收起后等级不变（仍是自动）', reasoning.label === '思考 自动', JSON.stringify(reasoning));
    check('收起后 aria-expanded=false', reasoning.ariaExpanded === 'false', JSON.stringify(reasoning));

    // 选择「高」→ 菜单收起、标签变高、重新打开后高亮「高」
    await page.click('#deepReasoningButton');
    await page.waitForTimeout(200);
    await page.click('#reasoningMenu [data-reasoning-effort="high"]');
    await page.waitForTimeout(800);
    reasoning = await reasoningState();
    check('选择「高」后标签更新', reasoning.label === '思考 高', JSON.stringify(reasoning));
    check('选择后菜单收起', reasoning.menuHidden === true, JSON.stringify(reasoning));
    await page.click('#deepReasoningButton');
    await page.waitForTimeout(300);
    reasoning = await reasoningState();
    check('重新打开高亮「高」', reasoning.active.length === 1 && reasoning.active[0] === 'high', JSON.stringify(reasoning));
    // 点空白处收起
    await page.mouse.click(20, 400);
    await page.waitForTimeout(300);
    reasoning = await reasoningState();
    check('点空白处收起菜单', reasoning.menuHidden === true, JSON.stringify(reasoning));

    check('零 pageerror / console.error', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, String(error && error.message));
  } finally {
    await browser.close();
    if (conversationId) await apiJson(`/api/conversations/${conversationId}`, { method: 'DELETE' }).catch(() => {});
  }

  console.log();
  console.log(`开始页/思考按钮冒烟结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exitCode = failures.length ? 1 : 0;
})();
