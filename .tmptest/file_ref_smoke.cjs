// @ 工作区引用弹层 UI 冒烟（源码 server，端口 8799）。
// 运行：$env:NODE_PATH="%USERPROFILE%\node_modules"; node .tmptest\file_ref_smoke.cjs
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:8799';
const WS = path.join(__dirname, 'fr_ws');

const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

function prepareWorkspace() {
  fs.rmSync(WS, { recursive: true, force: true });
  fs.mkdirSync(path.join(WS, 'docs', 'api'), { recursive: true });
  fs.writeFileSync(path.join(WS, 'notes.md'), 'note');
  fs.writeFileSync(path.join(WS, 'docs', 'guide.md'), 'guide');
  fs.writeFileSync(path.join(WS, 'docs', 'api', 'README.md'), 'readme');
  fs.writeFileSync(path.join(WS, '.hidden.md'), 'hidden');
}

(async () => {
  prepareWorkspace();
  const created = await fetch(`${BASE}/api/conversations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: 'FR冒烟', workspace_dir: WS }),
  }).then((r) => r.json());
  const conversationId = String(created.id || '');
  check('冒烟会话已创建', Boolean(conversationId), JSON.stringify(created).slice(0, 200));

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });
  let chatBody = null;
  page.on('request', (req) => {
    if (req.url().endsWith('/api/chat') && req.method() === 'POST') chatBody = req.postDataJSON();
  });

  const popupText = () => page.evaluate(() => {
    const popup = document.querySelector('#filePopup');
    if (!popup || popup.hidden) return null;
    return {
      crumb: popup.querySelector('.file-popup-crumb')?.textContent || '',
      names: [...popup.querySelectorAll('.file-popup-name')].map((el) => el.textContent),
      kinds: [...popup.querySelectorAll('.file-popup-item')].map((el) => el.dataset.kind),
      tabVisible: (() => {
        const tab = popup.querySelector('.file-popup-item.selected .file-popup-tab');
        return tab ? getComputedStyle(tab).display !== 'none' : false;
      })(),
    };
  });
  const inputValue = () => page.inputValue('#messageInput');

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 15000 });
    await page.waitForTimeout(2000);

    // ① 输入 @ → 列出工作区根目录（目录优先、隐藏点号条目）
    await page.fill('#messageInput', '@');
    await page.waitForSelector('#filePopup:not([hidden]) .file-popup-item', { timeout: 15000 });
    let state = await popupText();
    check('@ 触发弹层', Boolean(state), JSON.stringify(state));
    check('根目录列出 docs 与 notes.md', Boolean(state && state.names.includes('docs') && state.names.includes('notes.md')), JSON.stringify(state?.names));
    check('隐藏 .hidden.md', Boolean(state && !state.names.includes('.hidden.md')), JSON.stringify(state?.names));
    check('目录优先（首项为目录）', Boolean(state && state.kinds[0] === 'directory'), JSON.stringify(state?.kinds));
    check('选中目录行显示 Tab 提示', Boolean(state && state.tabVisible), JSON.stringify(state));
    check('根目录面包屑', Boolean(state && state.crumb.includes('根目录')), state?.crumb);

    // ② Tab 进入 docs（Shift+Tab 返回根目录）
    await page.press('#messageInput', 'Tab');
    await page.waitForTimeout(600);
    state = await popupText();
    check('Tab 进入 docs', Boolean(state && state.crumb.includes('docs')), state?.crumb);
    check('docs 内列出 api 与 guide.md', Boolean(state && state.names.includes('api') && state.names.includes('guide.md')), JSON.stringify(state?.names));
    check('进入后输入框 token 同步为 @docs/', (await inputValue()) === '@docs/', await inputValue());
    check('首项为「返回上一级」', Boolean(state && state.kinds[0] === 'parent'), JSON.stringify(state?.kinds));

    await page.press('#messageInput', 'Shift+Tab');
    await page.waitForTimeout(600);
    state = await popupText();
    check('Shift+Tab 返回根目录', Boolean(state && state.crumb.includes('根目录')), state?.crumb);
    check('返回后 token 回到 @', (await inputValue()) === '@', await inputValue());

    // ③ 手机端路径：点击目录行右侧箭头进入目录
    await page.click('#filePopup .file-popup-item[data-kind="directory"] .file-popup-enter');
    await page.waitForTimeout(600);
    state = await popupText();
    check('点击箭头进入 docs', Boolean(state && state.crumb.includes('docs')), state?.crumb);

    // ④ 点击目录行（非箭头）= 引用该目录
    await page.click('#filePopup .file-popup-item[data-kind="directory"] .file-popup-name');
    await page.waitForTimeout(400);
    check('点击目录行引用该目录', (await inputValue()) === '@docs/api/ ', await inputValue());
    check('引用后弹层关闭', (await popupText()) === null, '仍显示');

    // ⑤ 逐级进入后按 Enter 引用文件
    await page.fill('#messageInput', '@docs/api/');
    await page.waitForSelector('#filePopup:not([hidden]) .file-popup-item', { timeout: 10000 });
    await page.press('#messageInput', 'ArrowDown');
    await page.press('#messageInput', 'Enter');
    await page.waitForTimeout(400);
    check('Enter 引用文件', (await inputValue()) === '@docs/api/README.md ', await inputValue());

    // ⑥ 引用高亮（镜像层）+ 发送时后端解析为绝对路径
    await page.fill('#messageInput', '处理 @docs/api/ 下的文件');
    await page.waitForTimeout(300);
    const mirror = await page.evaluate(() => {
      const el = document.querySelector('#inputMirror .file-ref');
      return el ? el.textContent : null;
    });
    check('镜像层高亮 @ 引用', mirror === '@docs/api/', String(mirror));
    check('发送按钮可点（有文字）', (await page.isDisabled('#sendButton')) === false);

    await page.click('#sendButton');
    await page.waitForTimeout(2500);
    check('已发出 /api/chat', Boolean(chatBody), JSON.stringify(chatBody).slice(0, 200));
    if (chatBody) {
      check('请求 display_message 保留 @ 原文', String(chatBody.display_message || '').includes('@docs/api/'), String(chatBody.display_message));
    }
    // 落库的模型可见文本由后端解析为绝对路径（前端只发原文）。
    const stored = await fetch(`${BASE}/api/conversations/${conversationId}`).then((r) => r.json());
    const storedUser = (stored.messages || []).filter((m) => m.role === 'user').pop() || {};
    const expected = `${path.join(WS, 'docs', 'api')}\\`;
    check('落库内容含目录绝对路径', String(storedUser.content || '').includes(expected), String(storedUser.content));
    check('落库展示文本保留 @ 原文', String((storedUser.metadata || {}).display_content || '').includes('@docs/api/'), JSON.stringify((storedUser.metadata || {}).display_content));
    const bubble = await page.evaluate(() => {
      const row = document.querySelector('#messages .message-row.user');
      const ref = row?.querySelector('.message-body .file-ref');
      return { ref: ref ? ref.textContent : null, text: (row?.querySelector('.message-body p')?.textContent || '').trim() };
    });
    check('气泡高亮 @ 引用', bubble.ref === '@docs/api/', JSON.stringify(bubble));
    check('气泡正文保留 @ 原文', bubble.text.includes('@docs/api/'), JSON.stringify(bubble));

    // ⑦ 打字过滤当前目录
    await page.fill('#messageInput', '@no');
    await page.waitForSelector('#filePopup:not([hidden]) .file-popup-item', { timeout: 10000 });
    state = await popupText();
    check('@no 过滤出 notes.md', Boolean(state && state.names.includes('notes.md') && !state.names.includes('docs')), JSON.stringify(state?.names));

    // ⑧ 邮箱不被当作引用；不存在的路径只是"没有匹配项"，不会被误替换（后端解析）
    await page.fill('#messageInput', '联系 a@b.com 或 @不存在.md');
    await page.waitForSelector('#filePopup:not([hidden])', { timeout: 10000 });
    state = await popupText();
    check('不存在的路径无匹配项', Boolean(state && state.names.length === 0), JSON.stringify(state?.names));
    await page.fill('#messageInput', '联系 a@b.com');
    await page.waitForTimeout(400);
    check('邮箱不触发弹层', (await popupText()) === null, '弹层仍显示');

    check('零页面错误', pageErrors.length === 0, pageErrors.join(' | '));
    await page.screenshot({ path: path.join(__dirname, 'file_ref_smoke.png') });
  } catch (error) {
    failures.push(`执行异常: ${error.message}`);
    console.log('执行异常:', error.message);
  }

  await browser.close();
  console.log('');
  console.log('@ 引用 UI 冒烟结果：', failures.length ? `${failures.length} 项失败 -> ${JSON.stringify(failures)}` : '全部通过');
  process.exit(failures.length ? 1 : 0);
})();
