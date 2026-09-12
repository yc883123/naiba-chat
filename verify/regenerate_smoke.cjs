// 「重新生成」+「编辑」回归冒烟（由 verify/regenerate_smoke.py 起独立源码 server 后调用）。
//
// 覆盖（全部走真实前端 + 真实后端截断，只用假流挡住模型调用）：
//   ① 按钮就位与顺序：用户消息 编辑 → 分支；AI 回复 复制 → 重新生成 → 新会话；
//   ② 中间轮点「重新生成」必须弹确认（含剩余条数 + 草稿提示），取消则一切不变；
//   ③ 最后一轮点「重新生成」不弹确认，**截断点是那条提问**，重发的是同一条提问原文，
//      且前缀里没有原答复、没有重复提问；
//   ④ 「编辑」一条**富消息**（/ref 技能引用 + @ 工作区引用 + 图片附件）：
//      编辑框逐字回填、原附件可见 → 改字 → 点**底部发送按钮**确认 → 三条引用链都得活着：
//        · display_message = 用户原样（/ref 与 @ 逐字保留）
//        · message 已剥离 /ref，但 @ 仍在（后端才解析成绝对路径）
//        · attachments 带回图片的 path 与 thumb_path（不是只有 path）
//      并且前缀（更早的消息）逐字保留、后端只掉被截断的那一段；
//   ⑤ 编辑最前面那条提问 → 后端清空（截断到头的边界）；
//   ⑥ 零页面错误。
//
// 假 /api/chat：只回一段最小 NDJSON，让 UI 落定；真实的前缀缓存在后端，
// 由「后端剩余消息 == 预期前缀」来断言（build_model_history 对存储消息 1:1 映射）。
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8796';
const BOUND_MODEL = process.env.NAIBA_SMOKE_BOUND_MODEL || 'smoke-pro';
const Q1 = process.env.NAIBA_SMOKE_Q1 || '重新生成冒烟：第一问';
const A1 = process.env.NAIBA_SMOKE_A1 || '第一答：冒烟历史。';
const A2 = process.env.NAIBA_SMOKE_A2 || '第二答：继续的回复。';
const A3 = process.env.NAIBA_SMOKE_A3 || '第三答：收尾的回复。';
const Q2_PLAIN = process.env.NAIBA_SMOKE_Q2_PLAIN || '第二问：继续';
const Q2_DISPLAY = process.env.NAIBA_SMOKE_Q2_DISPLAY || Q2_PLAIN;
const Q2_CONTENT = process.env.NAIBA_SMOKE_Q2_CONTENT || Q2_PLAIN;
const Q3_PLAIN = process.env.NAIBA_SMOKE_Q3_PLAIN || '第三问：收尾';
const Q3_DISPLAY = process.env.NAIBA_SMOKE_Q3_DISPLAY || Q3_PLAIN;
const AT_REF = process.env.NAIBA_SMOKE_AT_REF || '';
const IMG_PATH = process.env.NAIBA_SMOKE_IMG_PATH || '';
const IMG_THUMB = process.env.NAIBA_SMOKE_IMG_THUMB || '';
// 第二问里那个 /ref 引用（display 去掉 @ 引用与正文后的那段）。
const SKILL_REF = Q2_DISPLAY.replace(Q2_PLAIN, '').replace(`@${AT_REF}`, '').trim();
const EDITED = '第一问：改过之后的问题';
const EDITED_RICH = '第二问：改过之后的问题（带引用重发）';

const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function apiJson(path, options = {}) {
  const init = { headers: { 'Content-Type': 'application/json' }, ...options };
  if (init.body && typeof init.body !== 'string') init.body = JSON.stringify(init.body);
  const response = await fetch(`${BASE}${path}`, init);
  return response.json().catch(() => ({}));
}

async function messagesOf(conversationId) {
  const data = await apiJson(`/api/conversations/${encodeURIComponent(conversationId)}`);
  return data.messages || [];
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`);
  });
  // 确认框统一「取消」：只有明确要执行的步骤才需要它不出现/被接受。
  const dialogs = [];
  page.on('dialog', async (dialog) => {
    dialogs.push(dialog.message());
    await dialog.dismiss().catch(() => {});
  });

  // 目录请求拦截：让「会话底部选择到已检测模型」这一发送前置条件成立（不真连供应商）。
  await page.route('**/api/providers/models', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ models: [{ id: BOUND_MODEL, name: BOUND_MODEL }, { id: 'smoke-flash', name: 'smoke-flash' }] }),
  }));

  // /api/chat 拦截：记录请求体，回一段最小 NDJSON 让 UI 落定（不真调模型）。
  const chatPayloads = [];
  await page.route('**/api/chat', async (route) => {
    let body = {};
    try { body = route.request().postDataJSON() || {}; } catch (_) { body = {}; }
    chatPayloads.push(body);
    const runId = `smoke-run-${chatPayloads.length}`;
    const stream = [
      { type: 'run_started', run_id: runId, sequence: 1 },
      { type: 'delta', run_id: runId, content: '冒烟答复', sequence: 2 },
      {
        type: 'done',
        run_id: runId,
        sequence: 3,
        message: {
          id: `smoke-assistant-${chatPayloads.length}`,
          role: 'assistant',
          content: '冒烟答复',
          created_at: Date.now(),
          metadata: {},
        },
      },
    ].map((event) => JSON.stringify(event)).join('\n') + '\n';
    await route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: stream });
  });

  async function waitFor(fn, timeout = 15000, step = 120) {
    const end = Date.now() + timeout;
    let last = null;
    while (Date.now() < end) {
      last = await fn();
      if (last) return last;
      await page.waitForTimeout(step);
    }
    return last;
  }

  // 页面重载 + 等消息渲染：一次「发送」之后 state.messages 里会留下乐观行（乐观 user 行没有 id、
  // 假流回的 assistant 行 id 是 smoke-assistant-N），它们会污染后续「剩余条数」的推导，
  // 所以每个阶段之间重载一次，让 DOM/state 全部回到后端真值。
  async function reload() {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 25000 });
    await page.waitForSelector('#messages .message-row[data-message-id]', { timeout: 25000 });
    await page.waitForTimeout(600);
  }

  const actionSnapshot = () => page.evaluate(() => {
    const rows = [...document.querySelectorAll('#messages .message-row[data-message-id]')];
    return rows.map((row) => {
      const regen = row.querySelector('[data-regenerate-message]');
      return {
        role: row.classList.contains('user') ? 'user' : 'assistant',
        buttons: [...row.querySelectorAll('.message-actions button')].map((b) => b.textContent.trim()),
        regenShown: regen ? getComputedStyle(regen).display !== 'none' : null,
      };
    });
  });

  const bridge = () => page.evaluate(() => {
    const btn = document.querySelector('#sendButton');
    const input = document.querySelector('#messageInput');
    return {
      editingClass: document.body.classList.contains('is-editing-message'),
      inputDisabled: Boolean(input && input.disabled),
      placeholder: input ? input.placeholder : '',
      attachDisabled: Boolean(document.querySelector('#attachButton')?.disabled),
      sendTitle: btn ? btn.title : '',
      sendDisabled: btn ? btn.disabled : null,
    };
  });

  try {
    await reload();

    // 会话 ID 从接口拿：只有一个会话，直接取列表第一项。
    const list = await apiJson('/api/conversations');
    const convId = String(((list.conversations || [])[0] || {}).id || '');
    check('拿到冒烟会话 ID', Boolean(convId), convId);

    // ---- ① 按钮就位与顺序 ----
    const snap = await actionSnapshot();
    check('初始 6 条消息（三轮问答）', snap.length === 6, JSON.stringify(snap.map((s) => s.role)));
    const userRows = snap.filter((s) => s.role === 'user');
    const aiRows = snap.filter((s) => s.role === 'assistant');
    check('用户消息操作区 = 编辑 → 分支',
      userRows.length === 3 && userRows.every((s) => s.buttons.join(',') === '编辑,分支'),
      JSON.stringify(userRows.map((s) => s.buttons)));
    check('AI 回复操作区 = 复制 → 重新生成 → 新会话',
      aiRows.length === 3 && aiRows.every((s) => s.buttons.join(',') === '复制,重新生成,新会话'),
      JSON.stringify(aiRows.map((s) => s.buttons)));
    check('「重新生成」按钮在空闲态可见', aiRows.every((s) => s.regenShown === true),
      JSON.stringify(aiRows.map((s) => s.regenShown)));
    check('富消息的图片附件渲染出来了（图片引用没在路上丢）',
      (await page.locator(`#messages .message-row:has-text("${Q2_PLAIN}") figure.attachment-image`).count()) === 1);

    // 留一张真实截图，便于人工肉眼确认按钮就位（失败时也照样留，用于排查）。
    await page.screenshot({
      path: process.env.NAIBA_SMOKE_SHOT
        || require('path').join(__dirname, 'regenerate_smoke_actions.png'),
      fullPage: false,
    });

    // ---- ② 中间轮：弹确认（含剩余条数 + 草稿提示），取消后一切不变 ----
    dialogs.length = 0;
    await page.fill('#messageInput', '这是一段草稿');
    await page.click(`#messages .message-row:has-text("${A2}") [data-regenerate-message]`);
    await waitFor(() => (dialogs.length ? true : null), 6000);
    check('中间轮「重新生成」弹确认框', dialogs.length === 1, JSON.stringify(dialogs));
    check('确认文案标出会被删除的条数', Boolean(dialogs[0] && dialogs[0].includes('还有 2 条消息')), dialogs[0]);
    check('确认文案标出草稿会被替换', Boolean(dialogs[0] && dialogs[0].includes('输入框里的草稿会被替换成这条提问')), dialogs[0]);
    check('取消后不发任何请求', chatPayloads.length === 0, String(chatPayloads.length));
    check('取消后消息数不变（6 条）', (await messagesOf(convId)).length === 6);
    await page.fill('#messageInput', '');

    // ---- ③ 最后一轮：不弹确认；截断点是那条提问 ----
    dialogs.length = 0;
    await page.click(`#messages .message-row:has-text("${A3}") [data-regenerate-message]`);
    const p1 = await waitFor(() => chatPayloads[0] || null);
    check('最后一轮「重新生成」不弹确认框', dialogs.length === 0, JSON.stringify(dialogs));
    check('发起了重发（一次 /api/chat）', Boolean(p1));
    check('display_message 是用户原样（/ref 引用逐字保留）',
      Boolean(p1) && p1.display_message === Q3_DISPLAY, String(p1 && p1.display_message));
    check('发给模型的正文已剥离 /ref（引用不当正文塞给模型）',
      Boolean(p1) && !String(p1.message).includes(SKILL_REF) && String(p1.message).trim() === Q3_PLAIN,
      JSON.stringify({ message: p1 && p1.message, ref: SKILL_REF }));
    check('用的是当前顶栏选中的模型', p1 && p1.model_name === BOUND_MODEL, String(p1 && p1.model_name));
    check('重发针对当前会话', p1 && p1.conversation_id === convId, String(p1 && p1.conversation_id));
    const after3 = await messagesOf(convId);
    check('截断点 = 那条提问：后端只剩前两轮（4 条）',
      after3.length === 4, JSON.stringify(after3.map((m) => `${m.role}:${String(m.content).slice(0, 10)}`)));
    check('U1,A1 逐字节保留（前缀缓存命中的前提）',
      after3[0] && after3[0].content === Q1 && after3[1] && after3[1].content === A1,
      JSON.stringify(after3.map((m) => String(m.content))));
    check('原答复已从历史移除（不会重复提问/答复）',
      !JSON.stringify(after3).includes(A3), JSON.stringify(after3.map((m) => String(m.content))));

    await reload();

    // ---- ④ 「编辑」富消息：完整 composer 移入气泡，附件可删/取消可恢复 ----
    dialogs.length = 0;
    const before = chatPayloads.length;
    await page.click(`#messages .message-row:has-text("${Q2_PLAIN}") [data-edit-message]`);
    await page.waitForSelector(`#messages .message-row:has-text("${Q2_PLAIN}") .composer-wrap`, { timeout: 10000 });
    await page.waitForTimeout(300);
    const prefilled = await page.inputValue('#messages .message-row:has-text("第二问") #messageInput');
    check('「编辑」逐字回填用户原文（含 /ref 与 @ 工作区引用）', prefilled === Q2_DISPLAY, prefilled);
    check('编辑框里带出了原图片附件（不是只留文字）',
      (await page.locator(`#messages .message-row:has-text("${Q2_PLAIN}") #pendingFiles .pending-item`).count()) === 1);

    const placement = await page.evaluate(() => {
      const row = [...document.querySelectorAll('#messages .message-row')]
        .find((el) => (el.textContent || '').includes('第二问'));
      const inline = row?.querySelector('.composer-wrap');
      const bottom = [...document.querySelectorAll('.composer-wrap')].find((el) => !row?.contains(el));
      return {
        inline: Boolean(inline),
        inputInRow: Boolean(inline?.querySelector('#messageInput')),
        bottomHidden: Boolean(bottom && (bottom.hidden || getComputedStyle(bottom).display === 'none')),
        uniqueInput: document.querySelectorAll('#messageInput').length === 1,
      };
    });
    check('完整 composer-wrap 已移动到选择编辑的消息气泡',
      placement.inline && placement.inputInRow && placement.uniqueInput, JSON.stringify(placement));
    check('底部 composer 隐藏并保留占位', placement.bottomHidden, JSON.stringify(placement));

    const b1 = await bridge();
    check('编辑态已标记（body.is-editing-message）', b1.editingClass === true, JSON.stringify(b1));
    check('移动后的输入框保持可编辑', b1.inputDisabled === false, JSON.stringify(b1));
    check('移动后的添加文件按钮保持可用', b1.attachDisabled === false, JSON.stringify(b1));
    check('发送按钮变成「重新发送」且可用',
      b1.sendDisabled === false && b1.sendTitle.includes('重新发送'), JSON.stringify(b1));

    // 留一张编辑态截图：底部输入区让位 + 编辑框里的附件 + 发送键变「重新发送」。
    await page.screenshot({
      path: process.env.NAIBA_SMOKE_SHOT_EDIT
        || require('path').join(__dirname, 'regenerate_smoke_editing.png'),
      fullPage: false,
    });

    // 原附件位于移动后的 pendingFiles，点移除应立即消失；取消编辑后原附件恢复。
    await page.click(`#messages .message-row:has-text("${Q2_PLAIN}") #pendingFiles [data-remove-file]`);
    check('编辑态可删除原消息附件',
      (await page.locator(`#messages .message-row:has-text("${Q2_PLAIN}") #pendingFiles .pending-item`).count()) === 0);
    await page.click(`#messages .message-row:has-text("${Q2_PLAIN}") [data-edit-cancel]`);
    await page.waitForTimeout(250);
    check('取消编辑后底部 composer 恢复',
      (await page.locator('.composer-wrap #messageInput').count()) === 1);
    await page.click(`#messages .message-row:has-text("${Q2_PLAIN}") [data-edit-message]`);
    await page.waitForSelector(`#messages .message-row:has-text("${Q2_PLAIN}") .composer-wrap`);
    check('取消后重新编辑仍恢复原附件',
      (await page.locator(`#messages .message-row:has-text("${Q2_PLAIN}") #pendingFiles .pending-item`).count()) === 1);

    // 清空编辑框 → 底部按钮可用性跟着编辑框走（纯附件轮次：有附件仍应可发）
    await page.fill('#messages .message-row:has-text("第二问") #messageInput', '');
    await page.waitForTimeout(200);
    const b2 = await bridge();
    check('编辑框清空但仍有图片附件时「重新发送」保持可用', b2.sendDisabled === false, JSON.stringify(b2));

    // 改字后点**底部发送按钮**（不是编辑框里的按钮）→ 必须确认编辑，而不是发新消息
    await page.fill('#messages .message-row:has-text("第二问") #messageInput', EDITED_RICH);
    await page.click(`#messages .message-row:has-text("${Q2_PLAIN}") #pendingFiles [data-remove-file]`);
    await page.waitForTimeout(200);
    await page.click('#sendButton');
    const p2 = await waitFor(() => chatPayloads[before] || null);
    check('底部发送按钮确认了编辑（重发改后文本）', p2 && p2.message === EDITED_RICH, String(p2 && p2.message));
    check('重发的 display_message 就是编辑框里的文本', p2 && p2.display_message === EDITED_RICH,
      String(p2 && p2.display_message));
    check('重发使用编辑后保留的附件（已删除则数量为 0）',
      Boolean(p2) && Array.isArray(p2.attachments) && p2.attachments.length === 0,
      JSON.stringify(p2 && p2.attachments));
    const after4 = await messagesOf(convId);
    check('「编辑」从该提问截断：后端只剩更早的两条（U1,A1）',
      after4.length === 2, JSON.stringify(after4.map((m) => `${m.role}:${String(m.content).slice(0, 12)}`)));
    check('截断点之前的消息逐字节保留（改后文的这一轮不污染前缀）',
      after4[0] && after4[0].content === Q1 && after4[1] && after4[1].content === A1,
      JSON.stringify(after4.map((m) => String(m.content))));
    const b3 = await bridge();
    check('确认后编辑态已退出（底部恢复常态）',
      b3.editingClass === false && !b3.placeholder.includes('正在编辑'), JSON.stringify(b3));

    await reload();

    // ---- ⑤ 编辑最前面那条 → 后端清空（截断到头的边界）----
    await page.click(`#messages .message-row:has-text("${Q1}") [data-edit-message]`);
    await page.waitForSelector('#messages .message-row:has-text("第一问") #messageInput', { timeout: 10000 });
    const p3before = chatPayloads.length;
    await page.fill('#messages .message-row:has-text("第一问") #messageInput', EDITED);
    await page.waitForTimeout(200);
    await page.click('#messages [data-edit-confirm]');
    const p3 = await waitFor(() => chatPayloads[p3before] || null);
    check('编辑框内「重新发送」按钮生效', p3 && p3.message === EDITED, String(p3 && p3.message));
    check('编辑首条提问后后端清空', (await messagesOf(convId)).length === 0);

    // ---- ⑥ 零页面错误 ----
    check('页面无 JS 错误', pageErrors.length === 0, pageErrors.join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, error && error.message ? error.message : String(error));
  } finally {
    await browser.close();
  }

  console.log(failures.length ? `\n${failures.length} 项未通过` : '\n全部通过');
  process.exit(failures.length ? 1 : 0);
})();
