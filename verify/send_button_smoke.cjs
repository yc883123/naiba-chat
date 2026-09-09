// 发送按钮状态机 + 纯附件发送 UI 冒烟（源码 server，端口 8799）。
// 运行：$env:NODE_PATH="%USERPROFILE%\node_modules"; node verify\send_button_smoke.cjs
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const BASE = 'http://127.0.0.1:8799/';
const PNG_PATH = path.join(__dirname, 'send_btn_smoke.png');

const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

(async () => {
  // 1x1 PNG
  fs.writeFileSync(
    PNG_PATH,
    Buffer.from(
      '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a49444154789c6360000002000154a24f5b0000000049454e44ae426082',
      'hex'
    )
  );

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  const notFound = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });
  page.on('response', (res) => { if (res.status() === 404) notFound.push(res.url()); });

  let chatBody = null;
  page.on('request', (req) => {
    if (req.url().endsWith('/api/chat') && req.method() === 'POST') chatBody = req.postDataJSON();
  });

  const btn = () => page.evaluate(() => {
    const b = document.querySelector('#sendButton');
    return { disabled: b.disabled, title: b.title, bg: getComputedStyle(b).backgroundColor };
  });

  try {
    await page.goto(BASE, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#sendButton', { timeout: 15000 });
    await page.waitForTimeout(1500);

    // ① 初始空输入：不可发送、灰暗
    let s = await btn();
    check('初始：发送按钮 disabled', s.disabled === true, JSON.stringify(s));
    check('初始：灰暗背景（surface-2 #F1F2F7）', s.bg === 'rgb(241, 242, 247)', s.bg);
    check('初始：title 提示输入或添加文件', s.title === '输入消息或添加文件后发送', s.title);

    // ② 有文字：可发送、accent 实心
    await page.fill('#messageInput', '你好');
    await page.waitForTimeout(150);
    s = await btn();
    check('有文字：发送按钮可用', s.disabled === false, JSON.stringify(s));
    check('有文字：accent 实心（#6D5AE6）', s.bg === 'rgb(109, 90, 230)', s.bg);
    check('有文字：title 发送', s.title === '发送', s.title);

    // ③ 清空文字：回到不可发送
    await page.fill('#messageInput', '');
    await page.waitForTimeout(150);
    s = await btn();
    check('清空文字：回到 disabled', s.disabled === true, JSON.stringify(s));
    check('清空文字：回到灰暗', s.bg === 'rgb(241, 242, 247)', s.bg);

    // ④ 仅附件（无文字）：上传完成后可发送
    const before = await page.evaluate(() => document.querySelector('#pendingFiles')?.innerHTML || '');
    console.log('上传前 #pendingFiles:', JSON.stringify(before).slice(0, 200));
    await page.setInputFiles('#fileInput', PNG_PATH);
    await page.waitForFunction(
      () => {
        const box = document.querySelector('#pendingFiles');
        return box && box.querySelector('.file-chip') && !box.innerText.includes('上传中');
      },
      null,
      { timeout: 30000 }
    );
    await page.waitForTimeout(400);
    const chipText = await page.evaluate(() => document.querySelector('#pendingFiles')?.innerText || '');
    // 服务端按内容 sha256 去重，命中已存文件时返回既有存储名（前缀 + 原名），故只断言"有 chip"。
    check('附件 chip 已渲染', chipText.includes('.png'), chipText);
    s = await btn();
    check('仅附件：发送按钮可用（核心新行为）', s.disabled === false, JSON.stringify(s));
    check('仅附件：accent 实心', s.bg === 'rgb(109, 90, 230)', s.bg);

    // ⑤ 点击发送：请求体 message 为空、attachments 有内容；用户气泡只有附件、无文字
    await page.click('#sendButton');
    await page.waitForTimeout(2500);
    check('发送请求已发出', Boolean(chatBody), JSON.stringify(chatBody));
    if (chatBody) {
      check('请求 message 为空串', chatBody.message === '', JSON.stringify(chatBody.message));
      check('请求携带 1 个附件', (chatBody.attachments || []).length === 1, JSON.stringify(chatBody.attachments));
    }
    const bubble = await page.evaluate(() => {
      const row = document.querySelector('#messages .message-row.user');
      if (!row) return null;
      const body = row.querySelector('.message-body');
      return {
        prose: (body?.querySelector('p')?.innerText || '').trim(),
        images: row.querySelectorAll('.attachment-image img, .media-grid img').length,
      };
    });
    check('用户气泡已渲染', Boolean(bubble), JSON.stringify(bubble));
    if (bubble) {
      check('气泡无正文文字', bubble.prose === '', JSON.stringify(bubble.prose));
      check('气泡含 1 张附件图', bubble.images === 1, JSON.stringify(bubble));
    }

    await page.waitForTimeout(4000);
    s = await btn();
    check('运行结束后回到不可发送态', s.disabled === true, JSON.stringify(s));
    check('零页面错误', pageErrors.length === 0, pageErrors.join(' | '));
    console.log('404 资源:', notFound.length ? JSON.stringify([...new Set(notFound)]) : '无');
    await page.screenshot({ path: path.join(__dirname, 'send_button_smoke.png'), fullPage: false });
  } catch (error) {
    failures.push(`执行异常: ${error.message}`);
    console.log('执行异常:', error.message);
  }

  await browser.close();
  console.log('');
  console.log('UI 冒烟结果：', failures.length ? `${failures.length} 项失败 -> ${JSON.stringify(failures)}` : '全部通过');
  process.exit(failures.length ? 1 : 0);
})();
