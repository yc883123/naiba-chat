// 待发送附件竖直列表冒烟（源码 server，端口 8790）。
// 覆盖：竖直堆叠 / 固定高度可滚动 / 文件名截断 / 图片预览 / 非图片占位图标 / 移除 / 空态隐藏。
// 运行：$env:NODE_PATH="<node_modules>"; node .tmptest\pending_files_smoke.cjs
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8790';
const FIXTURE_DIR = path.resolve(__dirname, 'upload_fixture');
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function apiJson(pathname, options = {}) {
  const response = await fetch(`${BASE}${pathname}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  return response.json().catch(() => ({}));
}

function makeFixtures() {
  fs.mkdirSync(FIXTURE_DIR, { recursive: true });
  const seedDir = path.resolve(__dirname, '..', 'data', 'generated', 'seed_fixture');
  const images = fs.existsSync(seedDir)
    ? fs.readdirSync(seedDir).filter((name) => name.endsWith('.png')).slice(0, 2)
    : [];
  const files = [];
  images.forEach((name, index) => {
    const target = path.join(FIXTURE_DIR, `pending_smoke_image_${index + 1}.png`);
    fs.copyFileSync(path.join(seedDir, name), target);
    files.push(target);
  });
  // 一个超长文件名（验证截断）+ 若干普通文件（验证滚动）
  files.push(path.join(FIXTURE_DIR, 'naiba_chat_1788901355_584f1d_Charge-neutral_electronic_excitations_in_quantum_insulators_一个非常非常长的中文文件名用于验证截断显示_补充一段更长的尾巴确保在宽屏下也一定溢出.txt'));
  for (let i = 1; i <= 6; i++) {
    const target = path.join(FIXTURE_DIR, `pending_smoke_note_${i}.txt`);
    fs.writeFileSync(target, `note ${i}\n`);
    files.push(target);
  }
  files.forEach((file) => {
    if (!fs.existsSync(file)) fs.writeFileSync(file, 'placeholder\n');
  });
  return files;
}

(async () => {
  const files = makeFixtures();
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(800);

    const emptyHidden = await page.evaluate(() => document.querySelector('#pendingFiles').hidden);
    check('无附件时列表隐藏', emptyHidden === true, String(emptyHidden));

    await page.setInputFiles('#fileInput', files);
    await page.waitForFunction((count) => document.querySelectorAll('#pendingFiles .pending-item').length === count, files.length, { timeout: 60000 });
    await page.waitForTimeout(1200);

    const layout = await page.evaluate(() => {
      const box = document.querySelector('#pendingFiles');
      const style = getComputedStyle(box);
      const items = [...box.querySelectorAll('.pending-item')];
      const nameStyle = items.length ? getComputedStyle(items[0].querySelector('.pending-name')) : null;
      const longName = items.map((el) => el.querySelector('.pending-name')).find((el) => el.scrollWidth > el.clientWidth + 1);
      return {
        hidden: box.hidden,
        display: style.display,
        overflowY: style.overflowY,
        maxHeight: style.maxHeight,
        clientHeight: box.clientHeight,
        scrollHeight: box.scrollHeight,
        count: items.length,
        // 竖直堆叠：每行 left 相同、top 递增
        lefts: [...new Set(items.map((el) => Math.round(el.getBoundingClientRect().left)))],
        tops: items.map((el) => Math.round(el.getBoundingClientRect().top)),
        nameEllipsis: nameStyle ? nameStyle.textOverflow : '',
        nameNowrap: nameStyle ? nameStyle.whiteSpace : '',
        hasTruncated: Boolean(longName),
        truncatedTitle: longName ? longName.getAttribute('title') : '',
        imageThumbs: items.filter((el) => el.querySelector('img.pending-thumb')).length,
        imageLoaded: [...box.querySelectorAll('img.pending-thumb')].every((img) => img.complete && img.naturalWidth > 0),
        fileIcons: items.filter((el) => el.querySelector('.pending-thumb-file')).length,
        removeButtons: box.querySelectorAll('[data-remove-file]').length,
      };
    });

    check('附件已全部进入列表', layout.count === files.length, JSON.stringify({ want: files.length, got: layout.count }));
    check('列表隐藏属性已解除', layout.hidden === false, String(layout.hidden));
    check('竖直堆叠（同一左缘、top 递增）',
      layout.lefts.length === 1 && layout.tops.every((top, i) => i === 0 || top > layout.tops[i - 1]),
      JSON.stringify({ lefts: layout.lefts, tops: layout.tops.slice(0, 5) }));
    check('固定高度 + 内部纵向滚动', layout.overflowY === 'auto' && layout.maxHeight === '180px' && layout.scrollHeight > layout.clientHeight,
      JSON.stringify({ overflowY: layout.overflowY, maxHeight: layout.maxHeight, scrollHeight: layout.scrollHeight, clientHeight: layout.clientHeight }));
    check('文件名一行截断（ellipsis + nowrap）', layout.nameEllipsis === 'ellipsis' && layout.nameNowrap === 'nowrap',
      JSON.stringify({ ellipsis: layout.nameEllipsis, nowrap: layout.nameNowrap }));
    check('超长文件名被截断且 title 保留全名', layout.hasTruncated && layout.truncatedTitle.includes('quantum_insulators'),
      JSON.stringify({ truncated: layout.hasTruncated, title: layout.truncatedTitle.slice(0, 60) }));
    check('图片附件显示预览缩略图且加载成功', layout.imageThumbs === 2 && layout.imageLoaded,
      JSON.stringify({ thumbs: layout.imageThumbs, loaded: layout.imageLoaded }));
    check('非图片附件显示占位图标', layout.fileIcons === files.length - 2, String(layout.fileIcons));
    check('每行都有移除按钮', layout.removeButtons === files.length, String(layout.removeButtons));

    // 与背景的区分度（用户实测反馈"文件框跟背景区分度太差"）
    const contrast = await page.evaluate(() => {
      const row = document.querySelector('#pendingFiles .pending-item');
      const style = getComputedStyle(row);
      const rgb = (value) => (value.match(/\d+/g) || []).slice(0, 3).map(Number);
      const rowBg = rgb(style.backgroundColor);
      const pageBg = rgb(getComputedStyle(document.body).backgroundColor);
      const diff = rowBg.reduce((sum, channel, index) => sum + Math.abs(channel - pageBg[index]), 0);
      return { rowBg, pageBg, diff, border: style.borderTopColor, shadow: style.boxShadow };
    });
    check('附件行与页面背景有明显色差（≥12）', contrast.diff >= 12, JSON.stringify(contrast));
    check('附件行有可见描边与投影', contrast.border !== 'rgba(0, 0, 0, 0)' && contrast.shadow !== 'none', JSON.stringify(contrast));

    // 点击图片预览 → 打开灯箱（沿用消息图片的交互）
    await page.click('#pendingFiles img.pending-thumb >> nth=0');
    await page.waitForTimeout(400);
    const lightbox = await page.evaluate(() => {
      const el = document.querySelector('#imageLightbox');
      return { open: el ? el.hidden === false : false };
    });
    check('点击待发送图片可看大图', lightbox.open === true, JSON.stringify(lightbox));
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);

    // 移除一行
    await page.click('#pendingFiles .pending-remove >> nth=0');
    await page.waitForTimeout(600);
    const afterRemove = await page.evaluate(() => document.querySelectorAll('#pendingFiles .pending-item').length);
    check('移除一行后列表少一条', afterRemove === files.length - 1, String(afterRemove));

    // 清空 → 列表隐藏
    for (let i = 0; i < files.length + 2; i++) {
      const button = await page.$('#pendingFiles .pending-remove');
      if (!button) break;
      await button.click();
      await page.waitForTimeout(250);
    }
    const cleared = await page.evaluate(() => ({
      hidden: document.querySelector('#pendingFiles').hidden,
      count: document.querySelectorAll('#pendingFiles .pending-item').length,
      sendDisabled: document.querySelector('#sendButton').disabled,
    }));
    check('清空后列表隐藏且发送按钮回到禁用', cleared.hidden === true && cleared.count === 0 && cleared.sendDisabled === true, JSON.stringify(cleared));

    check('零 pageerror / console.error', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, String(error && error.message));
  } finally {
    await browser.close();
    fs.rmSync(FIXTURE_DIR, { recursive: true, force: true });
  }

  console.log();
  console.log(`待发送附件列表冒烟结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exit(failures.length ? 1 : 0);
})();
