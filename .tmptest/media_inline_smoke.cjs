// 媒体就地内嵌冒烟（源码 server，端口 8799）。
// 前置：停掉源码 server 后先播种数据 —— python .tmptest/seed_media_message.py
// 运行：$env:NODE_PATH="<node_modules 目录>"; node .tmptest\media_inline_smoke.cjs
// 说明：本沙箱拦 Edge spawn，此脚本在用户本机（已装 playwright）执行。
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8799';
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

(async () => {
  const list = await apiJson('/api/conversations');
  const conversation = (list.conversations || []).find((item) => String(item.title || '').includes('媒体内嵌'));
  check('已播种「媒体内嵌冒烟」会话', Boolean(conversation), JSON.stringify(list).slice(0, 160));
  if (!conversation) process.exit(1);

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  const notFound = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });
  page.on('response', (res) => { if (res.status() === 404) notFound.push(res.url()); });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { timeout: 20000 });
    // 分组默认折叠 + 列表虚拟化：先展开全部分组，再按标题滚动定位后点开目标会话
    // （自动打开的是"最近更新"的会话，可能是别的播种数据）。
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
    for (let i = 0; i < 80; i++) {
      const state = await page.evaluate((needle) => {
        const tree = document.querySelector('#sidebarWorkspaceTree');
        const item = [...tree.querySelectorAll('.conversation-item')]
          .find((el) => (el.querySelector('.conversation-open')?.textContent || '').includes(needle));
        if (item) { item.scrollIntoView({ block: 'center' }); return 'found'; }
        const step = Math.max(120, tree.clientHeight * 0.8);
        if (tree.scrollTop + tree.clientHeight >= tree.scrollHeight - 1) return 'end';
        tree.scrollTop = Math.min(tree.scrollTop + step, tree.scrollHeight);
        return 'scroll';
      }, conversation.title);
      if (state === 'found' || state === 'end') break;
      await page.waitForTimeout(140);
    }
    await page.click(`#sidebarWorkspaceTree .conversation-item:has-text("${conversation.title}") .conversation-open`);
    await page.waitForTimeout(1200);
    await page.waitForSelector('#messages .message-row.assistant', { timeout: 20000 });

    const snapshot = await page.evaluate(() => {
      const rows = [...document.querySelectorAll('#messages .message-row.assistant')];
      const first = rows.find((row) => row.querySelector('.tool-media')) || null;
      const legacy = rows.find((row) => !row.querySelector('.tool-media')
        && row.querySelector('.message-body > .media-grid')) || null;
      const blocks = first ? [...first.querySelectorAll('.tool-media')] : [];
      return {
        assistantRows: rows.length,
        toolRuns: first ? first.querySelectorAll('.tool-run').length : 0,
        mediaBlocks: blocks.length,
        // 媒体块必须是工具块的紧邻后继（且不在折叠的 <details> 内）
        adjacency: blocks.map((block) => {
          const prev = block.previousElementSibling;
          return {
            afterToolRun: Boolean(prev && prev.classList.contains('tool-run')),
            insideDetails: Boolean(block.closest('details.tool-run')),
            images: block.querySelectorAll('img[data-large-url]').length,
            text: String(block.textContent || '').trim(),
          };
        }),
        inlineSources: blocks.flatMap((block) => [...block.querySelectorAll('img[data-large-url]')]
          .map((img) => img.getAttribute('data-large-url'))),
        // 末尾网格：只应渲染"没有就地归属"的附件（本例新消息应为空）
        bottomGrids: first ? [...first.querySelectorAll('.message-body > .media-grid')].length : 0,
        legacyGridImages: legacy ? legacy.querySelectorAll('.message-body > .media-grid img[data-large-url]').length : 0,
        conversationImages: document.querySelectorAll('#messages img[data-large-url]').length,
      };
    });

    check('助手消息含就地媒体行与旧格式行', Boolean(snapshot.assistantRows >= 2), String(snapshot.assistantRows));
    check('工具块 2 个', snapshot.toolRuns === 2, String(snapshot.toolRuns));
    check('媒体块 2 个（每个工具一个）', snapshot.mediaBlocks === 2, String(snapshot.mediaBlocks));
    check('媒体块紧邻工具块之后', snapshot.adjacency.every((item) => item.afterToolRun), JSON.stringify(snapshot.adjacency));
    check('媒体块不在折叠的 <details> 内（否则会被藏起来）', snapshot.adjacency.every((item) => !item.insideDetails), JSON.stringify(snapshot.adjacency));
    check('就地渲染 3 张图', snapshot.inlineSources.length === 3, JSON.stringify(snapshot.inlineSources));
    check('截断提示渲染（共 25 个媒体，仅显示前 1 个）',
      snapshot.adjacency.some((item) => item.text.includes('共 25 个媒体，仅显示前 1 个')),
      JSON.stringify(snapshot.adjacency.map((i) => i.text)));
    check('新消息末尾网格为空（不重复显示）', snapshot.bottomGrids === 0, String(snapshot.bottomGrids));
    check('旧格式消息仍走末尾网格', snapshot.legacyGridImages === 1, String(snapshot.legacyGridImages));

    // 灯箱：会话内图片列表 = 3 张就地 + 1 张旧格式 = 4，顺序按 DOM
    await page.click('#messages .tool-media img[data-large-url] >> nth=0');
    await page.waitForTimeout(400);
    const box = await page.evaluate(() => {
      const el = document.querySelector('#imageLightbox');
      const counter = document.querySelector('#imageLightboxCounter');
      return { open: el ? el.hidden === false : false, counter: counter ? counter.textContent : '' };
    });
    check('点击就地缩略图打开灯箱', box.open, JSON.stringify(box));
    check(`灯箱计数 1 / ${snapshot.conversationImages}（会话内全部可放大图片）`,
      box.counter === `1 / ${snapshot.conversationImages}` && snapshot.conversationImages >= 4,
      JSON.stringify(box));
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);

    check('零 pageerror / console.error', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
    check('零 404 资源', notFound.length === 0, notFound.slice(0, 3).join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, String(error && error.message));
  } finally {
    await browser.close();
  }

  console.log();
  console.log(`媒体内嵌冒烟结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exit(failures.length ? 1 : 0);
})();
