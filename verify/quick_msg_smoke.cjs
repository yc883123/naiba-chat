// 快捷消息面板 + 轻量/富文本移除 + 两列表独立（源码 server，端口 8799）。
// 运行：$env:NODE_PATH="%USERPROFILE%\node_modules"; node verify\quick_msg_smoke.cjs
const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:8799';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function apiJson(path, options = {}) {
  const response = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  return response.json().catch(() => ({}));
}

// 按 index 倒序删除，避免删除后索引左移导致误删（off-by-one）。
// 开始页列表不带 index 字段（其形状保持 {title,text}），回退用数组下标。
async function purge(path, key) {
  const listing = await apiJson(path);
  const targets = (listing[key] || [])
    .map((item, arrayIndex) => ({
      ...item,
      _index: Number.isFinite(Number(item.index)) ? Number(item.index) : arrayIndex,
    }))
    .filter((item) => String(item.text || '').startsWith('冒烟') || String(item.text || '').startsWith('开始页'))
    .sort((a, b) => b._index - a._index);
  for (const item of targets) {
    const result = await apiJson(`${path}/${item._index}`, { method: 'DELETE' });
    console.log(`  [purge] ${path}/${item._index} ${String(item.text || '').slice(0, 12)} -> ${JSON.stringify(result).slice(0, 60)}`);
  }
}

(async () => {
  // 清理残留，再建：开始页 2 条（开始页A/B）+ 快捷消息 3 条（冒烟A/B/C，只有正文）
  await purge('/api/starter-prompts', 'prompts');
  await purge('/api/quick-messages', 'messages');
  for (const name of ['开始页A', '开始页B']) {
    await apiJson('/api/starter-prompts', { method: 'POST', body: JSON.stringify({ title: name, text: `${name} 的内容` }) });
  }
  for (const name of ['冒烟A', '冒烟B', '冒烟C']) {
    await apiJson('/api/quick-messages', { method: 'POST', body: JSON.stringify({ text: `${name} 的内容` }) });
    await new Promise((r) => setTimeout(r, 30));
  }

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  const useCalls = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });
  page.on('response', (res) => { if (res.url().includes('/use')) useCalls.push(`${res.status()} ${res.url()}`); });

  const titles = () => page.evaluate(() =>
    [...document.querySelectorAll('#quickMessageList .quick-msg-main b')].map((el) => el.textContent));

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#quickMessageButton', { timeout: 15000 });
    await page.waitForTimeout(2000);

    // ① 轻量 / 富文本 两个控件已移除
    const removed = await page.evaluate(() => ({
      lightweight: Boolean(document.querySelector('#composerMetaMoreButton') || document.querySelector('#lightweightToolsToggle')),
      richText: Boolean(document.querySelector('#richTextToggle')),
      ruleBar: Boolean(document.querySelector('#conversationRuleBar')),
      panel: Boolean(document.querySelector('#composerMetaPanel')),
      labels: document.body.innerText.includes('轻量') || document.body.innerText.includes('富文本'),
    }));
    check('轻量按钮已移除', removed.lightweight === false, JSON.stringify(removed));
    check('富文本开关已移除', removed.richText === false, JSON.stringify(removed));
    check('轻量面板容器已移除', removed.panel === false, JSON.stringify(removed));
    check('页面不再出现「轻量」「富文本」文案', removed.labels === false, JSON.stringify(removed));
    check('规则条仍不存在', removed.ruleBar === false, JSON.stringify(removed));

    // ② 面板只列快捷消息（不含开始页自定义指令），行内文本即正文首行
    await page.click('#quickMessageButton');
    await page.waitForSelector('#quickMessagePanel:not([hidden]) .quick-msg-item', { timeout: 10000 });
    let order = await titles();
    check('面板列出 3 条快捷消息', order.length === 3, JSON.stringify(order));
    check('面板不含开始页自定义指令', !order.some((t) => String(t).startsWith('开始页')), JSON.stringify(order));
    check('行内显示正文（无标题）', order.every((t) => String(t).endsWith('的内容')), JSON.stringify(order));
    check('同权重按新增时间倒序（C→B→A）', JSON.stringify(order) === JSON.stringify(['冒烟C 的内容', '冒烟B 的内容', '冒烟A 的内容']), JSON.stringify(order));

    // ③ 点击插入 + 使用次数累加（等 /use 响应，避免与 fire-and-forget 竞态）
    const useResponse = page.waitForResponse((res) => res.url().includes('/use'), { timeout: 8000 }).catch(() => null);
    await page.click('#quickMessageList .quick-msg-item:has-text("冒烟A")');
    await useResponse;
    await page.waitForTimeout(400);
    check('点击插入到输入框', (await page.inputValue('#messageInput')).includes('冒烟A 的内容'), await page.inputValue('#messageInput'));
    check('插入后面板关闭', await page.isHidden('#quickMessagePanel'), '面板仍显示');
    const used = (await apiJson('/api/quick-messages')).messages.find((item) => item.text === '冒烟A 的内容');
    console.log('  诊断 use 请求:', JSON.stringify(useCalls));
    check('使用次数已累加', used && used.count === 1, JSON.stringify(used));

    // ④ 重新打开：按 次数×2 + 新鲜度 排序（A 8 > C 6 = B 6，并列取新增时间倒序）
    await page.click('#quickMessageButton');
    await page.waitForFunction(
      () => document.querySelector('#quickMessageList .quick-msg-main b')?.textContent === '冒烟A 的内容',
      null, { timeout: 10000 });
    order = await titles();
    check('使用过的条目排到最前（A→C→B）', JSON.stringify(order) === JSON.stringify(['冒烟A 的内容', '冒烟C 的内容', '冒烟B 的内容']), JSON.stringify(order));

    // ⑤ 编辑弹窗：快捷消息不设标题字段
    await page.click('#quickMessageList .quick-msg-item:has-text("冒烟B") [data-quick-edit]');
    await page.waitForTimeout(500);
    const dialog = await page.evaluate(() => ({
      open: document.querySelector('#starterPromptDialog')?.open === true,
      heading: document.querySelector('#starterPromptHeading')?.textContent || '',
      titleHidden: document.querySelector('#starterPromptTitleField')?.hidden === true,
      textLabel: document.querySelector('#starterPromptTextLabel')?.textContent || '',
      text: document.querySelector('#starterPromptText')?.value || '',
    }));
    check('编辑弹窗已打开', dialog.open, JSON.stringify(dialog));
    check('弹窗标题为「编辑快捷消息」', dialog.heading === '编辑快捷消息', JSON.stringify(dialog));
    check('快捷消息隐藏标题字段', dialog.titleHidden, JSON.stringify(dialog));
    check('正文标签为「正文」', dialog.textLabel === '正文', JSON.stringify(dialog));
    check('编辑弹窗回填正文', dialog.text === '冒烟B 的内容', JSON.stringify(dialog));
    await page.click('#starterPromptDialog [data-close="starterPromptDialog"]');
    await page.waitForTimeout(400);

    // ⑤b 新建弹窗同样无标题字段
    if (await page.isHidden('#quickMessagePanel')) await page.click('#quickMessageButton');
    await page.waitForSelector('#quickMessagePanel:not([hidden]) .quick-msg-item', { timeout: 10000 });
    await page.click('#quickMessageAdd');
    await page.waitForTimeout(400);
    const addDialog = await page.evaluate(() => ({
      heading: document.querySelector('#starterPromptHeading')?.textContent || '',
      titleHidden: document.querySelector('#starterPromptTitleField')?.hidden === true,
    }));
    check('新建弹窗标题为「添加快捷消息」', addDialog.heading === '添加快捷消息', JSON.stringify(addDialog));
    check('新建弹窗隐藏标题字段', addDialog.titleHidden, JSON.stringify(addDialog));
    await page.click('#starterPromptDialog [data-close="starterPromptDialog"]');
    await page.waitForTimeout(400);

    // ⑥ 删除：只删快捷消息，不动开始页列表
    if (await page.isHidden('#quickMessagePanel')) await page.click('#quickMessageButton');
    await page.waitForSelector('#quickMessagePanel:not([hidden]) .quick-msg-item', { timeout: 10000 });
    await page.click('#quickMessageList .quick-msg-item:has-text("冒烟C") [data-quick-delete]');
    await page.waitForTimeout(800);
    order = await titles();
    check('删除后条目消失', !order.includes('冒烟C'), JSON.stringify(order));
    const starters = (await apiJson('/api/starter-prompts')).prompts.filter((i) => String(i.title).startsWith('开始页'));
    check('开始页自定义指令未受影响', starters.length === 2, JSON.stringify(starters.map((i) => i.title)));
    const startersShape = starters[0] || {};
    check('开始页条目无使用统计字段', !('count' in startersShape), JSON.stringify(startersShape));

    // ⑦ 开始新对话页：只显示开始页自定义指令卡片
    const created = await apiJson('/api/conversations', { method: 'POST', body: JSON.stringify({ title: '冒烟空会话' }) });
    await page.reload({ waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#emptyState', { timeout: 15000 });
    await page.waitForTimeout(1500);
    const cards = await page.evaluate(() =>
      [...document.querySelectorAll('.starter-grid .custom-starter .starter-title')].map((el) => el.textContent));
    check('开始页渲染自定义指令卡片', cards.includes('开始页A') && cards.includes('开始页B'), JSON.stringify(cards));
    check('开始页不显示快捷消息', !cards.some((t) => String(t).startsWith('冒烟')), JSON.stringify(cards));
    // 开始页编辑弹窗仍保留标题字段（编辑按钮悬停卡片才显示）
    await page.hover('.starter-grid .custom-starter');
    await page.waitForTimeout(300);
    await page.click('.starter-grid .custom-starter .starter-edit');
    await page.waitForTimeout(400);
    const starterDialog = await page.evaluate(() => ({
      heading: document.querySelector('#starterPromptHeading')?.textContent || '',
      titleHidden: document.querySelector('#starterPromptTitleField')?.hidden === true,
      title: document.querySelector('#starterPromptTitle')?.value || '',
    }));
    check('开始页弹窗标题为「编辑自定义指令」', starterDialog.heading === '编辑自定义指令', JSON.stringify(starterDialog));
    check('开始页弹窗保留标题字段', starterDialog.titleHidden === false, JSON.stringify(starterDialog));
    check('开始页弹窗回填标题', starterDialog.title === '开始页A', JSON.stringify(starterDialog));
    await page.click('#starterPromptDialog [data-close="starterPromptDialog"]');
    await page.waitForTimeout(300);

    // ⑧ 富文本默认渲染（发一条含白名单标签的消息，检查气泡里是真的 <b> 元素）
    await page.fill('#messageInput', '<b>加粗测试</b>');
    await page.click('#sendButton');
    await page.waitForTimeout(1200);
    const bold = await page.evaluate(() => {
      const el = document.querySelector('#messages .message-row.user .message-body b');
      return el ? el.textContent : null;
    });
    check('富文本默认渲染（<b> 成为元素）', bold === '加粗测试', String(bold));

    check('零页面错误', pageErrors.length === 0, pageErrors.join(' | '));
    await page.screenshot({ path: `${__dirname}\\quick_msg_smoke.png` });
    if (created?.id) await apiJson(`/api/conversations/${created.id}`, { method: 'DELETE' });
  } catch (error) {
    failures.push(`执行异常: ${error.message}`);
    console.log('执行异常:', error.message);
  }

  await browser.close();
  await purge('/api/starter-prompts', 'prompts');
  await purge('/api/quick-messages', 'messages');

  console.log('');
  console.log('快捷消息 UI 冒烟结果：', failures.length ? `${failures.length} 项失败 -> ${JSON.stringify(failures)}` : '全部通过');
  process.exit(failures.length ? 1 : 0);
})();
