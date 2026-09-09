// 顶栏样式冒烟（源码 server，端口 8790）。
// 覆盖：任务计数为普通数字文本 / Skill 按钮无重复前缀 / MCP 只看颜色 / 刷新按钮回到操作区且「⋯」按钮已删。
// 运行：$env:NODE_PATH="<node_modules>"; node .tmptest\topbar_smoke.cjs
const { chromium } = require('playwright');

const BASE = process.env.NAIBA_SMOKE_BASE || 'http://127.0.0.1:8790';
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
  page.on('console', (msg) => { if (msg.type() === 'error') pageErrors.push(`console.error: ${msg.text()}`); });

  try {
    await page.goto(`${BASE}/`, { waitUntil: 'load', timeout: 20000 });
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    await page.waitForTimeout(1200);

    const snapshot = await page.evaluate(() => {
      const actions = document.querySelector('.topbar-actions');
      const taskCount = document.querySelector('#taskCount');
      const taskStyle = taskCount ? getComputedStyle(taskCount) : null;
      const skills = document.querySelector('#openSkills');
      const mcp = document.querySelector('#mcpStatus');
      const dot = mcp ? mcp.querySelector('i') : null;
      const reload = document.querySelector('#reloadPage');
      return {
        overflowGone: !document.querySelector('#topbarMoreButton') && !document.querySelector('#topbarOverflowMenu'),
        reloadInActions: Boolean(reload && actions?.contains(reload)),
        reloadText: reload ? reload.textContent.trim() : '',
        unloadExists: Boolean(document.querySelector('#unloadModel')),
        taskText: taskCount ? taskCount.textContent.trim() : '',
        taskBg: taskStyle ? taskStyle.backgroundColor : '',
        taskRadius: taskStyle ? taskStyle.borderRadius : '',
        skillsText: skills ? skills.textContent.replace(/\s+/g, ' ').trim() : '',
        mcpText: mcp ? mcp.querySelector('span')?.textContent.trim() : '',
        mcpTitle: mcp ? mcp.getAttribute('title') : '',
        mcpDotColor: dot ? getComputedStyle(dot).backgroundColor : '',
        mcpStateClasses: mcp ? [...mcp.classList].filter((name) => ['connected', 'calling', 'connecting', 'error', 'disconnected'].includes(name)) : [],
      };
    });

    check('「⋯」按钮与溢出菜单已删除', snapshot.overflowGone === true, JSON.stringify(snapshot));
    check('刷新按钮回到顶栏操作区', snapshot.reloadInActions && snapshot.reloadText.includes('刷新'),
      JSON.stringify({ inActions: snapshot.reloadInActions, text: snapshot.reloadText }));
    check('顶栏「卸载模型」按钮已移除（设置页仍可卸载）', snapshot.unloadExists === false, String(snapshot.unloadExists));
    check('任务计数是普通数字文本（无胶囊底色/圆角）',
      /^\d+$/.test(snapshot.taskText)
      && (snapshot.taskBg === 'rgba(0, 0, 0, 0)' || snapshot.taskBg === 'transparent')
      && snapshot.taskRadius === '0px',
      JSON.stringify({ text: snapshot.taskText, bg: snapshot.taskBg, radius: snapshot.taskRadius }));
    check('Skill 按钮不再重复前缀（形如「8 Skill」）', /^\d+\s+Skill$/.test(snapshot.skillsText), snapshot.skillsText);
    check('MCP 文字恒为「MCP」', snapshot.mcpText === 'MCP', snapshot.mcpText);
    check('MCP 状态只在颜色上体现（单一状态类 + 有 title）',
      snapshot.mcpStateClasses.length <= 1 && snapshot.mcpTitle.startsWith('MCP：'),
      JSON.stringify({ classes: snapshot.mcpStateClasses, title: snapshot.mcpTitle, color: snapshot.mcpDotColor }));

    // 按钮不被挤压成竖排/多行（窗口偏窄时曾出现「卸载模型」「刷新」竖排、「文件」拆两行）
    // #openFileTabs 默认隐藏（无已打开文件时），这里强制显示以便测量样式。
    await page.evaluate(() => { const btn = document.querySelector('#openFileTabs'); if (btn) btn.hidden = false; });
    await page.waitForTimeout(150);
    const layout = await page.evaluate(() => {
      const buttons = [...document.querySelectorAll('.topbar-actions .control-button, .topbar-actions .mcp-button')];
      return buttons.map((button) => {
        const rect = button.getBoundingClientRect();
        const label = button.querySelector('.button-label');
        return {
          id: button.id,
          height: Math.round(rect.height),
          width: Math.round(rect.width),
          // 单行文字：标签高度不超过一行
          labelHeight: label ? Math.round(label.getBoundingClientRect().height) : 0,
          nowrap: getComputedStyle(button).whiteSpace,
        };
      });
    });
    check('顶栏按钮均为单行（高度 34px 且文字不换行）',
      layout.length > 0 && layout.every((item) => item.height <= 36 && item.labelHeight <= 20 && item.nowrap === 'nowrap'),
      JSON.stringify(layout));

    // 文件按钮与其它按钮同款：默认底色/描边/hover 与「任务」一致
    const styles = await page.evaluate(async () => {
      const fileBtn = document.querySelector('#openFileTabs');
      const taskBtn = document.querySelector('#openTasks');
      const read = (el) => {
        const style = getComputedStyle(el);
        return { bg: style.backgroundColor, border: style.borderTopColor, color: style.color };
      };
      const idle = { file: read(fileBtn), task: read(taskBtn) };
      return { idle };
    });
    check('文件按钮默认样式与「任务」一致（浅色主题）',
      styles.idle.file.bg === styles.idle.task.bg && styles.idle.file.border === styles.idle.task.border,
      JSON.stringify(styles.idle));

    // hover 后两者仍一致
    await page.hover('#openFileTabs');
    await page.waitForTimeout(200);
    const hoverFile = await page.evaluate(() => {
      const style = getComputedStyle(document.querySelector('#openFileTabs'));
      return { bg: style.backgroundColor, border: style.borderTopColor };
    });
    await page.hover('#openTasks');
    await page.waitForTimeout(200);
    const hoverTask = await page.evaluate(() => {
      const style = getComputedStyle(document.querySelector('#openTasks'));
      return { bg: style.backgroundColor, border: style.borderTopColor };
    });
    check('文件按钮 hover 样式与「任务」一致',
      hoverFile.bg === hoverTask.bg && hoverFile.border === hoverTask.border,
      JSON.stringify({ file: hoverFile, task: hoverTask }));

    // 窄窗口：按钮整体换行可以，但绝不能被压成竖排文字（用户截图里的问题）
    for (const width of [900, 640]) {
      await page.setViewportSize({ width, height: 800 });
      await page.waitForTimeout(400);
      const narrow = await page.evaluate(() => [...document.querySelectorAll('.topbar-actions .control-button, .topbar-actions .mcp-button')]
        .map((button) => {
          const rect = button.getBoundingClientRect();
          return { id: button.id, height: Math.round(rect.height), width: Math.round(rect.width) };
        }));
      check(`窗口 ${width}px 下按钮仍为单行（高度 ≤36px）`,
        narrow.length > 0 && narrow.every((item) => item.height <= 36 && item.width >= 34),
        JSON.stringify(narrow));
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.waitForTimeout(300);

    // 打开设置页不得污染顶栏任务徽标（真实 bug：历史数据管理里的运行记录与顶栏共用 id
    // taskCount，loadStorageStats 把顶栏写成了「0 条（已结束 0）」）。
    await page.click('#openSettings');
    await page.waitForSelector('#settingsDialog[open]', { timeout: 10000 });
    await page.waitForTimeout(1200);
    const afterSettings = await page.evaluate(() => ({
      topbarTask: document.querySelector('#taskCount').textContent.trim(),
      storageTask: document.querySelector('#storageTaskCount')?.textContent.trim() || '',
      duplicateTaskIds: document.querySelectorAll('#taskCount').length,
    }));
    check('打开设置页后顶栏任务计数仍是纯数字',
      /^\d+$/.test(afterSettings.topbarTask),
      JSON.stringify(afterSettings));
    check('历史数据管理的运行记录独立显示（条/已结束）',
      afterSettings.storageTask.includes('条') && afterSettings.duplicateTaskIds === 1,
      JSON.stringify(afterSettings));
    await page.evaluate(() => document.querySelector('#settingsDialog').close());
    await page.waitForTimeout(300);

    // 刷新按钮确实触发重载
    await page.evaluate(() => { window.__naibaBeforeReload = 'set'; });
    await page.click('#reloadPage');
    await page.waitForLoadState('load', { timeout: 20000 }).catch(() => {});
    await page.waitForSelector('#messageInput', { timeout: 20000 });
    const marker = await page.evaluate(() => window.__naibaBeforeReload || '');
    check('点击刷新确实重载页面', marker === '', marker);

    check('零 pageerror / console.error', pageErrors.length === 0, pageErrors.slice(0, 3).join(' | '));
  } catch (error) {
    check('冒烟执行未抛异常', false, String(error && error.message));
  } finally {
    await browser.close();
  }

  console.log();
  console.log(`顶栏冒烟结果：${failures.length ? `${failures.length} 项失败 -> ${failures}` : '全部通过'}`);
  process.exit(failures.length ? 1 : 0);
})();
