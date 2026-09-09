// Agent 设置页「快捷提示词套用 + 角色卡追加导入」冒烟（源码 server，端口 8790）。
// 前置：python verify/make_test_card.py && python server.py --port 8790
// 运行：$env:NODE_PATH="<node_modules 目录>"; node verify\agent_prompt_smoke.cjs
const { chromium } = require('playwright');
const path = require('path');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8790';
const CARD = path.resolve(__dirname, '..', 'data', 'generated', 'seed_fixture', '收藏冒烟角色卡.png');
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

async function apiJson(url, options = {}) {
  const response = await fetch(`${BASE}${url}`, { headers: { 'Content-Type': 'application/json' }, ...options });
  return response.json().catch(() => ({}));
}

(async () => {
  // 播种一条快捷提示词（冒烟后删除，避免污染用户配置）
  const presetText = '你是冒烟测试用的提示词。';
  const created = await apiJson('/api/conversation-prompt-presets', {
    method: 'POST',
    body: JSON.stringify({ title: '收藏冒烟提示词', system_prompt: presetText }),
  });
  const presetId = created?.preset?.id || '';
  check('已播种快捷提示词', Boolean(presetId), JSON.stringify(created).slice(0, 160));

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  const notFound = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });
  page.on('response', (res) => { if (res.status() === 404) notFound.push(res.url()); });
  page.on('dialog', (dialog) => dialog.accept());

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#sidebarWorkspaceTree .conversation-item', { timeout: 20000 });

    // 顶栏已无「对话设置」入口，设置页仍在
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('[data-settings-tab="agent"]');
    // Agent 页已卡片化：点卡片打开设置弹层（旧 #agentList/#addAgent 已移除）。
    await page.waitForSelector('#agentCards .agent-card', { timeout: 10000 });
    await page.click('#agentCards [data-agent-card] .agent-card-name');
    await page.waitForSelector('#agentDialog[open]', { timeout: 10000 });

    const wiring = await page.evaluate(() => {
      const select = document.querySelector('#agentPromptPresetSelect');
      const promptRow = [...document.querySelectorAll('#agentForm label')]
        .find((el) => (el.textContent || '').includes('系统提示词（预设与规则）'));
      const tools = document.querySelector('.agent-prompt-tools');
      return {
        hasSelect: Boolean(select),
        options: select ? [...select.options].map((o) => o.textContent) : [],
        hasImport: Boolean(document.querySelector('#importAgentCharacterCard')),
        hasFileInput: Boolean(document.querySelector('#agentCharacterCardFileInput')),
        toolsAfterPrompt: Boolean(promptRow && tools
          && promptRow.compareDocumentPosition(tools) & Node.DOCUMENT_POSITION_FOLLOWING),
        hint: document.querySelector('.agent-prompt-hint')?.textContent || '',
      };
    });
    check('Agent 表单有快捷提示词下拉', wiring.hasSelect, JSON.stringify(wiring));
    check('下拉已载入快捷提示词', wiring.options.some((t) => t.includes('收藏冒烟提示词')), JSON.stringify(wiring.options));
    check('导入角色卡按钮 + 文件输入就位', wiring.hasImport && wiring.hasFileInput, JSON.stringify(wiring));
    check('导入/套用入口位于「系统提示词」行下方', wiring.toolsAfterPrompt, JSON.stringify(wiring));
    check('提示文案说明「追加不覆盖」', wiring.hint.includes('追加'), wiring.hint);

    // 1) 角色卡导入 = 追加（不覆盖已有内容）
    await page.fill('#agentSystemPromptEdit', '原始规则：保持简短。');
    await page.setInputFiles('#agentCharacterCardFileInput', CARD);
    await page.waitForTimeout(1200);
    const afterImport = await page.inputValue('#agentSystemPromptEdit');
    check('导入后保留原有内容', afterImport.startsWith('原始规则：保持简短。'), afterImport.slice(0, 60));
    check('导入内容追加到末尾（空行分隔）', afterImport.includes('\n\n') && afterImport.includes('姓名：收藏冒烟角色'), afterImport.slice(0, 200));
    check('角色卡描述被解析', afterImport.includes('沉默寡言的图书馆管理员'), afterImport.slice(0, 200));

    // 2) 快捷提示词套用 = 覆盖（有内容时先确认，dialog 已自动 accept）
    await page.selectOption('#agentPromptPresetSelect', presetId);
    await page.waitForTimeout(600);
    const afterPreset = await page.inputValue('#agentSystemPromptEdit');
    check('套用快捷提示词覆盖为预设文本', afterPreset === presetText, afterPreset.slice(0, 120));

    // 3) 取消：不选任何预设时下拉回到占位
    const selectValue = await page.evaluate(() => document.querySelector('#agentPromptPresetSelect').value);
    check('套用后下拉保留当前选择', selectValue === presetId, selectValue);

    check('零 pageerror / console.error', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
    check('零 404 资源', notFound.length === 0, notFound.slice(0, 3).join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, String(error && error.message));
  } finally {
    await browser.close();
    if (presetId) {
      await apiJson(`/api/conversation-prompt-presets/${encodeURIComponent(presetId)}`, { method: 'DELETE' });
      console.log(`（已清理冒烟快捷提示词 ${presetId}）`);
    }
  }

  console.log();
  console.log(`Agent 提示词冒烟结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  // 不用 process.exit：undici 的 keep-alive 连接未关时强退会触发 libuv 断言（Windows/Node 24）。
  process.exitCode = failures.length ? 1 : 0;
})();
