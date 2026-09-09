// P4 显示侧冒烟（源码 server，端口 8799）：用户侧音视频播放 / GIF 缩略图 / 助手侧文件名标签。
// 前置：停掉源码 server 后先播种 —— python verify/seed_media_message.py
// 运行：$env:NODE_PATH="<node_modules>"; node verify\p4_display_smoke.cjs
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
  page.on('requestfailed', (req) => pageErrors.push(`requestfailed: ${req.url()} ${req.failure()?.errorText}`));

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messages .message-row.user', { timeout: 20000 });
    await page.click(`#conversations >> text=${conversation.title}`).catch(() => {});
    await page.waitForTimeout(1500);

    // 等待视频/音频元数据（浏览器对 preload=metadata 会发 Range 请求；readyState>=1 即元数据已就绪）
    await page.waitForFunction(() => {
      const video = document.querySelector('#messages .message-row.user video');
      const audio = document.querySelector('#messages .message-row.user audio');
      return Boolean(video && audio && video.readyState >= 1 && audio.readyState >= 1);
    }, { timeout: 15000 }).catch(() => {});

    const snapshot = await page.evaluate(() => {
      const userRow = [...document.querySelectorAll('#messages .message-row.user')]
        .find((row) => row.querySelector('video'));
      const video = userRow ? userRow.querySelector('video') : null;
      const audio = userRow ? userRow.querySelector('audio') : null;
      const gif = userRow ? userRow.querySelector('img.thumbnail') : null;
      const assistantRow = [...document.querySelectorAll('#messages .message-row.assistant')]
        .find((row) => row.querySelector('.tool-media'));
      const captions = assistantRow
        ? [...assistantRow.querySelectorAll('.tool-media figcaption')].map((el) => el.textContent.trim())
        : [];
      const gridItems = userRow ? userRow.querySelectorAll('.media-grid > *').length : 0;
      return {
        hasVideo: Boolean(video),
        videoSrc: video ? video.getAttribute('src') : '',
        videoReadyState: video ? video.readyState : -1,
        videoDuration: video ? video.duration : 0,
        videoControls: video ? video.hasAttribute('controls') : false,
        hasAudio: Boolean(audio),
        audioReadyState: audio ? audio.readyState : -1,
        audioDuration: audio ? audio.duration : 0,
        gifSrc: gif ? gif.getAttribute('src') : '',
        gifNaturalWidth: gif ? gif.naturalWidth : 0,
        gifLargeUrl: gif ? gif.getAttribute('data-large-url') : '',
        captions,
        gridItems,
      };
    });

    check('用户气泡渲染 <video>', snapshot.hasVideo, JSON.stringify(snapshot));
    check('video 带 controls', snapshot.videoControls, JSON.stringify(snapshot));
    check('video 元数据已就绪（readyState ≥ 1）', snapshot.videoReadyState >= 1, String(snapshot.videoReadyState));
    check('video 时长 > 0（Range 拉取成功）', snapshot.videoDuration > 0, String(snapshot.videoDuration));
    check('用户气泡渲染 <audio>', snapshot.hasAudio, JSON.stringify(snapshot));
    check('audio 元数据已就绪（readyState ≥ 1）', snapshot.audioReadyState >= 1, String(snapshot.audioReadyState));
    check('audio 时长 > 0', snapshot.audioDuration > 0, String(snapshot.audioDuration));
    check('GIF 缩略图按推导路径加载（naturalWidth > 0）', snapshot.gifNaturalWidth > 0, JSON.stringify(snapshot));
    check('GIF 缩略图走 _thumb.webp', snapshot.gifSrc.includes('_thumb.webp'), snapshot.gifSrc);
    check('GIF 可点开大图（data-large-url 指向主图）', snapshot.gifLargeUrl.includes('seed_anim.gif'), snapshot.gifLargeUrl);
    check('用户气泡媒体项 3 个（视频/音频/图片）', snapshot.gridItems === 3, String(snapshot.gridItems));
    check('助手侧媒体带文件名标签', snapshot.captions.includes('seed_a.png'), JSON.stringify(snapshot.captions));

    check('零 pageerror / console.error / requestfailed', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
    check('零 404 资源', notFound.length === 0, notFound.slice(0, 3).join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, String(error && error.message));
  } finally {
    await browser.close();
  }

  console.log();
  console.log(`P4 显示侧冒烟结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exit(failures.length ? 1 : 0);
})();
