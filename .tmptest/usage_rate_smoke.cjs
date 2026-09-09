// 用量速率显示冒烟（源码 server，端口 8799）。
// 前置：先执行 `python .tmptest/seed_usage_message.py`（服务停止时播种带 requests_detail 的消息）。
// 运行：$env:NODE_PATH="%USERPROFILE%\node_modules"; node .tmptest\usage_rate_smoke.cjs
const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:8799';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messages .message-row.assistant', { timeout: 20000 });
    await page.waitForTimeout(1500);

    const summary = await page.evaluate(() => {
      const el = document.querySelector('#messages .usage-duration');
      return el ? el.textContent : null;
    });
    check('总结行存在', Boolean(summary), String(summary));
    // 播种数据：输出 232+368=600 tokens / 请求耗时 3.2+2.0=5.2s → 115.38 → 平均115 token/s
    check('总结行含平均速率', Boolean(summary && summary.includes('平均 115 token/s')), String(summary));
    check('总结行格式', Boolean(summary && /^本轮总耗时 5\.2s，平均 115 token\/s，共 2 次请求。\d{4}年\d{1,2}月\d{1,2}日 \d{2}:\d{2}:\d{2}$/.test(summary)), String(summary));

    // 展开请求明细
    await page.click('[data-usage-toggle]');
    await page.waitForTimeout(400);
    const lines = await page.evaluate(() => {
      const box = document.querySelector('.usage-requests');
      if (!box || box.hidden) return null;
      return [...box.querySelectorAll('.usage-request-line')].map((el) => ({
        text: el.textContent,
        speed: el.querySelector('.n-speed') ? el.querySelector('.n-speed').textContent : null,
      }));
    });
    check('请求明细已展开', Array.isArray(lines) && lines.length === 2, JSON.stringify(lines));

    if (Array.isArray(lines) && lines.length === 2) {
      // 第 1 次请求：232 输出 / 3.2s = 72.5 → 73（%4d 右对齐 → "  73"）
      check('第 1 次请求含速率段', lines[0].text.includes('速率') && lines[0].text.includes('73 token/s'), lines[0].text);
      check('第 1 次请求 %4d 速率', lines[0].speed === '  73', JSON.stringify(lines[0].speed));
      check('第 1 次请求速率在耗时前', lines[0].text.indexOf('73 token/s') < lines[0].text.indexOf('耗时'), lines[0].text);
      check('第 1 次请求耗时 3.2s', lines[0].text.includes('耗时 3.2s'), lines[0].text);
      check('第 1 次请求输入/输出/总', lines[0].text.includes('输入 2367') && lines[0].text.includes('输出 232') && lines[0].text.includes('总 2599'), lines[0].text);
      check('第 1 次请求命中率', lines[0].text.includes('命中率 0.0%') && lines[0].text.includes('（命中 0 / 重算 2367）'), lines[0].text);
      // 第 2 次请求：368 输出 / 2.0s = 184 → " 184"
      check('第 2 次请求 %4d 速率', lines[1].speed === ' 184', JSON.stringify(lines[1].speed));
      check('第 2 次请求耗时 2.0s', lines[1].text.includes('耗时 2.0s'), lines[1].text);
    }

    check('零页面错误', pageErrors.length === 0, pageErrors.join(' | '));
    await page.screenshot({ path: `${__dirname}\\usage_rate_smoke.png` });
  } catch (error) {
    failures.push(`执行异常: ${error.message}`);
    console.log('执行异常:', error.message);
  }

  await browser.close();
  console.log('');
  console.log('用量速率冒烟结果：', failures.length ? `${failures.length} 项失败 -> ${JSON.stringify(failures)}` : '全部通过');
  process.exit(failures.length ? 1 : 0);
})();
