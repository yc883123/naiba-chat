// 大图灯箱左右切换冒烟（源码 server，端口 8799）。
// 运行：$env:NODE_PATH="%USERPROFILE%\node_modules"; node verify\lightbox_smoke.cjs
const zlib = require('zlib');
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

// 最小 PNG 生成（不同尺寸/颜色 → 内容哈希不同，避免上传去重）
function png(width, height, rgb) {
  const raw = Buffer.alloc((width * 3 + 1) * height);
  for (let y = 0; y < height; y += 1) {
    const rowStart = y * (width * 3 + 1);
    raw[rowStart] = 0;
    for (let x = 0; x < width; x += 1) {
      const off = rowStart + 1 + x * 3;
      raw[off] = rgb[0];
      raw[off + 1] = rgb[1];
      raw[off + 2] = rgb[2];
    }
  }
  const chunk = (type, data) => {
    const length = Buffer.alloc(4);
    length.writeUInt32BE(data.length);
    const typeBuf = Buffer.from(type, 'ascii');
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(zlib.crc32(Buffer.concat([typeBuf, data])) >>> 0);
    return Buffer.concat([length, typeBuf, data, crc]);
  };
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8;
  ihdr[9] = 2; // truecolor
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(raw)),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

async function upload(name, bytes) {
  const boundary = `----lightboxsmoke${Date.now()}${Math.random().toString(16).slice(2)}`;
  const body = Buffer.concat([
    Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${name}"\r\nContent-Type: image/png\r\n\r\n`, 'utf-8'),
    bytes,
    Buffer.from(`\r\n--${boundary}--\r\n`, 'utf-8'),
  ]);
  const response = await fetch(`${BASE}/api/uploads`, {
    method: 'POST',
    headers: { 'Content-Type': `multipart/form-data; boundary=${boundary}` },
    body,
  });
  return response.json();
}

async function waitRunDone(conversationId, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const active = await apiJson(`/api/runs?conversation_id=${encodeURIComponent(conversationId)}&active_only=1`);
    if (!(active.runs || []).length) return true;
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

async function sendTurn(conversationId, attachment, text) {
  const response = await fetch(`${BASE}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      conversation_id: conversationId,
      message: text,
      display_message: text,
      attachments: [attachment],
    }),
  });
  await response.body?.cancel?.().catch(() => {});
  return waitRunDone(conversationId);
}

function pathOf(url) {
  try {
    return decodeURIComponent(new URL(url, BASE).searchParams.get('path') || '');
  } catch (_) {
    return '';
  }
}

(async () => {
  // 准备 3 张尺寸/颜色不同的图片并上传
  const uploaded = [];
  const specs = [
    // A 用大图（缩放后大于视口，才能验证平移）；B/C 用小图
    { name: '灯箱A.png', w: 1600, h: 1200, rgb: [200, 60, 60] },
    { name: '灯箱B.png', w: 230, h: 161, rgb: [60, 160, 90] },
    { name: '灯箱C.png', w: 250, h: 171, rgb: [70, 90, 210] },
  ];
  for (const spec of specs) {
    const result = await upload(spec.name, png(spec.w, spec.h, spec.rgb));
    uploaded.push({ name: spec.name, path: result.path, thumb: result.thumb_path });
  }
  check('3 张图片已上传', uploaded.every((item) => Boolean(item.path)), JSON.stringify(uploaded.map((i) => i.path)));

  const created = await apiJson('/api/conversations', {
    method: 'POST',
    body: JSON.stringify({ title: '灯箱冒烟' }),
  });
  const conversationId = String(created.id || '');
  check('冒烟会话已创建', Boolean(conversationId), JSON.stringify(created).slice(0, 120));
  for (let i = 0; i < uploaded.length; i += 1) {
    // 与真实前端一致：附件带 thumb_path，避免派生缩略图路径 404。
    const ok = await sendTurn(
      conversationId,
      { name: uploaded[i].name, path: uploaded[i].path, thumb_path: uploaded[i].thumb },
      `第 ${i + 1} 张图`,
    );
    check(`第 ${i + 1} 轮已发送`, ok, 'run 未结束');
  }

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  const notFound = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });
  page.on('response', (res) => { if (res.status() === 404) notFound.push(res.url()); });

  const lightbox = () => page.evaluate(() => {
    const box = document.querySelector('#imageLightbox');
    const img = document.querySelector('#imageLightboxImg');
    const prev = document.querySelector('#imageLightboxPrev');
    const next = document.querySelector('#imageLightboxNext');
    const counter = document.querySelector('#imageLightboxCounter');
    return {
      open: box ? box.hidden === false : false,
      src: img ? (img.getAttribute('src') || '') : '',
      prevHidden: prev ? prev.hidden : null,
      nextHidden: next ? next.hidden : null,
      counter: counter ? counter.textContent : '',
      counterHidden: counter ? counter.hidden : null,
    };
  });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messages .attachment-image img[data-large-url]', { timeout: 20000 });
    await page.waitForTimeout(1200);

    const thumbs = await page.evaluate(() =>
      [...document.querySelectorAll('#messages img[data-large-url]')].map((el) => el.getAttribute('data-large-url')));
    check('会话内渲染 3 张图片', thumbs.length === 3, JSON.stringify(thumbs.map(pathOf)));

    // ① 点击第 2 张 → 灯箱打开、计数 2/3、左右按钮可见
    await page.click('#messages img[data-large-url] >> nth=1');
    await page.waitForTimeout(500);
    let state = await lightbox();
    check('灯箱已打开', state.open, JSON.stringify(state));
    check('计数为 2 / 3', state.counter === '2 / 3', JSON.stringify(state));
    check('左右按钮可见', state.prevHidden === false && state.nextHidden === false, JSON.stringify(state));
    check('显示第 2 张图', pathOf(state.src) === uploaded[1].path, `${pathOf(state.src)} vs ${uploaded[1].path}`);

    // ② 下一张 → 3/3；再下一张 → 回到 1/3（循环）
    await page.click('#imageLightboxNext');
    await page.waitForTimeout(300);
    state = await lightbox();
    check('点击 → 到第 3 张', state.counter === '3 / 3' && pathOf(state.src) === uploaded[2].path, JSON.stringify(state));
    await page.click('#imageLightboxNext');
    await page.waitForTimeout(300);
    state = await lightbox();
    check('继续 → 循环回第 1 张', state.counter === '1 / 3' && pathOf(state.src) === uploaded[0].path, JSON.stringify(state));

    // ③ 键盘 ← / → 切换
    await page.keyboard.press('ArrowLeft');
    await page.waitForTimeout(300);
    state = await lightbox();
    check('← 回到第 3 张', state.counter === '3 / 3' && pathOf(state.src) === uploaded[2].path, JSON.stringify(state));
    await page.keyboard.press('ArrowLeft');
    await page.waitForTimeout(300);
    state = await lightbox();
    check('← 到第 2 张', state.counter === '2 / 3', JSON.stringify(state));
    await page.keyboard.press('ArrowRight');
    await page.waitForTimeout(300);
    state = await lightbox();
    check('→ 回到第 3 张', state.counter === '3 / 3', JSON.stringify(state));

    // ④ 点击左侧按钮（上一张）
    await page.click('#imageLightboxPrev');
    await page.waitForTimeout(300);
    state = await lightbox();
    check('点击 ← 按钮到第 2 张', state.counter === '2 / 3', JSON.stringify(state));

    // ⑤ Esc 关闭
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);
    state = await lightbox();
    check('Esc 关闭灯箱', state.open === false, JSON.stringify(state));

    // ⑤b 半屏点击翻页（重新打开）
    await page.click('#messages img[data-large-url] >> nth=1');
    await page.waitForTimeout(400);
    check('重新打开在 2 / 3', (await lightbox()).counter === '2 / 3', JSON.stringify(await lightbox()));
    await page.mouse.click(120, 450);
    await page.waitForTimeout(300);
    state = await lightbox();
    check('点击左半屏 → 上一张', state.counter === '1 / 3' && pathOf(state.src) === uploaded[0].path, JSON.stringify(state));
    await page.mouse.click(1280 - 120, 450);
    await page.waitForTimeout(300);
    state = await lightbox();
    check('点击右半屏 → 下一张', state.counter === '2 / 3' && pathOf(state.src) === uploaded[1].path, JSON.stringify(state));

    // ⑤c 缩放 / 拖动（用第 1 张大图：缩放后大于视口才能验证平移）
    await page.mouse.click(120, 450);
    await page.waitForTimeout(300);
    state = await lightbox();
    check('回到第 1 张大图', state.counter === '1 / 3', JSON.stringify(state));

    await page.mouse.move(640, 450);
    await page.mouse.wheel(0, -300);
    await page.waitForTimeout(350);
    const zoomState = () => page.evaluate(() => {
      const img = document.querySelector('#imageLightboxImg');
      const box = document.querySelector('#imageLightbox');
      const match = /scale\(([\d.]+)\)/.exec(img.style.transform || '');
      return {
        scale: match ? Number(match[1]) : 1,
        transform: img.style.transform || '',
        isZoomed: box.classList.contains('is-zoomed'),
        rect: (() => { const r = img.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; })(),
      };
    });
    let zoom = await zoomState();
    check('滚轮放大', zoom.scale > 1.05, JSON.stringify(zoom));
    check('缩放态标记 is-zoomed', zoom.isZoomed === true, JSON.stringify(zoom));
    check('缩放后图片大于视口', zoom.rect[0] > 1280, JSON.stringify(zoom.rect));

    await page.mouse.click(120, 450);
    await page.waitForTimeout(300);
    state = await lightbox();
    check('缩放后单击不翻页', state.counter === '1 / 3', JSON.stringify(state));

    const beforeDrag = (await zoomState()).transform;
    await page.mouse.move(640, 450);
    await page.mouse.down();
    await page.mouse.move(760, 510, { steps: 8 });
    await page.mouse.up();
    await page.waitForTimeout(250);
    const afterDrag = (await zoomState()).transform;
    check('拖动平移改变位移', beforeDrag !== afterDrag, `${beforeDrag} -> ${afterDrag}`);

    await page.mouse.dblclick(640, 450);
    await page.waitForTimeout(300);
    zoom = await zoomState();
    check('双击复位缩放', zoom.scale === 1 && zoom.transform === '' && zoom.isZoomed === false, JSON.stringify(zoom));

    // ⑤d 换图后缩放复位
    await page.mouse.move(640, 450);
    await page.mouse.wheel(0, -300);
    await page.waitForTimeout(300);
    await page.click('#imageLightboxNext');
    await page.waitForTimeout(300);
    zoom = await zoomState();
    check('换图后缩放复位', zoom.scale === 1 && zoom.transform === '', JSON.stringify(zoom));
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);

    // ⑥ 输入区待发送附件：单张，不显示左右按钮（每次运行内容不同，避免命中去重）
    await page.setInputFiles('#fileInput', {
      name: '灯箱D.png',
      mimeType: 'image/png',
      buffer: png(180, 140, [120, 120, 100 + (Date.now() % 100)]),
    });
    await page.waitForSelector('#pendingFiles .file-chip img[data-large-url]', { timeout: 20000 });
    await page.waitForTimeout(600);
    await page.click('#pendingFiles img[data-large-url]');
    await page.waitForTimeout(400);
    state = await lightbox();
    check('输入区图片也能打开灯箱', state.open, JSON.stringify(state));
    check('单张图不显示左右按钮', state.prevHidden === true && state.nextHidden === true, JSON.stringify(state));
    check('单张图不显示计数', state.counterHidden === true, JSON.stringify(state));
    // 单张图：点击空白处关闭（点击图片本身不关闭）
    await page.mouse.click(120, 450);
    await page.waitForTimeout(300);
    check('单张图点击空白处关闭', (await lightbox()).open === false, JSON.stringify(await lightbox()));

    check('零页面错误', pageErrors.length === 0, pageErrors.join(' | '));
    console.log('  404 资源:', notFound.length ? JSON.stringify([...new Set(notFound)]) : '无');
    console.log('  上传结果:', JSON.stringify(uploaded));
    await page.screenshot({ path: `${__dirname}\\lightbox_smoke.png` });
  } catch (error) {
    failures.push(`执行异常: ${error.message}`);
    console.log('执行异常:', error.message);
  }

  await browser.close();
  if (conversationId) await apiJson(`/api/conversations/${conversationId}`, { method: 'DELETE' });

  console.log('');
  console.log('灯箱切换冒烟结果：', failures.length ? `${failures.length} 项失败 -> ${JSON.stringify(failures)}` : '全部通过');
  process.exit(failures.length ? 1 : 0);
})();
