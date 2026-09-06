// ============================================================
// 05-bootstrap.js —— 拆分自 public/app.js 第 1502-1605 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

async function authenticate(token) {
  const response = await fetch('/api/auth', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  });
  if (!response.ok) throw new Error('访问口令不正确');
  state.token = token;
  localStorage.setItem('naibaChatToken', token);
}

function renderNetworkAccess() {
  const access = state.bootstrap || {};
  const configuredHost = String(access.settings?.host || '0.0.0.0');
  const pendingRestart = Boolean(access.lan_restart_required);
  const address = access.lan_enabled && access.lan_url
    ? access.lan_url
    : (pendingRestart || configuredHost === '127.0.0.1' ? '当前仅本机访问' : '手机访问不可用');
  const reason = pendingRestart
    ? '手机访问已启用，请完全退出并重新启动 naiba-chat。'
    : (access.lan_reason || '手机与电脑需连接同一局域网。');
  $('#lanAddress').textContent = address;
  $('#connectionAddress').textContent = access.lan_url || access.local_url || '未检测到可用地址';
  $('#connectionReason').textContent = reason;
  $('#enableLanActions').hidden = access.lan_enabled || pendingRestart || configuredHost === '0.0.0.0';
  const copyButton = $('#copyAddress');
  copyButton.disabled = !access.lan_enabled || !access.lan_url;
  copyButton.title = copyButton.disabled ? reason : '复制手机访问地址';
}

async function enableLanAccess() {
  try {
    const result = await api('/api/settings', { method: 'POST', body: { host: '0.0.0.0' } });
    Object.assign(state.bootstrap.settings, result.settings || {});
    state.bootstrap.lan_restart_required = Boolean(result.restart_required);
    renderNetworkAccess();
    toast('手机访问已启用，请完全退出并重新启动 naiba-chat');
  } catch (error) {
    toast(`启用手机访问失败：${error.message}`);
  }
}

async function initialize() {
  restoreSidebarWidth();
  sidebarScrollToActive = true;
  try {
    state.bootstrap = await api('/api/bootstrap');
  } catch (error) {
    state.token = '';
    localStorage.removeItem('naibaChatToken');
    localStorage.removeItem('lanSkillToken');
    $('#authDialog').showModal();
    return;
  }
  const migration = state.bootstrap.data_location?.migration;
  if (migration?.migrated) {
    const restored = [
      migration.config ? 'API 配置' : '',
      migration.data ? '对话数据' : '',
    ].filter(Boolean).join('和');
    toast(`已从旧目录恢复${restored || '数据'}`);
  }
  $('#serverDot').className = 'connected';
  $('#serverLabel').textContent = '服务已连接';
  state.workspaces = Array.isArray(state.bootstrap?.workspaces) ? state.bootstrap.workspaces : [];
  renderNetworkAccess();
  populateModels();
  renderAgents();
  renderAgentManager();
  // 恢复模式 Tab 状态
  $$('.mode-tab').forEach(tab => tab.classList.toggle('active', tab.dataset.mode === state.mode));
  populateRuntimeSettings();
  populateVisionSettings();
  populateSearchSettings();
  renderSkills();
  renderProviders();
  renderMcp();
  renderUpdateStatus(state.bootstrap.update || {});
  await loadConversationPromptPresets();
  await loadConversations();
  await loadStarterPrompts();
  await loadTasks();
  startTaskSync();
  startConversationSync();
  startMcpPoll();
  startUpdatePoll();
}

// 后台检查可能启动时才开始（checking 阶段），前端以 30 秒间隔轮询感知结果。
function startUpdatePoll() {
  if (state.updatePollTimer) return;
  state.updatePollTimer = window.setInterval(async () => {
    const status = state.bootstrap.update || {};
    if (['checking', 'downloading', 'restarting'].includes(status.phase)) return;
    try {
      const next = await api('/api/update');
      if (next.checked_at !== status.checked_at || next.phase !== status.phase) {
        state.bootstrap.update = next;
        renderUpdateStatus(next);
      }
    } catch (_) { /* 网络抖动时忽略，下一轮重试 */ }
  }, 30000);
}

