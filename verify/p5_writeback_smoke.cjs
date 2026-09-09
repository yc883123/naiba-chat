// P5 写回冒烟（源码 server，端口 8799）：Job 产物写回后，前端**不刷新**即可看到。
// 前置：停掉源码 server 后先播种 —— python verify/seed_media_message.py
// 运行：$env:NODE_PATH="<node_modules>"; node verify\p5_writeback_smoke.cjs
// 说明：Node 侧用 execFileSync 调 verify/p5_writeback_apply.py 模拟"Job 跑完"
//（真实 JobMediaWriter + 真实 DB），再断言既有轮询把新 metadata 渲染出来。
const { execFileSync } = require('child_process');
const path = require('path');
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8799';
const ROOT = path.resolve(__dirname, '..');
const PYTHON = process.env.NAIBA_SMOKE_PYTHON || path.join(ROOT, '.venv', 'Scripts', 'python.exe');
const MARKER = '已提交后台生成，完成后会自动显示。';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function apiJson(pathname) {
  const response = await fetch(`${BASE}${pathname}`);
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
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });

  const p5Row = () => page.evaluate((marker) => {
    const rows = [...document.querySelectorAll('#messages .message-row.assistant')];
    const row = rows.find((item) => (item.textContent || '').includes(marker));
    if (!row) return null;
    const media = row.querySelector('.tool-media');
    const img = media ? media.querySelector('img[data-large-url]') : null;
    return {
      hasMedia: Boolean(media),
      images: media ? media.querySelectorAll('img[data-large-url]').length : 0,
      naturalWidth: img ? img.naturalWidth : 0,
    };
  }, MARKER);

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messages .message-row.assistant', { timeout: 20000 });
    await page.click(`#conversations >> text=${conversation.title}`).catch(() => {});
    await page.waitForTimeout(1500);

    const before = await p5Row();
    check('写回前：该消息尚无就地媒体', before && before.hasMedia === false, JSON.stringify(before));

    // 模拟 Job 跑完（真实 JobMediaWriter 写回 + 推进 conversations.updated_at）
    const raw = execFileSync(PYTHON, [path.join(ROOT, 'verify', 'p5_writeback_apply.py')], {
      cwd: ROOT, encoding: 'utf-8', timeout: 120000,
      env: { ...process.env, TEMP: path.join(ROOT, 'verify'), TMP: path.join(ROOT, 'verify'), PYTHONPATH: path.join(ROOT, 'verify') },
    });
    const applied = JSON.parse(raw.trim().split('\n').pop());
    check('写回执行成功', Boolean(applied.written && applied.written.added >= 1), raw.slice(0, 200));

    // 前端既有轮询（syncCurrentConversation）应在数秒内感知并重渲染——不刷新页面
    let after = null;
    for (let i = 0; i < 20; i += 1) {
      await page.waitForTimeout(1000);
      after = await p5Row();
      if (after && after.hasMedia && after.naturalWidth > 0) break;
    }
    check('不刷新页面即出现就地媒体', Boolean(after && after.hasMedia), JSON.stringify(after));
    check('媒体缩略图已加载（naturalWidth > 0）', Boolean(after && after.naturalWidth > 0), JSON.stringify(after));
    check('零 pageerror / console.error', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, String(error && error.message));
  } finally {
    await browser.close();
  }

  console.log();
  console.log(`P5 写回冒烟结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exit(failures.length ? 1 : 0);
})();
