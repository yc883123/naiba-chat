// 浏览器冒烟：加载 http://127.0.0.1:8765/（可用 NAIBA_SMOKE_BASE 覆盖端口），
// 收集页面错误（pageerror/console/JS ERROR 条），截图。
// 运行：$env:NODE_PATH="%USERPROFILE%\node_modules"; node .tmptest\browser_smoke.cjs
const { chromium } = require('playwright');
const path = require('path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8765';

(async () => {
  const errors = [];
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(`console.error: ${msg.text()}`);
  });
  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForTimeout(3500);
    const banner = await page.evaluate(() => {
      const body = document.body.innerText || '';
      return {
        jsError: body.includes('JS ERROR'),
        promiseReject: body.includes('PROMISE REJECT'),
        hasMessages: !!document.querySelector('#messages'),
        hasSidebar: !!document.querySelector('#sidebar'),
        hasComposer: !!document.querySelector('#composer') || !!document.querySelector('[data-composer]'),
        title: document.title,
      };
    });
    console.log('DOM 状态:', JSON.stringify(banner, null, 2));
    if (banner.jsError) errors.push('DOM 存在 JS ERROR 警示条');
    if (banner.promiseReject) errors.push('DOM 存在 PROMISE REJECT 警示条');
    await page.screenshot({ path: path.join(__dirname, 'browser_smoke.png'), fullPage: false });
  } catch (e) {
    errors.push(`加载失败: ${e.message}`);
  }
  await browser.close();
  if (errors.length) {
    console.log('❌ 页面错误：');
    errors.forEach((e) => console.log('  ' + e));
    process.exit(1);
  }
  console.log('✅ 浏览器冒烟通过：零页面错误（pageerror/console/DOM 警示条）');
})();
