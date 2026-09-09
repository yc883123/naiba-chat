// B 级针对性检查：对话刻度轨（40 轮会话）。
// 覆盖：刻度数 = 30（滑动窗口）/ 视口中心所在轮次高亮且唯一 / 滚到顶/底时高亮跟随 /
//       悬停出现概要弹窗（用户一行 + AI 两行、超长省略）/ 点击刻度跳转到对应轮次 /
//       消息列因轨道左移（右padding > 左padding）/ 窄屏隐藏轨道 / 零页面错误。
// 前置：python verify/seed_turn_rail_chat.py
// 运行：$env:NODE_PATH="<node_modules>"; node verify\_check_turn_rail.cjs
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8790';
const TITLE = '刻度轨冒烟会话';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function ensureVisible(page, title) {
  await page.evaluate(() => { const tree = document.querySelector('#sidebarWorkspaceTree'); if (tree) tree.scrollTop = 0; });
  await page.waitForTimeout(250);
  for (let i = 0; i < 60; i++) {
    const state = await page.evaluate((needle) => {
      const tree = document.querySelector('#sidebarWorkspaceTree');
      if (!tree) return 'missing';
      const item = [...tree.querySelectorAll('.conversation-item')]
        .find((el) => (el.querySelector('.conversation-open')?.textContent || '').includes(needle));
      if (item) { item.scrollIntoView({ block: 'center' }); return 'found'; }
      const step = Math.max(120, tree.clientHeight * 0.8);
      if (tree.scrollTop + tree.clientHeight >= tree.scrollHeight - 1) return 'end';
      tree.scrollTop = Math.min(tree.scrollTop + step, tree.scrollHeight);
      return 'scroll';
    }, title);
    if (state === 'found') { await page.waitForTimeout(200); return true; }
    if (state === 'end' || state === 'missing') return false;
    await page.waitForTimeout(140);
  }
  return false;
}

// 按规则（视口中心落在哪一轮）在页面里独立算一遍期望值，避免把期望写死。
// 程序化滚动：临时关掉 .messages 的 scroll-behavior: smooth，避免"滚动还没结束"干扰断言。
async function instantScroll(page, scrollTop) {
  await page.evaluate((top) => {
    const container = document.querySelector('#messages');
    const previous = container.style.scrollBehavior;
    container.style.scrollBehavior = 'auto';
    if (top === null) container.scrollTop = 0;
    else container.scrollTop = top;
    container.style.scrollBehavior = previous;
  }, scrollTop);
  await page.waitForTimeout(400);
}

async function expectedActive(page) {
  // 懒加载后 DOM 里只有渲染窗口内的轮次，返回「视口中心所在轮次」的用户消息 id，
  // 与刻度上的 data-turn-message-id 比对（而不是相对下标）。
  return page.evaluate(() => {
    const container = document.querySelector('#messages');
    const rows = [...container.querySelectorAll('.message-row.user[data-message-id]')];
    const base = container.getBoundingClientRect().top - container.scrollTop;
    const center = container.scrollTop + container.clientHeight / 2;
    let visible = '';
    rows.forEach((row) => {
      if (row.getBoundingClientRect().top - base <= center) visible = row.dataset.messageId;
    });
    return visible;
  });
}

// 滚到"最顶上"：懒加载下每次 scrollTop=0 只会往前预渲染一段并保持视口不动，
// 需要反复滚到底（直到窗口起点为 0、scrollTop 真为 0）。
async function scrollToVeryTop(page) {
  for (let round = 0; round < 12; round += 1) {
    await instantScroll(page, 0);
    const info = await page.evaluate(() => {
      const container = document.querySelector('#messages');
      return { scrollTop: Math.round(container.scrollTop) };
    });
    if (info.scrollTop === 0) return true;
  }
  return false;
}

async function railSnapshot(page) {
  return page.evaluate(() => {
    const rail = document.querySelector('#turnRail');
    if (!rail) return { exists: false };
    const ticks = [...rail.querySelectorAll('.turn-tick')];
    const active = ticks.filter((t) => t.classList.contains('active'));
    const idleTick = ticks.find((t) => !t.classList.contains('active')) || ticks[0];
    const lineWidth = (tick) => (tick ? Math.round(tick.querySelector('.turn-tick-line').getBoundingClientRect().width) : 0);
    const rect = rail.getBoundingClientRect();
    const container = document.querySelector('#messages');
    const style = getComputedStyle(container);
    return {
      exists: true,
      hidden: rail.hidden,
      tickCount: ticks.length,
      firstIndex: ticks.length ? Number(ticks[0].dataset.turnIndex) : -1,
      lastIndex: ticks.length ? Number(ticks[ticks.length - 1].dataset.turnIndex) : -1,
      activeCount: active.length,
      activeIndex: active.length ? Number(active[0].dataset.turnIndex) : -1,
      activeMessageId: active.length ? String(active[0].dataset.turnMessageId || '') : '',
      activeWidth: lineWidth(active[0]),
      idleWidth: lineWidth(idleTick),
      blockSizes: [...new Set(ticks.map((t) => {
        const r = t.getBoundingClientRect();
        return `${Math.round(r.width)}x${Math.round(r.height)}`;
      }))],
      blockTop: ticks.length ? Math.round(ticks[0].getBoundingClientRect().top) : 0,
      railRight: Math.round(window.innerWidth - rect.right),
      paddingLeft: parseFloat(style.paddingLeft),
      paddingRight: parseFloat(style.paddingRight),
      scrollTop: Math.round(container.scrollTop),
      scrollHeight: Math.round(container.scrollHeight),
      clientHeight: Math.round(container.clientHeight),
    };
  });
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') errors.push(`console.error: ${msg.text()}`); });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(900);
    check('侧栏里能找到播种会话', await ensureVisible(page, TITLE), TITLE);
    await page.click(`#sidebarWorkspaceTree .conversation-open:has-text("${TITLE}")`);
    await page.waitForSelector('#messages .message-row.assistant', { timeout: 20000 });
    await page.waitForTimeout(1200);

    let rail = await railSnapshot(page);
    check('刻度轨已渲染', rail.exists === true && rail.hidden === false, JSON.stringify(rail));
    check('只显示视口附近的 30 条（40 轮 → 30 条滑动窗口）',
      rail.tickCount === 30, JSON.stringify({ tickCount: rail.tickCount, first: rail.firstIndex, last: rail.lastIndex }));
    check('同一时刻只有一条高亮', rail.activeCount === 1, JSON.stringify(rail));
    const topExpectId = await expectedActive(page);
    check('打开会话（已到底）→ 高亮 = 视口中心所在轮次',
      rail.activeMessageId === topExpectId && Boolean(topExpectId),
      JSON.stringify({ activeIndex: rail.activeIndex, activeMessageId: rail.activeMessageId, expect: topExpectId }));
    check('高亮刻度比普通刻度更宽（加粗凸起）',
      rail.activeWidth > rail.idleWidth, JSON.stringify({ active: rail.activeWidth, idle: rail.idleWidth }));
    check('轨道贴在会话区右缘（距窗口右缘 < 40px）', rail.railRight < 40, String(rail.railRight));
    check('消息列左移让位（右内边距 > 左内边距）',
      rail.paddingRight > rail.paddingLeft + 10,
      JSON.stringify({ left: rail.paddingLeft, right: rail.paddingRight }));

    // 滚到顶：高亮仍按"视口中心所在轮次"（顶部时中心落在中间那几轮之一），窗口前移到 0..29
    check('滚到最顶上（懒加载会逐段预渲染）', await scrollToVeryTop(page) === true);
    rail = await railSnapshot(page);
    const topExpect = await expectedActive(page);
    check('滚到顶 → 高亮 = 视口中心所在轮次',
      rail.activeMessageId === topExpect, JSON.stringify({ activeIndex: rail.activeIndex, activeMessageId: rail.activeMessageId, expect: topExpect }));
    check('滚到顶 → 窗口前移到 0..29',
      rail.firstIndex === 0 && rail.lastIndex === 29, JSON.stringify({ first: rail.firstIndex, last: rail.lastIndex }));

    // 滚到中间某处：高亮跟随视口中心
    const midTop = await page.evaluate(() => {
      const container = document.querySelector('#messages');
      const rows = [...container.querySelectorAll('.message-row.user[data-message-id]')];
      const target = rows[19];               // 第 20 轮
      const base = container.getBoundingClientRect().top - container.scrollTop;
      const rect = target.getBoundingClientRect();
      // 把该轮的用户消息滚到视口垂直中心 → 按规则高亮应落在它身上
      return rect.top - base - Math.max(0, (container.clientHeight - rect.height) / 2);
    });
    await instantScroll(page, midTop);
    rail = await railSnapshot(page);
    check('滚动到第 20 轮 → 高亮落在该轮',
      rail.activeIndex === 19, JSON.stringify({ activeIndex: rail.activeIndex, expect: 19 }));
    check('窗口随滚动滑动（不再从 0 开始）',
      rail.firstIndex > 0 && rail.tickCount === 30, JSON.stringify({ first: rail.firstIndex, count: rail.tickCount }));

    // 悬停：概要弹窗（第 N 轮 + 用户消息一行 + AI 回复两行，靠字号字重区分、无「用户/AI」标签）
    const tick = await page.evaluate(() => {
      const ticks = [...document.querySelectorAll('#turnRail .turn-tick')];
      const target = ticks.find((t) => t.classList.contains('active')) || ticks[0];
      const rect = target.getBoundingClientRect();
      return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, index: Number(target.dataset.turnIndex) };
    });
    const sizesBefore = (await railSnapshot(page)).blockSizes;
    await page.mouse.move(tick.x, tick.y);
    await page.waitForTimeout(500);
    const sizesAfter = (await railSnapshot(page)).blockSizes;
    check('悬停后所有刻度的固定块尺寸不变（位置不抖）',
      JSON.stringify(sizesBefore) === JSON.stringify(sizesAfter),
      JSON.stringify({ before: sizesBefore, after: sizesAfter }));

    const tip = await page.evaluate((tickPos) => {
      const el = document.querySelector('#turnTip');
      if (!el) return { exists: false };
      const user = el.querySelector('.turn-tip-user');
      const reply = el.querySelector('.turn-tip-reply');
      const indexLine = el.querySelector('.turn-tip-index');
      const rect = el.getBoundingClientRect();
      const userStyle = getComputedStyle(user);
      const replyStyle = getComputedStyle(reply);
      return {
        exists: true,
        hidden: el.hidden,
        text: (el.textContent || '').replace(/\s+/g, ' ').trim(),
        indexText: indexLine ? indexLine.textContent.trim() : '',
        firstClass: el.firstElementChild ? el.firstElementChild.className : '',
        userClamp: userStyle.webkitLineClamp,
        replyClamp: replyStyle.webkitLineClamp,
        userSize: parseFloat(userStyle.fontSize),
        replySize: parseFloat(replyStyle.fontSize),
        userWeight: Number(userStyle.fontWeight),
        replyWeight: Number(replyStyle.fontWeight),
        replyColor: replyStyle.color,
        hasBoldLabels: Boolean(el.querySelector('b')),
        leftOfTick: rect.right < tickPos.x,
        inViewport: rect.left >= 0 && rect.top >= 0 && rect.right <= window.innerWidth,
      };
    }, tick);
    check('悬停刻度弹出概要', tip.exists === true && tip.hidden === false, JSON.stringify(tip));
    check('概要保留「第 N 轮」且不再有「用户 / AI」标签',
      tip.indexText === `第 ${tick.index + 1} 轮` && tip.hasBoldLabels === false
      && tip.firstClass === 'turn-tip-index',
      JSON.stringify({ indexText: tip.indexText, firstClass: tip.firstClass, hasBoldLabels: tip.hasBoldLabels }));
    check('用户消息大而粗、AI 回复小而浅（靠字体区分）',
      tip.userSize > tip.replySize && tip.userWeight >= 600 && tip.replyWeight <= 400
      && tip.userClamp === '1' && tip.replyClamp === '2',
      JSON.stringify(tip));
    check('弹窗出现在刻度左侧且不出视口',
      tip.leftOfTick === true && tip.inViewport === true, JSON.stringify(tip));

    // 判定区 = 整个固定块：把鼠标放到块的上边缘（远离那根 3px 线）也应命中
    const blockEdge = await page.evaluate((index) => {
      const tick = [...document.querySelectorAll('#turnRail .turn-tick')]
        .find((t) => Number(t.dataset.turnIndex) === index);
      const rect = tick.getBoundingClientRect();
      return { x: rect.left + rect.width / 2, y: rect.top + 1, height: Math.round(rect.height), width: Math.round(rect.width) };
    }, tick.index);
    await page.mouse.move(blockEdge.x, 0);
    await page.waitForTimeout(200);
    await page.mouse.move(blockEdge.x, blockEdge.y);
    await page.waitForTimeout(500);
    const edgeTip = await page.evaluate(() => {
      const el = document.querySelector('#turnTip');
      return { hidden: el ? el.hidden : true };
    });
    check('判定区是整个固定块（点块边缘也能命中）',
      blockEdge.height >= 8 && edgeTip.hidden === false,
      JSON.stringify({ ...blockEdge, hidden: edgeTip.hidden }));

    await page.screenshot({ path: 'verify/turn_rail.png' });

    // 点击刻度跳转
    const clickTarget = await page.evaluate(() => {
      const ticks = [...document.querySelectorAll('#turnRail .turn-tick')];
      const target = ticks[0];
      const rect = target.getBoundingClientRect();
      return {
        index: Number(target.dataset.turnIndex),
        messageId: String(target.dataset.turnMessageId || ''),
        x: rect.left + rect.width / 2,
        y: rect.top + rect.height / 2,
      };
    });
    await page.mouse.click(clickTarget.x, clickTarget.y);
    await page.waitForTimeout(2000);
    rail = await railSnapshot(page);
    check('点击刻度跳转到该轮（高亮切过去）',
      rail.activeIndex === clickTarget.index && rail.activeMessageId === clickTarget.messageId,
      JSON.stringify({ clicked: clickTarget.index, active: rail.activeIndex, clickedId: clickTarget.messageId, activeId: rail.activeMessageId }));

    // 窄屏：隐藏轨道、消息列恢复居中
    await page.setViewportSize({ width: 640, height: 800 });
    await page.waitForTimeout(500);
    const narrow = await page.evaluate(() => {
      const rail = document.querySelector('#turnRail');
      const style = getComputedStyle(document.querySelector('#messages'));
      return { railDisplay: getComputedStyle(rail).display, padL: parseFloat(style.paddingLeft), padR: parseFloat(style.paddingRight) };
    });
    check('窄屏（640px）隐藏轨道且消息列恢复居中',
      narrow.railDisplay === 'none' && Math.abs(narrow.padL - narrow.padR) < 1,
      JSON.stringify(narrow));
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.waitForTimeout(400);

    check('零 pageerror / console.error', errors.length === 0, errors.slice(0, 3).join(' | '));
  } catch (error) {
    check('检查脚本未抛异常', false, String(error && error.stack ? error.stack.split('\n').slice(0, 2).join(' | ') : error));
  } finally {
    await browser.close();
  }

  console.log();
  console.log(`对话刻度轨检查：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exitCode = failures.length ? 1 : 0;
})();
