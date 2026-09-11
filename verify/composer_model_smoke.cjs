// 会话模型下拉冒烟（由 composer_model_smoke.py 起独立源码 server 后调用）。
// 覆盖：目录返回空列表不算「已缓存」/ 会话内「↻ 重新检测模型」强制重拉并还原原选择 /
//       主动刷新失败要出声 / 会话没有明确选择时显示占位项而不是静默选中目录第一项。
//
// 同步口径：`saveModelSelection` 的最后一行是 `await populateComposerModels('')` 之后的
// `toast('API 已切换')`——看到这条 toast 即代表这一次切 API 的目录请求已完成、DOM 已重绘。
// 不靠 sleep 猜时序，否则会读到上一次切 API 留下的旧下拉内容（实测踩过）。
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8799';
const PROVIDER_A = process.env.NAIBA_SMOKE_PROVIDER_A || 'online:smoke-a';
const PROVIDER_B = process.env.NAIBA_SMOKE_PROVIDER_B || 'online:smoke-b';
const BOUND_MODEL = process.env.NAIBA_SMOKE_BOUND_MODEL || 'smoke-pro';
const REFRESH_VALUE = '__refresh_composer_models__';
const CATALOG = [
  { id: 'smoke-flash', name: 'smoke-flash' },   // 故意把非预期模型排第一（复刻供应商返回顺序）
  { id: 'smoke-pro', name: 'smoke-pro' },
  { id: 'smoke-1-flash', name: 'smoke-1-flash' },
];

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
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    // 本冒烟故意注入一次 502 目录故障（验证「刷新失败要出声」），浏览器随之打印的
    // "Failed to load resource ... 502" 是这次注入的必然产物，不计为页面错误。
    if (msg.text().includes('status of 502')) return;
    pageErrors.push(`console.error: ${msg.text()}`);
  });

  // 目录请求全部拦截：不真连供应商，只注入可控的目录形态。
  let mode = 'full';            // full | empty | fail
  let requestCount = 0;
  await page.route('**/api/providers/models', async (route) => {
    requestCount += 1;
    if (mode === 'fail') {
      await route.fulfill({
        status: 502, contentType: 'application/json',
        body: JSON.stringify({ error: '冒烟注入的目录故障' }),
      });
      return;
    }
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ models: mode === 'empty' ? [] : CATALOG }),
    });
  });

  const snapshot = () => page.evaluate(() => {
    const el = document.querySelector('#composerModelSelect');
    if (!el) return null;
    return {
      value: el.value,
      disabled: el.disabled,
      selectedText: el.selectedOptions[0] ? el.selectedOptions[0].textContent.trim() : '',
      options: [...el.options].map((o) => ({ value: o.value, text: o.textContent.trim(), disabled: o.disabled })),
    };
  });
  const toastText = () => page.evaluate(() => (document.querySelector('#toast') || {}).textContent || '');
  const modelOptions = (snap) => (snap ? snap.options.filter((o) => CATALOG.some((m) => m.id === o.value)) : []);
  async function waitFor(fn, timeout = 20000, step = 150) {
    const end = Date.now() + timeout;
    let last = null;
    while (Date.now() < end) {
      last = await fn();
      if (last) return last;
      await page.waitForTimeout(step);
    }
    return last;
  }
  // 诊断行：每步打印目录请求次数与两个下拉框的当前状态（失败时一眼看出卡在哪）。
  async function dump(label) {
    const info = await page.evaluate(() => {
      const api = document.querySelector('#modelSelect');
      const model = document.querySelector('#composerModelSelect');
      return {
        apiValue: api ? api.value : null,
        modelValue: model ? model.value : null,
        options: model ? [...model.options].map((o) => o.value) : [],
      };
    });
    console.log(`  · ${label} | requests=${requestCount} mode=${mode} api=${info.apiValue}`
      + ` model=${JSON.stringify(info.modelValue)} options=${JSON.stringify(info.options)}`);
  }
  const conversationModelName = () => page.evaluate(async () => {
    const payload = await fetch('/api/conversations').then((r) => r.json());
    return payload.conversations && payload.conversations.length
      ? String(payload.conversations[0].model_name || '') : null;
  });
  // 切顶栏 API 并等到本次切换彻底落地（见文件头「同步口径」）。
  async function switchApi(value) {
    await page.evaluate(() => { document.querySelector('#toast').textContent = ''; });
    await page.selectOption('#modelSelect', value);
    return (await waitFor(async () => ((await toastText()) === 'API 已切换' ? true : null))) === true;
  }
  // 等目录请求次数增长（切 API 的目录请求是处理器里的后续步骤，不能立即断言）。
  async function waitForRequestGrowth(before) {
    await waitFor(() => (requestCount > before ? true : null), 15000);
    return requestCount - before;
  }

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#composerModelSelect', { timeout: 20000 });

    // ---- ① 目录就绪后的默认选中：会话已保存的模型，而不是目录第一项 ----
    const loaded = await waitFor(async () => {
      const snap = await snapshot();
      return snap && modelOptions(snap).length === CATALOG.length ? snap : null;
    });
    check('目录返回后下拉列出全部模型', !!loaded && modelOptions(loaded).length === 3,
      JSON.stringify(loaded && modelOptions(loaded)));
    check('会话已保存的模型被选中（不是目录第一项）', !!loaded && loaded.value === BOUND_MODEL,
      JSON.stringify(loaded && { value: loaded.value, text: loaded.selectedText }));
    const last = loaded ? loaded.options[loaded.options.length - 1] : null;
    check('下拉末尾固定一项「↻ 重新检测模型」', !!last && last.value === REFRESH_VALUE
      && last.text.includes('重新检测模型'), JSON.stringify(last));

    // ---- ② 「↻ 重新检测模型」：强制重拉 + 还原原选择 + 不落库 ----
    let before = requestCount;
    await page.selectOption('#composerModelSelect', REFRESH_VALUE);
    check('会话内主动刷新会强制重拉目录（绕过缓存）', (await waitForRequestGrowth(before)) === 1,
      `+${requestCount - before}`);
    await waitFor(async () => {
      const snap = await snapshot();
      return snap && !snap.disabled ? snap : null;
    });
    const refreshed = await snapshot();
    check('刷新后还原刷新前的选中模型', refreshed.value === BOUND_MODEL,
      JSON.stringify({ value: refreshed.value, text: refreshed.selectedText }));
    check('刷新完成给出成功提示', (await toastText()).includes('已重新检测到 3 个模型'),
      await toastText());
    check('伪选项没有被写进会话模型', (await conversationModelName()) === BOUND_MODEL,
      String(await conversationModelName()));

    // ---- ③ 主动刷新失败必须出声，且不破坏已有目录 ----
    mode = 'fail';
    before = requestCount;
    await page.selectOption('#composerModelSelect', REFRESH_VALUE);
    await waitForRequestGrowth(before);
    await waitFor(async () => {
      const snap = await snapshot();
      return snap && !snap.disabled ? snap : null;
    });
    check('刷新失败在界面上出声（不静默）', (await toastText()).includes('无法获取模型目录'),
      await toastText());
    check('刷新失败不破坏已有目录与选择', (await snapshot()).value === BOUND_MODEL,
      JSON.stringify(await snapshot()));

    // ---- ④ 空目录不算「已查过」：换个 API 再切回来必须重拉 ----
    mode = 'empty';
    let mark = requestCount;
    check('切到 B（saveModelSelection 完成）', await switchApi(PROVIDER_B));
    check('切到 B 会拉一次目录（B 尚无缓存）', requestCount === mark + 1, `+${requestCount - mark}`);
    const emptySnap = await snapshot();
    await dump('切到 B（目录为空）');
    check('目录为空时只显示「请先在设置中检查模型」占位', emptySnap.value === ''
      && emptySnap.selectedText.includes('请先在设置中检查模型'),
      JSON.stringify({ value: emptySnap.value, text: emptySnap.selectedText }));

    mode = 'full';
    mark = requestCount;
    await switchApi(PROVIDER_A);
    const backToA = await snapshot();
    await dump('切回 A（目录已缓存）');
    check('目录非空时切 API 不重复请求（缓存仍生效）', requestCount === mark, `+${requestCount - mark}`);
    check('切回 A 立即用已缓存目录重绘', modelOptions(backToA).length === 3,
      JSON.stringify(modelOptions(backToA)));

    mark = requestCount;
    await switchApi(PROVIDER_B);
    await dump('再切到 B（空缓存必须重拉）');
    check('空目录不算已缓存：切回来必须重拉（模型后上架也能刷出来）', requestCount === mark + 1,
      `${mark} -> ${requestCount}`);
    const recovered = await snapshot();
    check('目录就绪后仍不静默选中第一项（占位待选）', recovered.value === ''
      && recovered.selectedText.includes('请选择模型'),
      JSON.stringify({ value: recovered.value, text: recovered.selectedText }));
    check('占位项是禁用的空值项', recovered.options[0].value === ''
      && recovered.options[0].disabled === true, JSON.stringify(recovered.options[0]));

    // ---- ⑤ 未选模型时发送被拦下；显式选择后才落库 ----
    await page.fill('#messageInput', '冒烟：未选模型不应发出');
    await page.click('#sendButton');
    await page.waitForTimeout(400);
    check('未选模型时发送被拦下并给出提示', (await toastText()).includes('模型'), await toastText());
    check('被拦下时不产生消息', (await page.locator('#messages .message-row').count()) === 0,
      String(await page.locator('#messages .message-row').count()));
    await page.fill('#messageInput', '');
    await page.selectOption('#composerModelSelect', 'smoke-flash');
    const persisted = await waitFor(async () => ((await conversationModelName()) === 'smoke-flash'));
    check('显式选择的模型落库到会话', persisted === true, String(await conversationModelName()));

    check('零页面错误 / console.error', pageErrors.length === 0, JSON.stringify(pageErrors.slice(0, 3)));
  } catch (error) {
    check('冒烟脚本自身异常', false, String(error && error.message ? error.message : error));
  } finally {
    await browser.close();
  }

  console.log(failures.length ? `\nFAILED ${failures.length} 项：${failures.join(' | ')}` : '\nALL PASS');
  process.exit(failures.length ? 1 : 0);
})();
