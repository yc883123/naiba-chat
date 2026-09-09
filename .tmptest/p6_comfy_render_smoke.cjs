// P6 渲染校验：真实 ComfyUI 产物（经采集缓存后）在会话里就地显示。
// 前置：python .tmptest/p6_comfy_extract_check.py && python server.py --port 8799
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8799';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

(async () => {
  const list = await fetch(`${BASE}/api/conversations`).then((r) => r.json()).catch(() => ({}));
  const conversation = (list.conversations || []).find((item) => String(item.title || '').includes('ComfyUI 产物冒烟'));
  check('已播种「ComfyUI 产物冒烟」会话', Boolean(conversation), JSON.stringify(list).slice(0, 160));
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
    await page.waitForSelector('#messages .message-row.assistant', { timeout: 20000 });
    await page.click(`#conversations >> text=${conversation.title}`).catch(() => {});
    await page.waitForTimeout(2000);

    const state = await page.evaluate(() => {
      const media = document.querySelector('#messages .tool-media');
      const img = media ? media.querySelector('img[data-large-url]') : null;
      return {
        hasMediaBlock: Boolean(media),
        images: media ? media.querySelectorAll('img[data-large-url]').length : 0,
        naturalWidth: img ? img.naturalWidth : 0,
        caption: media && media.querySelector('figcaption') ? media.querySelector('figcaption').textContent : '',
        src: img ? img.getAttribute('src') : '',
      };
    });

    check('就地媒体块存在', state.hasMediaBlock, JSON.stringify(state));
    check('1 张图片', state.images === 1, JSON.stringify(state));
    check('图片真实加载（naturalWidth > 0）', state.naturalWidth > 0, JSON.stringify(state));
    check('文件名标签为产物名', state.caption === 'lumine_cute_00001_.png', JSON.stringify(state));
    check('走 /api/file 托管缓存', state.src.includes('/api/file?'), state.src);
    check('零 pageerror / console.error', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
    check('零 404 资源', notFound.length === 0, notFound.slice(0, 3).join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, String(error && error.message));
  } finally {
    await browser.close();
  }

  console.log();
  console.log(`P6 渲染校验结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exit(failures.length ? 1 : 0);
})();
