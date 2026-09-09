// 工具分类改版冒烟（随仓库发布的可复用资产；由 tool_groups_smoke.py 起源码 server 后调用）。
// 覆盖：6 组顺序 / 风险徽标与配色 / 徽标与分类名同行 / 说明非空 / 展开收起 /
//       分类级全选与计数 / 搜索框过滤与恢复 / MCP 按服务器二级分组与二级全选 / 零页面错误。
// 运行：.venv\Scripts\python.exe verify\tool_groups_smoke.py
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8793';
const failures = [];
function check(label, ok, detail = '') {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok || !detail ? '' : `  -> ${detail}`}`);
  if (!ok) failures.push(label);
}

const EXPECTED_GROUPS = ['读取与检索', '文件写入与编辑', '命令与脚本执行', '联网与外部服务', '视觉与图片', '任务与扩展'];
const EXPECTED_BADGES = {
  '读取与检索': '只读',
  '文件写入与编辑': '会改文件',
  '命令与脚本执行': '高风险',
  '联网与外部服务': '联网',
  '视觉与图片': '会写产物',
  '任务与扩展': '会改动',
};
const RETIRED_GROUPS = ['文件读取/搜索', '文件写入/编辑', '命令执行', 'Skill 脚本', '网络', '会话与记忆',
  'MCP', '后台/Job/子任务', 'ComfyUI', '能力/Skill 管理', '文档（PDF）', '视觉'];

async function scopeSnapshot(page) {
  return page.evaluate(() => {
    const scope = document.querySelector('#agentToolScope');
    const groups = [...scope.querySelectorAll('.agent-tool-group')];
    return {
      hasFilter: Boolean(document.querySelector('#agentToolFilter')),
      groupCount: groups.length,
      groups: groups.map((group) => {
        const title = group.querySelector('.group-title');
        const badge = group.querySelector('.group-badge');
        const desc = group.querySelector('.group-desc');
        const head = group.querySelector('.agent-tool-group-head');
        const titleRect = title.getBoundingClientRect();
        const badgeRect = badge ? badge.getBoundingClientRect() : null;
        return {
          name: group.dataset.group,
          titleText: title.childNodes[0]?.textContent.trim() || '',
          badge: badge ? badge.textContent.trim() : '',
          tone: badge ? badge.dataset.tone : '',
          badgeInline: Boolean(badgeRect) && Math.abs(badgeRect.top - titleRect.top) < 8
            && badgeRect.left >= titleRect.left,
          desc: desc ? desc.textContent.trim() : '',
          collapsed: group.classList.contains('collapsed'),
          bodyHidden: Boolean(group.querySelector('.agent-tool-group-body')?.hidden),
          toolCards: group.querySelectorAll('.permission-grid input[type="checkbox"]').length,
          count: group.querySelector('.group-count')?.textContent.trim() || '',
          headHeight: Math.round(head.getBoundingClientRect().height),
        };
      }),
    };
  });
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`));
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });
  page.on('dialog', (dialog) => dialog.accept());

  try {
    // ① 接口数据：分组顺序 / 徽标 / 二级分组字段齐备
    const catalog = await (await fetch(`${BASE}/api/tool_catalog`)).json();
    const apiGroups = catalog.groups || [];
    check('接口返回 6 个分类', apiGroups.length === 6, JSON.stringify(apiGroups.map((g) => g.name)));
    check('分类顺序与设计一致', JSON.stringify(apiGroups.map((g) => g.name)) === JSON.stringify(EXPECTED_GROUPS),
      JSON.stringify(apiGroups.map((g) => g.name)));
    check('每组都带风险徽标与配色', apiGroups.every((g) => g.badge && g.tone),
      JSON.stringify(apiGroups.map((g) => [g.name, g.badge, g.tone])));
    check('每组都有说明小字', apiGroups.every((g) => String(g.desc || '').trim()),
      JSON.stringify(apiGroups.map((g) => [g.name, g.desc])));
    check('每组都有 direct_tools / subgroups 字段',
      apiGroups.every((g) => Array.isArray(g.direct_tools) && Array.isArray(g.subgroups)), '');
    const names = apiGroups.map((g) => g.name);
    check('旧分类名不再出现', RETIRED_GROUPS.every((name) => !names.includes(name)),
      JSON.stringify(names.filter((name) => RETIRED_GROUPS.includes(name))));
    const allTools = apiGroups.flatMap((g) => g.tools);
    check('每个工具只归入一个分类', allTools.length === new Set(allTools).size && allTools.length === (catalog.tools || []).length,
      JSON.stringify({ placed: allTools.length, unique: new Set(allTools).size, total: (catalog.tools || []).length }));

    // ② 打开设置 → Agent → 卡片 → 弹层
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(800);
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('.settings-nav button[data-settings-tab="agent"]');
    await page.waitForTimeout(400);
    await page.click('#agentCards .agent-card');
    await page.waitForTimeout(600);

    let snap = await scopeSnapshot(page);
    check('弹层里有工具搜索框', snap.hasFilter === true, '');
    check('渲染 6 个分类', snap.groupCount === 6, JSON.stringify(snap.groups.map((g) => g.name)));
    check('分类顺序与接口一致', JSON.stringify(snap.groups.map((g) => g.name)) === JSON.stringify(EXPECTED_GROUPS),
      JSON.stringify(snap.groups.map((g) => g.name)));
    check('分类名与徽标逐项匹配',
      snap.groups.every((g) => EXPECTED_BADGES[g.name] === g.badge),
      JSON.stringify(snap.groups.map((g) => [g.name, g.badge])));
    check('徽标与分类名同行（不额外占列）', snap.groups.every((g) => g.badgeInline),
      JSON.stringify(snap.groups.map((g) => [g.name, g.badgeInline])));
    check('每个分类都有说明小字', snap.groups.every((g) => g.desc), JSON.stringify(snap.groups.map((g) => [g.name, g.desc])));
    check('分类头仍是一行（高度 ≤ 44px）', snap.groups.every((g) => g.headHeight <= 44),
      JSON.stringify(snap.groups.map((g) => [g.name, g.headHeight])));
    check('默认全部折叠（展开区隐藏）', snap.groups.every((g) => g.collapsed && g.bodyHidden), '');

    // ③ 展开「命令与脚本执行」→ 全选 → 计数与勾选状态
    const targetGroup = '命令与脚本执行';
    await page.click(`.agent-tool-group[data-group="${targetGroup}"] .agent-tool-group-head`);
    await page.waitForTimeout(300);
    snap = await scopeSnapshot(page);
    const expanded = snap.groups.find((g) => g.name === targetGroup);
    check(`展开「${targetGroup}」后工具可见`, expanded && !expanded.collapsed && !expanded.bodyHidden
      && expanded.toolCards === 2, JSON.stringify(expanded));
    const allCb = `.agent-tool-group[data-group="${targetGroup}"] input.group-select-all`;
    await page.evaluate((sel) => {
      const cb = document.querySelector(sel);
      if (cb && !cb.checked) cb.click();
    }, allCb);
    await page.waitForTimeout(300);
    const afterSelect = await page.evaluate((group) => {
      const el = document.querySelector(`.agent-tool-group[data-group="${group}"]`);
      const boxes = [...el.querySelectorAll('.permission-grid input[type="checkbox"]')];
      return {
        count: el.querySelector('.group-count')?.textContent.trim() || '',
        checked: boxes.filter((cb) => cb.checked).length,
        total: boxes.length,
      };
    }, targetGroup);
    check(`分类级全选把「${targetGroup}」全部勾上`, afterSelect.checked === afterSelect.total && afterSelect.total > 0,
      JSON.stringify(afterSelect));
    check('分类计数反映已选/总数', afterSelect.count === `${afterSelect.total}/${afterSelect.total}`,
      JSON.stringify(afterSelect));

    // ④ 搜索框：过滤成平铺结果 → 卡片带所属分类标签 → 清空恢复分组
    await page.fill('#agentToolFilter', 'vision');
    await page.waitForTimeout(400);
    const filtered = await page.evaluate(() => {
      const scope = document.querySelector('#agentToolScope');
      const boxes = [...scope.querySelectorAll('.permission-grid input[type="checkbox"]')];
      return {
        groupBlocks: scope.querySelectorAll('.agent-tool-group').length,
        names: boxes.map((cb) => cb.value),
        tags: [...scope.querySelectorAll('.tool-group-tag')].map((el) => el.textContent.trim()),
        headTitle: scope.querySelector('.group-title')?.childNodes[0]?.textContent.trim() || '',
      };
    });
    check('搜索命中 vision 相关工具', filtered.names.includes('vision_analyze') && filtered.names.includes('vision_image_ops'),
      JSON.stringify(filtered.names));
    check('搜索结果平铺成一个块', filtered.groupBlocks === 1 && filtered.headTitle === '搜索结果',
      JSON.stringify({ blocks: filtered.groupBlocks, head: filtered.headTitle }));
    check('搜索结果的卡片标出所属分类',
      filtered.tags.length === filtered.names.length && filtered.tags.every((tag) => tag === '视觉与图片'),
      JSON.stringify(filtered.tags));
    await page.fill('#agentToolFilter', '不存在的工具xyz');
    await page.waitForTimeout(300);
    const emptyHint = await page.evaluate(() => document.querySelector('#agentToolScope .tool-scope-empty')?.textContent.trim() || '');
    check('无匹配时给出空态提示', emptyHint.includes('没有匹配'), emptyHint);
    await page.fill('#agentToolFilter', '');
    await page.waitForTimeout(400);
    snap = await scopeSnapshot(page);
    check('清空搜索框恢复 6 组视图', snap.groupCount === 6, JSON.stringify(snap.groups.map((g) => g.name)));

    // ⑥ MCP 二级分组：拦截 /api/tool_catalog 注入假 MCP 工具（源码 server 未配 MCP 服务），
    //    验证「联网与外部服务」内按服务器分块 + 二级全选 + 计数。
    await page.route('**/api/tool_catalog', async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      const injected = [
        { name: 'mcp__comfy-mcp__system_stats', description: '查看 ComfyUI 状态' },
        { name: 'mcp__comfy-mcp__run_workflow', description: '运行工作流' },
        { name: 'mcp__other__ping', description: '探活' },
      ];
      for (const tool of injected) {
        payload.tools.push({ ...tool, group: '联网与外部服务', subgroup: tool.name.split('__')[1], default_selected: false });
      }
      const net = (payload.groups || []).find((group) => group.name === '联网与外部服务');
      net.tools.push(...injected.map((tool) => tool.name));
      net.subgroups.push(
        { name: 'comfy-mcp', tools: ['mcp__comfy-mcp__system_stats', 'mcp__comfy-mcp__run_workflow'] },
        { name: 'other', tools: ['mcp__other__ping'] },
      );
      await route.fulfill({ json: payload });
    });
    await page.reload({ waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(800);
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.click('.settings-nav button[data-settings-tab="agent"]');
    await page.waitForTimeout(400);
    await page.click('#agentCards .agent-card');
    await page.waitForTimeout(600);
    await page.click('.agent-tool-group[data-group="联网与外部服务"] .agent-tool-group-head');
    await page.waitForTimeout(300);
    const subgroupSnap = await page.evaluate(() => {
      const group = document.querySelector('.agent-tool-group[data-group="联网与外部服务"]');
      const blocks = [...group.querySelectorAll('.tool-subgroup')];
      return {
        count: blocks.length,
        titles: blocks.map((block) => block.querySelector('.subgroup-title')?.textContent.trim() || ''),
        tools: blocks.map((block) => [...block.querySelectorAll('.permission-grid input[type="checkbox"]')].map((cb) => cb.value)),
        hasSelectAll: blocks.every((block) => Boolean(block.querySelector('input.subgroup-select-all'))),
        directTools: [...group.querySelectorAll('.agent-tool-group-body > .permission-grid input[type="checkbox"]')]
          .map((cb) => cb.value),
        groupCount: group.querySelector('.group-count')?.textContent.trim() || '',
      };
    });
    check('MCP 工具按服务器分成二级分组', subgroupSnap.count === 2 && JSON.stringify(subgroupSnap.titles) === JSON.stringify(['comfy-mcp', 'other']),
      JSON.stringify(subgroupSnap));
    check('二级分组内只放该服务器的工具',
      JSON.stringify(subgroupSnap.tools) === JSON.stringify([
        ['mcp__comfy-mcp__system_stats', 'mcp__comfy-mcp__run_workflow'], ['mcp__other__ping'],
      ]), JSON.stringify(subgroupSnap.tools));
    check('平铺区不重复出现二级分组里的工具',
      !subgroupSnap.directTools.some((name) => name.startsWith('mcp__')), JSON.stringify(subgroupSnap.directTools));
    check('每个二级分组都有全选框', subgroupSnap.hasSelectAll === true, JSON.stringify(subgroupSnap));
    await page.evaluate(() => {
      const cb = document.querySelector('.tool-subgroup input.subgroup-select-all');
      if (cb && !cb.checked) cb.click();
    });
    await page.waitForTimeout(300);
    const subgroupSelected = await page.evaluate(() => {
      const block = document.querySelector('.tool-subgroup');
      const boxes = [...block.querySelectorAll('.permission-grid input[type="checkbox"]')];
      return {
        checked: boxes.filter((cb) => cb.checked).length,
        total: boxes.length,
        count: block.querySelector('.subgroup-count')?.textContent.trim() || '',
      };
    });
    check('二级全选只勾选该服务器的工具',
      subgroupSelected.checked === subgroupSelected.total && subgroupSelected.total === 2
      && subgroupSelected.count === '2/2', JSON.stringify(subgroupSelected));

    // ⑦ 零页面错误
    check('零页面错误 / console.error', pageErrors.length === 0, JSON.stringify(pageErrors.slice(0, 3)));
  } catch (error) {
    check('冒烟脚本自身异常', false, String(error && error.message ? error.message : error));
  } finally {
    await browser.close();
  }

  console.log(failures.length ? `\nFAILED ${failures.length} 项：${failures.join(' | ')}` : '\nALL PASS');
  process.exit(failures.length ? 1 : 0);
})();
