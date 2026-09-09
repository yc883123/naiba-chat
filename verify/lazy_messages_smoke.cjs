// 消息列表懒加载冒烟（由 verify/lazy_messages_smoke.py 编排，源码 server 8796）。
// 覆盖：默认只渲染最近 N 轮 / 向上滚动预渲染且视口不跳 / 分割线与其锚点同进同出 /
//       刻度轨仍覆盖全部轮次并可跳到未渲染的轮次 / 滚到顶后全部渲染 / 零页面错误。
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8796';
const TITLE = process.env.NAIBA_LAZY_TITLE || '懒加载冒烟';
const TURNS = Number(process.env.NAIBA_LAZY_TURNS || 30);
const MARKER_TURN = Number(process.env.NAIBA_LAZY_MARKER_TURN || 5);
const failures = [];

function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function snapshot(page) {
  return page.evaluate(() => {
    const container = document.querySelector('#messages');
    const rows = [...container.querySelectorAll('.message-row[data-message-id]')];
    const rendered = rows.filter((row) => !row.classList.contains('session-divider'));
    const texts = rendered.map((row) => (row.querySelector('.answer-content, .message-body')?.textContent || '').trim());
    const dividers = rows.filter((row) => row.classList.contains('session-divider'));
    return {
      renderedRows: rendered.length,
      firstRole: rendered[0]?.classList.contains('user') ? 'user' : (rendered[0] ? 'assistant' : ''),
      dividers: dividers.length,
      firstText: texts[0] || '',
      lastText: texts[texts.length - 1] || '',
      hasFirstTurn: Boolean(texts.find((text) => text.includes('第 1 答'))),
      hasLastTurn: Boolean(texts.find((text) => text.includes(`第 ${window.__LAZY_TURNS__} 答`))),
      scrollTop: Math.round(container.scrollTop),
      scrollHeight: Math.round(container.scrollHeight),
      ticks: document.querySelectorAll('#turnRail .turn-tick').length,
      railHidden: document.querySelector('#turnRail')?.hidden === true,
    };
  });
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });

  try {
    await page.addInitScript((cfg) => {
      window.__LAZY_TURNS__ = cfg.turns;
      window.__LAZY_MARKER_TURN__ = cfg.markerTurn;
    }, { turns: TURNS, markerTurn: MARKER_TURN });
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { timeout: 20000 });
    await page.click(`#sidebarWorkspaceTree .conversation-item:has-text("${TITLE}") .conversation-open`);
    await page.waitForSelector('#messages .message-row[data-message-id]', { timeout: 20000 });
    await page.waitForTimeout(1200);

    // ① 默认只渲染最近 10 轮（20 条消息 + 1 条分割线），且是「末尾」那一段
    const initial = await snapshot(page);
    check('默认只渲染最近 10 轮（≤ 21 行）', initial.renderedRows > 0 && initial.renderedRows <= 21,
      JSON.stringify(initial));
    check('渲染的是末尾一段（最后一条在、第一条不在）',
      initial.hasLastTurn === true && initial.hasFirstTurn === false, JSON.stringify(initial));
    check('初始停在底部', initial.scrollTop > 0, JSON.stringify(initial));
    check('刻度轨覆盖全部轮次（数据驱动，不受渲染窗口限制）',
      initial.railHidden === false && initial.ticks === TURNS, JSON.stringify(initial));

    // ② 滚到顶：预渲染上一段，且视口内容不跳
    // 先同步把 scrollTop 归零并立刻量"第一行"的视口位置（此时预渲染还没发生），
    // 预渲染后同一行必须停在原处（补进来的高度会加回 scrollTop）。
    const anchorBefore = await page.evaluate(() => {
      const container = document.querySelector('#messages');
      container.scrollTop = 0;
      const row = container.querySelector('.message-row[data-message-id]');
      return {
        text: row?.textContent.trim().slice(0, 24) || '',
        top: Math.round(row?.getBoundingClientRect().top || 0),
      };
    });
    await page.waitForTimeout(900);
    const afterExtend = await snapshot(page);
    check('滚到顶后往前预渲染（行数变多）', afterExtend.renderedRows > initial.renderedRows,
      JSON.stringify({ before: initial.renderedRows, after: afterExtend.renderedRows }));
    check('预渲染后窗口仍以「轮」为边界（首行是用户消息、行数为轮数×2）',
      afterExtend.firstRole === 'user' && afterExtend.renderedRows % 2 === 0,
      JSON.stringify(afterExtend));
    const anchorAfter = await page.evaluate((text) => {
      const row = [...document.querySelectorAll('#messages .message-row[data-message-id]')]
        .find((el) => el.textContent.trim().startsWith(text));
      return row ? Math.round(row.getBoundingClientRect().top) : null;
    }, anchorBefore.text);
    check('预渲染不跳视口（原来那条仍停在原处）',
      anchorAfter !== null && Math.abs(anchorAfter - anchorBefore.top) <= 60,
      JSON.stringify({ before: anchorBefore, after: anchorAfter }));

    // ③ 继续滚到顶直到全部渲染
    let guard = 0;
    let last = await snapshot(page);
    while (last.hasFirstTurn === false && guard < 12) {
      await page.evaluate(() => { document.querySelector('#messages').scrollTop = 0; });
      await page.waitForTimeout(600);
      last = await snapshot(page);
      guard += 1;
    }
    check('持续向上滚动后第一轮也被渲染出来', last.hasFirstTurn === true, JSON.stringify(last));
    check('全部渲染时消息条数与播种一致（60 条 + 1 条分割线）',
      last.renderedRows === TURNS * 2 && last.dividers === 1, JSON.stringify(last));

    // ④ 分割线与锚点同进同出：第 5 轮的线紧跟在第 5 答之后
    const dividerAdjacency = await page.evaluate(() => {
      const rows = [...document.querySelectorAll('#messages .message-row[data-message-id]')];
      const dividerIndex = rows.findIndex((row) => row.classList.contains('session-divider'));
      if (dividerIndex <= 0) return { ok: false, reason: '没有找到分割线' };
      const anchor = rows[dividerIndex - 1];
      return {
        ok: anchor.textContent.includes(`第 ${window.__LAZY_MARKER_TURN__} 答`),
        anchor: anchor.textContent.trim().slice(0, 24),
      };
    });
    check('分割线紧跟在被标记的那条 AI 回复之后（同进同出）',
      dividerAdjacency.ok === true, JSON.stringify(dividerAdjacency));

    // ⑤ 刻度轨点击未渲染的轮次 → 先渲染再跳转
    await page.reload({ waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { timeout: 20000 });
    await page.click(`#sidebarWorkspaceTree .conversation-item:has-text("${TITLE}") .conversation-open`);
    await page.waitForSelector('#messages .message-row[data-message-id]', { timeout: 20000 });
    await page.waitForTimeout(1200);
    const beforeJump = await snapshot(page);
    check('刷新后仍只渲染末尾一段', beforeJump.hasFirstTurn === false, JSON.stringify(beforeJump));
    await page.click('#turnRail .turn-tick[data-turn-index="0"]');
    await page.waitForTimeout(1600);
    const afterJump = await snapshot(page);
    check('点第 1 轮刻度：该轮被渲染并滚动到视口',
      afterJump.hasFirstTurn === true
      && await page.evaluate(() => {
        const row = [...document.querySelectorAll('#messages .message-row[data-message-id]')]
          .find((el) => el.textContent.includes('第 1 问'));
        if (!row) return false;
        const rect = row.getBoundingClientRect();
        return rect.top > -80 && rect.top < window.innerHeight;
      }), JSON.stringify(afterJump));

    check('零页面错误 / console.error', pageErrors.length === 0, JSON.stringify(pageErrors.slice(0, 3)));
  } catch (error) {
    check('冒烟脚本自身异常', false, String(error && error.message ? error.message : error));
  } finally {
    await browser.close();
  }

  console.log(failures.length ? `\nFAILED ${failures.length} 项：${failures.join(' | ')}` : '\nALL PASS');
  process.exit(failures.length ? 1 : 0);
})();
