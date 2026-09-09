// 发送按钮可用性冒烟（源码 server，端口 8799）：程序化插入文本后必须刷新为可发送。
// 覆盖：快捷消息插入（用户实测 bug）/ @ 引用插入 / 清空回到不可发送态 / 零页面错误。
// 运行：$env:NODE_PATH="<node_modules>"; node verify\send_state_smoke.cjs
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8799';
const QUICK_TEXT = '发送按钮冒烟：程序化插入的快捷消息';
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
  // 准备一条快捷消息（跑完删掉，保持库干净）
  await apiJson('/api/quick-messages', { method: 'POST', body: JSON.stringify({ text: QUICK_TEXT }) });
  const listing = await apiJson('/api/quick-messages');
  const entry = (listing.messages || []).find((item) => String(item.text || '') === QUICK_TEXT);
  const createdIndex = Number(entry?.index ?? -1);
  check('快捷消息已创建', createdIndex >= 0, JSON.stringify(listing).slice(0, 160));

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });

  const sendState = () => page.evaluate(() => {
    const button = document.querySelector('#sendButton');
    const input = document.querySelector('#messageInput');
    return {
      disabled: button ? button.disabled : null,
      title: button ? button.title : '',
      value: input ? input.value : '',
    };
  });

  // 侧栏虚拟列表：目标会话可能不在 DOM 里，先回顶再按视口步进滚动直到出现。
  async function ensureVisible(title) {
    await page.evaluate(() => { document.querySelector('#sidebarWorkspaceTree').scrollTop = 0; });
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
      if (state === 'found') { await page.waitForTimeout(180); return true; }
      if (state === 'end' || state === 'missing') return false;
      await page.waitForTimeout(140);
    }
    return false;
  }

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    // 前置：需要一个「已有消息」的会话（否则 #messages 里没有消息行）。
    // 自动打开的是最近更新的会话，可能是刚播种的空会话——按需点开一个带消息的。
    if (!(await page.$('#messages .message-row'))) {
      // 等侧栏渲染完成再展开分组（否则查不到任何分组、点了也没用）。
      await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { timeout: 20000 });
      // 分组默认折叠（只展开当前工作区）——先展开全部分组，目标会话才会进入虚拟窗口。
      for (let i = 0; i < 12; i++) {
        const clicked = await page.evaluate(() => {
          const group = [...document.querySelectorAll('#sidebarWorkspaceTree .workspace-group')]
            .find((el) => !el.classList.contains('expanded'));
          if (!group) return false;
          group.querySelector('.workspace-group-header')?.click();
          return true;
        });
        if (!clicked) break;
        await page.waitForTimeout(220);
      }
      const list = await apiJson('/api/conversations');
      let target = null;
      for (const item of (list.conversations || [])) {
        const detail = await apiJson(`/api/conversations/${item.id}`);
        if ((detail.messages || []).length) { target = String(item.title || ''); break; }
      }
      if (target && await ensureVisible(target)) {
        await page.click(`#sidebarWorkspaceTree .conversation-item:has-text("${target}") .conversation-open`);
        await page.waitForTimeout(800);
      }
    }
    await page.waitForSelector('#messages .message-row', { timeout: 20000 });
    await page.waitForTimeout(1200);

    // 清空输入框（真实键盘输入 → 触发 input 事件）
    await page.click('#messageInput');
    await page.keyboard.press('Control+A');
    await page.keyboard.press('Delete');
    await page.waitForTimeout(300);
    let state = await sendState();
    check('空输入 → 发送按钮禁用', state.disabled === true, JSON.stringify(state));

    // ① 快捷消息插入（用户实测 bug 路径）
    await page.click('#quickMessageButton');
    await page.waitForSelector('#quickMessagePanel .quick-msg-item', { timeout: 10000 });
    await page.click(`#quickMessagePanel .quick-msg-item[data-quick-index="${createdIndex}"]`).catch(async () => {
      await page.click('#quickMessagePanel .quick-msg-item');
    });
    await page.waitForTimeout(500);
    state = await sendState();
    check('快捷消息插入文本', state.value.includes(QUICK_TEXT.slice(0, 8)), state.value);
    check('快捷消息插入后 → 发送按钮可点（用户实测 bug）', state.disabled === false, JSON.stringify(state));

    // ② 清空后回到禁用态（确认状态机仍双向）
    await page.click('#messageInput');
    await page.keyboard.press('Control+A');
    await page.keyboard.press('Delete');
    await page.waitForTimeout(300);
    state = await sendState();
    check('再次清空 → 发送按钮禁用', state.disabled === true, JSON.stringify(state));

    // ③ @ 引用插入（同一类程序化改值）
    await page.click('#messageInput');
    await page.keyboard.type('@');
    await page.waitForTimeout(800);
    const hasFileItem = await page.evaluate(() => Boolean(document.querySelector('#filePopup [data-file-index], #filePopup .file-popup-item')));
    if (hasFileItem) {
      await page.keyboard.press('Tab');
      await page.waitForTimeout(400);
      state = await sendState();
      check('@ 引用插入后 → 发送按钮可点', state.disabled === false, JSON.stringify(state));
    } else {
      console.log('SKIP  @ 引用插入（当前会话工作区无可浏览条目）');
    }

    check('零 pageerror / console.error', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, String(error && error.message));
  } finally {
    await browser.close();
    if (createdIndex >= 0) await apiJson(`/api/quick-messages/${createdIndex}`, { method: 'DELETE' }).catch(() => {});
  }

  console.log();
  console.log(`发送按钮状态冒烟结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exit(failures.length ? 1 : 0);
})();
