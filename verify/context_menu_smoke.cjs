// 文本框右键菜单冒烟（源码 server，端口 8790）。
// 覆盖：会话页/设置页/嵌套弹层里的文本框右键都能看到菜单且盖在弹层之上（top layer）/
//       菜单条目齐全（撤销 重做 剪切 复制 粘贴 删除 全选）/「全选」与「复制」真的生效/
//       弹层里右键不会误弹底层会话页的「复制选中 / 快速发送」菜单/零页面错误。
// 运行：$env:NODE_PATH="<node_modules 目录>"; node verify\context_menu_smoke.cjs
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8790';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function menuSnapshot(page) {
  return page.evaluate(() => {
    const menu = document.querySelector('#textContextMenu');
    if (!menu) return { exists: false };
    const rect = menu.getBoundingClientRect();
    const top = rect.width > 0
      ? document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)
      : null;
    return {
      exists: true,
      hidden: menu.hidden,
      parent: menu.parentElement ? `${menu.parentElement.tagName.toLowerCase()}#${menu.parentElement.id || ''}` : '',
      items: [...menu.querySelectorAll('button')].map((b) => b.textContent.trim()),
      onTop: Boolean(top && (top === menu || menu.contains(top))),
      rect: { x: Math.round(rect.left), y: Math.round(rect.top), w: Math.round(rect.width), h: Math.round(rect.height) },
    };
  });
}

const EDIT_ITEMS = ['撤销', '重做', '剪切', '复制', '粘贴', '删除', '全选'];

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(900);

    // ---- 1) 会话输入框（无弹层）：基线 ----
    await page.click('#messageInput');
    await page.click('#messageInput', { button: 'right' });
    await page.waitForTimeout(350);
    let menu = await menuSnapshot(page);
    check('会话输入框右键弹出文本框菜单',
      menu.hidden === false && menu.onTop === true && menu.parent === 'body#', JSON.stringify(menu));
    check('菜单条目齐全（撤销/重做/剪切/复制/粘贴/删除/全选）',
      EDIT_ITEMS.every((item) => menu.items.includes(item)), JSON.stringify(menu.items));
    await page.keyboard.press('Escape');
    await page.waitForTimeout(200);

    // ---- 2) 注入一个 .message-body 选区（模拟底层会话页的残留选区）----
    await page.evaluate(() => {
      const holder = document.createElement('div');
      holder.id = 'smokeMessageBody';
      holder.className = 'message-body';
      holder.textContent = '冒烟选区文本';
      holder.style.cssText = 'position:fixed;left:8px;bottom:8px;width:120px;height:24px;opacity:.01;z-index:5';
      document.body.append(holder);
    });
    const selectMessageBody = () => page.evaluate(() => {
      const holder = document.querySelector('#smokeMessageBody');
      const range = document.createRange();
      range.selectNodeContents(holder);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    });
    await selectMessageBody();
    const selectionMenu = await page.evaluate(() => {
      const holder = document.querySelector('#smokeMessageBody');
      holder.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 40, clientY: 700 }));
      const menu = document.querySelector('#textContextMenu');
      return { hidden: menu.hidden, items: [...menu.querySelectorAll('button')].map((b) => b.textContent.trim()) };
    });
    check('会话页选区右键仍是「复制选中 / 快速发送」',
      selectionMenu.hidden === false && selectionMenu.items.includes('快速发送'), JSON.stringify(selectionMenu));
    await page.keyboard.press('Escape');

    // ---- 3) 设置页文本框：菜单必须可见且盖在弹层之上 ----
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('.settings-nav button[data-settings-tab="runtime"]');
    await page.waitForTimeout(400);
    await page.click('#commandTimeout', { button: 'right' });
    await page.waitForTimeout(400);
    menu = await menuSnapshot(page);
    check('设置页文本框右键弹出菜单', menu.hidden === false, JSON.stringify(menu));
    check('菜单挂在最上层模态弹层内部',
      menu.parent === 'dialog#settingsDialog', JSON.stringify(menu.parent));
    check('菜单真的盖在弹层之上（命中测试通过）', menu.onTop === true, JSON.stringify(menu));
    check('设置页菜单同样是文本框条目（含剪切/粘贴/全选）',
      EDIT_ITEMS.every((item) => menu.items.includes(item)), JSON.stringify(menu.items));

    // ---- 4) 弹层里右键不能误弹会话页的选区菜单 ----
    await page.evaluate(() => {
      const dialog = document.querySelector('#settingsDialog');
      dialog.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 400, clientY: 400 }));
    });
    await page.waitForTimeout(300);
    menu = await menuSnapshot(page);
    check('弹层内右键不再误弹「复制选中 / 快速发送」',
      menu.hidden === true || !menu.items.includes('快速发送'), JSON.stringify(menu));

    // ---- 5) 嵌套弹层（设置 → Agent 卡片）也要正常 + 菜单动作真的生效 ----
    await page.click('.settings-nav button[data-settings-tab="agent"]');
    await page.waitForSelector('#agentCards .agent-card', { timeout: 10000 });
    await page.click('#agentCards [data-agent-card] .agent-card-name');
    await page.waitForSelector('#agentDialog[open]', { timeout: 10000 });
    await page.waitForTimeout(500);
    await page.click('#agentName', { button: 'right' });
    await page.waitForTimeout(400);
    menu = await menuSnapshot(page);
    check('嵌套弹层里菜单挂到最上层弹层且可见',
      menu.hidden === false && menu.parent === 'dialog#agentDialog' && menu.onTop === true,
      JSON.stringify(menu));

    // 「全选」→ 文本框内容全选（#agentName 是 text 输入框，支持 selection API）
    await page.click('#agentName');
    await page.fill('#agentName', '右键冒烟');
    await page.click('#agentName', { button: 'right' });
    await page.waitForTimeout(300);
    await page.click('#textContextMenu button[data-context-action="select-all"]');
    await page.waitForTimeout(300);
    const selectionRange = await page.evaluate(() => {
      const input = document.querySelector('#agentName');
      return { start: input.selectionStart, end: input.selectionEnd, len: String(input.value).length };
    });
    check('「全选」选中了文本框全部内容',
      selectionRange.start === 0 && selectionRange.end === selectionRange.len && selectionRange.len > 0,
      JSON.stringify(selectionRange));

    // 「复制」→ 剪贴板写入成功（走 copyText：clipboard API 或 execCommand 兜底）
    await page.click('#agentName', { button: 'right' });
    await page.waitForTimeout(300);
    await page.click('#textContextMenu button[data-context-action="copy"]');
    await page.waitForTimeout(700);
    const toastText = await page.evaluate(() => document.querySelector('#toast')?.textContent || '');
    check('「复制」给出成功反馈', toastText.includes('已复制'), toastText);

    // number 输入框没有 selection API：复制退化为整值，不能提示"没有可复制的内容"
    await page.keyboard.press('Escape');
    await page.click('#cancelAgent');
    await page.waitForTimeout(400);
    await page.click('.settings-nav button[data-settings-tab="runtime"]');
    await page.waitForTimeout(300);
    await page.click('#commandTimeout', { button: 'right' });
    await page.waitForTimeout(300);
    await page.click('#textContextMenu button[data-context-action="copy"]');
    await page.waitForTimeout(600);
    const numberToast = await page.evaluate(() => document.querySelector('#toast')?.textContent || '');
    check('number 输入框「复制」退化为整值复制',
      numberToast.includes('已复制'), numberToast);

    await page.evaluate(() => document.querySelector('#smokeMessageBody')?.remove());
    check('零 pageerror / console.error', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, String(error && error.stack ? error.stack.split('\n')[0] : error));
  } finally {
    await browser.close();
  }

  console.log();
  console.log(`右键菜单冒烟结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exitCode = failures.length ? 1 : 0;
})();
