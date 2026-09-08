// ============================================================
// 07-models-agents.js —— 拆分自 public/app.js 第 1966-2329 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, api, state, toast } from "./01-core.js";
import { renderSidebar } from "./08-conversations.js";
import { renderSkills } from "./09-settings.js";
import { appendPresetSkillsToComposer } from "./13-skill-refs.js";
export function renderUpdateStatus(status) {
  const current = status.current_version || '开发版';
  $('#currentVersion').textContent = status.current_commit ? `${current} · ${status.current_commit.slice(0, 7)}` : current;
  const select = $('#updateVersionSelect');
  const releases = Array.isArray(status.releases) ? status.releases : [];
  const latestVersion = String(status.latest_version || '').trim();
  const hasNewVersion = status.phase === 'available' && latestVersion;
  $('#latestVersion').textContent = hasNewVersion
    ? latestVersion
    : (status.phase === 'current' ? `${current}（当前）`
      : (status.phase === 'checking' ? '正在检查…' : '尚未检查'));
  const previousValue = select.value;
  // 重建版本下拉：仅保留可安装项，当前版本标记为「当前」。
  const options = releases
    .filter((release) => release.installable)
    .map((release) => {
      const option = document.createElement('option');
      option.value = release.tag;
      const label = release.current ? `${release.version}（当前）` : release.version;
      option.textContent = release.published_at ? `${label} · ${release.published_at.slice(0, 10)}` : label;
      return option;
    });
  select.replaceChildren(...options);
  select.disabled = options.length === 0 || ['checking', 'downloading', 'restarting'].includes(status.phase);
  // 检查完成后优先选中新版本，而不是保留检查前的“当前版本”。否则在不展开
  // 下拉框时看不出已经有更新。手动选择版本后仍按用户选择保留。
  const newerOption = options.find((option) => {
    const release = releases.find((item) => item.tag === option.value);
    return release && !release.current;
  });
  if (state.updateAutoSelectLatest && status.phase !== 'checking') {
    if (newerOption) select.value = newerOption.value;
    state.updateAutoSelectLatest = false;
  } else if (previousValue && options.some((option) => option.value === previousValue)) {
    select.value = previousValue;
  } else if (options.length > 0) {
    select.value = options[0].value;
  }
  const selectedTag = select.value;
  const selected = releases.find((release) => release.tag === selectedTag);
  const notes = selected && Array.isArray(selected.release_notes)
    ? selected.release_notes.filter((note) => String(note || '').trim())
    : (selected && selected.release_notes ? [String(selected.release_notes)] : []);
  const notesPanel = $('#updateNotes');
  const notesList = $('#updateNotesList');
  notesList.replaceChildren(...notes.map((note) => {
    const item = document.createElement('li');
    item.textContent = note;
    return item;
  }));
  // 仅根据是否存在更新内容显示/隐藏详情；不重置用户已展开/收起状态
  notesPanel.hidden = notes.length === 0;
  const messages = {
    idle: '启动后仅检查更新，不会自动安装；请手动选择版本后点击「立即更新」。',
    checking: '正在检查更新…',
    current: '当前已经是最新版本。',
    available: '发现新版本，可以立即安装。',
    downloading: '正在下载并校验更新，请勿关闭程序。',
    restarting: '更新已准备好，程序即将重启。',
    error: status.error || '检查更新失败。',
  };
  $('#updateMessage').textContent = !status.supported
    ? '当前运行目录不支持自动更新，请确认它来自受支持的 Git 仓库。'
    : (messages[status.phase] || messages.idle);
  const pending = status.pending_verification;
  if (pending && pending.pending) {
    $('#updateMessage').textContent = pending.ok
      ? `上次更新已完成并验证通过（${pending.target_version}）。`
      : `上次更新到 ${pending.target_version} 后版本校验失败，请重新检查更新或手动安装。`;
  } else if (status.mode === 'source') {
    $('#updateMessage').textContent = '源码模式不支持一键更新，请在终端中执行 git pull --ff-only origin master。';
  } else if (selected && selected.current) {
    $('#updateMessage').textContent = '当前已安装该版本，无需更新。';
  } else if (status.phase === 'available' && selected) {
    $('#updateMessage').textContent = `将安装 ${selected.version}，完成后程序自动重启。`;
  }
  const canInstall = status.supported && status.mode !== 'source'
    && selected && !selected.current
    && !['downloading', 'restarting'].includes(status.phase);
  $('#installUpdate').hidden = !canInstall;
  $('#checkUpdate').disabled = ['checking', 'downloading', 'restarting'].includes(status.phase);
}

export async function checkUpdate() {
  const button = $('#checkUpdate');
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  button.disabled = true;
  state.updateAutoSelectLatest = true;
  renderUpdateStatus({ ...(state.bootstrap.update || {}), phase: 'checking' });
  try {
    let status = await api('/api/update/check', { method: 'POST', body: {}, signal: controller.signal });
    state.bootstrap.update = status;
    renderUpdateStatus(status);
    const startedAt = Date.now();
    while (status.phase === 'checking' && Date.now() - startedAt < 30000) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      status = await api('/api/update');
      state.bootstrap.update = status;
      renderUpdateStatus(status);
    }
    if (status.phase === 'checking') {
      renderUpdateStatus({ ...status, phase: 'error', error: '检查更新超时，请稍后重试。' });
    }
  } catch (error) {
    try {
      const status = await api('/api/update');
      state.bootstrap.update = status;
      renderUpdateStatus(status.phase === 'checking'
        ? { ...status, phase: 'error', error: '检查更新超时，请稍后重试。' }
        : status);
    } catch (_) {
      renderUpdateStatus({ ...(state.bootstrap.update || {}), phase: 'error', error: error.message });
    }
  } finally {
    clearTimeout(timeout);
    button.disabled = false;
  }
}

export async function installUpdate() {
  const button = $('#installUpdate');
  const select = $('#updateVersionSelect');
  const tag = select.value;
  const status = state.bootstrap.update || {};
  const releases = Array.isArray(status.releases) ? status.releases : [];
  const selected = releases.find((release) => release.tag === tag);
  if (!selected) {
    toast('请先选择要安装的版本');
    return;
  }
  if (!confirm(`确定要安装版本 ${selected.version} 吗？更新完成后程序将自动重启。`)) {
    return;
  }
  button.disabled = true;
  try {
    const newStatus = await api('/api/update/install', { method: 'POST', body: { tag } });
    state.bootstrap.update = newStatus;
    renderUpdateStatus(newStatus);
    toast('正在下载更新，完成后会自动重启');
  } catch (error) {
    toast(`更新失败：${error.message}`);
    button.disabled = false;
  }
}

export function populateModels() {
  const select = $('#modelSelect');
  const previous = select.value;
  const profiles = state.bootstrap.model_profiles || state.bootstrap.providers || [];
  const defaultKey = String(state.bootstrap.default_model_key || '');
  select.innerHTML = '';

  if (!profiles.length) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = '请先在设置中添加模型';
    select.append(opt);
  } else {
    const online = profiles.filter((p) => (p.kind || 'online') === 'online');
    const local = profiles.filter((p) => p.kind === 'local');
    const group = (label, list) => {
      if (!list.length) return;
      const og = document.createElement('optgroup');
      og.label = label;
      list.forEach((p) => {
        const option = document.createElement('option');
        option.value = p.model_key;
        option.textContent = `${p.name} · ${p.model}`;
        og.append(option);
      });
      select.append(og);
    };
    group('在线 API', online);
    group('本地模型', local);
  }

  if ([...select.options].some((o) => o.value === previous)) {
    select.value = previous;
  } else if (defaultKey && [...select.options].some((o) => o.value === defaultKey)) {
    select.value = defaultKey;
  } else if (select.options.length) {
    select.selectedIndex = 0;
  }
  updateUnloadModelButton();
}

export function selectedProvider() {
  const value = $('#modelSelect')?.value || '';
  if (!value) return null;
  const profiles = state.bootstrap.model_profiles || state.bootstrap.providers || [];
  return profiles.find((p) => p.model_key === value) || null;
}

export function localProviderKind(provider) {
  if (!provider) return '';
  const requestFormat = String(provider.request_format || '').toLowerCase();
  return ['ollama', 'lm_studio'].includes(requestFormat) ? requestFormat : '';
}

export function updateUnloadModelButton() {
  const busy = Boolean(state.chatRunId || state.taskSubmitting);
  // 顶栏的「卸载模型」按钮已移除（本地模型卸载统一在「设置 → API 供应商」里操作）。
  const settingsButton = $('#unloadProviderModel');
  if (settingsButton) {
    const providerId = $('#providerId')?.value || '';
    const provider = (state.bootstrap?.providers || []).find((item) => item.id === providerId);
    const kind = localProviderKind(provider);
    settingsButton.hidden = !kind;
    settingsButton.disabled = busy;
    settingsButton.title = kind ? `卸载${kind === 'ollama' ? ' Ollama' : ' LM Studio'} 当前模型` : '';
  }
}

export async function unloadProviderModel(provider) {
  const kind = localProviderKind(provider);
  if (!provider || !kind) {
    toast('当前供应商不是支持卸载的本地模型');
    return;
  }
  if (state.chatRunId || state.taskSubmitting) {
    toast('请先等待当前对话结束');
    return;
  }
  if (!confirm(`卸载${kind === 'ollama' ? ' Ollama' : ' LM Studio'} 模型“${provider.model}”？`)) return;
  $('#unloadProviderModel').disabled = true;
  try {
    const result = await api('/api/models/unload', {
      method: 'POST',
      body: { model_key: provider.model_key },
    });
    toast(`${result.provider} 模型已卸载，显存和内存将被回收`);
  } catch (error) {
    toast(`卸载失败：${error.message}`);
  } finally {
    updateUnloadModelButton();
  }
}

export async function unloadConfiguredProviderModel() {
  const providerId = $('#providerId').value;
  const provider = (state.bootstrap.model_profiles || state.bootstrap.providers || []).find((item) => item.id === providerId);
  await unloadProviderModel(provider);
}

export async function saveModelSelection() {
  const value = $('#modelSelect').value;
  const result = await api('/api/settings', { method: 'POST', body: { model_key: value } });
  Object.assign(state.bootstrap.settings, result.settings);
  state.bootstrap.default_model_key = result.default_model_key || value;
  if (state.conversationId) {
    try {
      await api(`/api/conversations/${state.conversationId}/settings`, {
        method: 'POST',
        body: { model_key: value },
      });
    } catch (error) {
      console.debug('[naiba] 保存对话模型失败:', error.message);
    }
  }
  populateModels();
  toast('模型已切换');
}

// 根据对话已保存的 model_key 恢复模型选择；未绑定或已删除时回退到全局默认
export function applyConversationModel(conversation) {
  const select = $('#modelSelect');
  if (!select) return;
  const target = String(conversation?.model_key || '');
  if (target && [...select.options].some((o) => o.value === target)) {
    select.value = target;
    updateUnloadModelButton();
    return;
  }
  const fallback = String(state.bootstrap.default_model_key || '');
  if (fallback && [...select.options].some((o) => o.value === fallback)) {
    select.value = fallback;
  } else if (select.options.length) {
    select.selectedIndex = 0;
  }
  updateUnloadModelButton();
}

export function renderAgents() {
  const select = $('#agentSelect');
  if (!select) return;
  const agents = state.bootstrap?.agents || [];
  select.innerHTML = '';
  agents.forEach((agent) => {
    const option = document.createElement('option');
    option.value = agent.id;
    option.textContent = agent.name;
    select.append(option);
  });
  if (!select.options.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = '暂无 Agent';
    select.append(option);
  }
  applyConversationAgent(state.conversations.find((item) => item.id === state.conversationId));
}

// 会话 Agent：首轮前可下拉选择；首轮固化工具集后只读展示（切换会破坏缓存，需新开对话）。
export function applyConversationAgent(conversation) {
  const select = $('#agentSelect');
  if (!select) return;
  const agents = state.bootstrap?.agents || [];
  const agentId = String(conversation?.agent_id || '');
  let agent = agents.find((a) => String(a.id) === agentId);
  const locked = Boolean(conversation?.enabled_tool_ids && conversation.enabled_tool_ids.length);
  if (agent && [...select.options].some((o) => o.value === agent.id)) {
    select.value = agent.id;
  } else {
    const fallback = String(state.bootstrap?.default_agent_id || '');
    if ([...select.options].some((o) => o.value === fallback)) {
      select.value = fallback;
    } else if (select.options.length) {
      select.selectedIndex = 0;
    }
    agent = agents.find((a) => String(a.id) === fallback) || agents[0] || null;
  }
  select.disabled = locked;
  select.title = locked
    ? `当前会话已绑定 Agent「${agent?.name || ''}」并固化工具集，会话内不可切换。如需切换，请让 AI 总结当前对话，复制总结后新开对话。`
    : '选择该会话使用的 Agent（发送首条消息后固化，之后不可切换）。';
  renderSkills($('#skillSearch')?.value || '');
}

export async function saveAgentSelection() {
  const value = $('#agentSelect').value;
  if (!state.conversationId) {
    toast('请先打开或新建一个对话');
    return;
  }
  try {
    const updated = await api(`/api/conversations/${state.conversationId}/settings`, {
      method: 'POST',
      body: { agent_id: value },
    });
    const index = state.conversations.findIndex((item) => item.id === state.conversationId);
    if (index >= 0) state.conversations[index] = { ...state.conversations[index], ...updated };
    renderSidebar();
    renderSkills($('#skillSearch')?.value || '');
    toast('Agent 已切换');
    appendPresetSkillsToComposer(value);
  } catch (error) {
    toast(`切换失败：${error.message}`);
    applyConversationAgent(state.conversations.find((item) => item.id === state.conversationId));
  }
}

