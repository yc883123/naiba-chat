// ============================================================
// 15-bind-events.js —— 拆分自 public/app.js 第 6658-7653 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, $$, api, contextMenuPreviousFocus, copyText, draggedFileCache, editableElement, ensureContextMenu, hideTextContextMenu, runTextContextAction, showTextContextMenu, state, toast } from "./01-core.js";
import { closeContextUsagePopover, closeImageLightbox, ensureImageContextMenu, hideImageContextMenu, isPywebview, openImageLightbox, positionContextUsagePopover, runImageContextAction, showImageContextMenu, toggleContextUsagePopover, updateSendButtonState } from "./03-media.js";
import { branchMessage, isNearBottom, setStickToBottom, startEditMessage } from "./04-messages.js";
import { authenticate, enableLanAccess, initialize } from "./05-bootstrap.js";
import { switchPermissionMode } from "./06-tasks-plans.js";
import { checkUpdate, installUpdate, populateModels, renderUpdateStatus, saveAgentSelection, saveModelSelection, unloadConfiguredProviderModel, unloadCurrentModel, unloadProviderModel } from "./07-models-agents.js";
import { applyConversationPromptPreset, clearConversationMessages, clearTerminalTasks, closeConversationPromptPresetForm, createConversation, createWorkspace, importCharacterCard, importConversationPromptPresetCard, loadConversationPromptPresets, onComposerWorkspaceChange, onSidebarTreeClick, openConversation, openConversationPromptPresetForm, openConversationSettings, renderConversationPromptPresets, renderSidebar, renderSidebarWindow, saveConversationPromptPreset, saveConversationSettings, setSidebarScrollRaf, sidebarRowCache, sidebarScrollRaf } from "./08-conversations.js";
import { addProvider, addSearchProfile, applyProviderModelCapabilities, applyToolTemplate, cancelProviderEdit, cleanImageCache, collectTemplateFromCurrent, compactDatabase, deleteAgent, deleteSearchProfile, deleteToolTemplate, deleteVisionProvider, editProvider, hideAgentForm, loadMcpServers, loadProviderModels, loadStorageStats, loadWorkspaceTree, onToolPresetSelect, openVisionProviderForm, persistSearchProfiles, pickWorkspace, populateVisionSettings, refreshImageCacheSize, renderAgentManager, renderAgentSkillPicker, renderImageCompressRow, renderProviders, renderProxyRows, renderSearchProfileFields, renderSkills, saveAccessToken, saveAgentForm, saveMcpServer, saveProvider, saveRuntimeSettings, saveSearchSettings, saveVisionSettings, saveWorkspaceSettings, searchProfiles, showAgentForm, showProviderForm, syncProviderKindOptions, testProvider, testSearchConnection, testVisionConnection, toggleAllToolGroups, toggleCustomModel, toggleProviderKey, updateProviderContextField, updateProviderFormatGuide, updateProviderVisionHint } from "./09-settings.js";
import { readAsDataUrl, renderPendingFiles, uploadFiles } from "./10-upload.js";
import { cancelCurrentRun, handlePasteImage, openStarterPromptDialog, reloadPage, saveStarterPrompt, sendMessage, startSkillEdit, startSkillInstall, toggleDeepReasoning, toggleLightweightFeature, toggleRichText, updateDeepReasoningButton } from "./12-chat-input.js";
import { commitSkillSelection, hideSkillPopup, insertSkillRefAtCursor, moveSkillPopupSelection, popupState, positionSkillPopup, renderInputMirror, resizeTextarea, setSkillPopupSelection, skillList, updateSkillPopup } from "./13-skill-refs.js";
import { activateFileTab, activeFileTab, applyFilePanelOpenClass, cancelFileEdit, closeFilePanel, closeSidebar, filePanelState, filePanelUsable, openFilePanel, openSidebar, removeFileTab, reopenFilePanel, restoreLeftSidebarCollapse, saveFileTab, setLeftSidebarCollapsed, sidebarDesktop, startFileEdit, updateFileTabsButton } from "./14-file-panel.js";
export function bindEvents() {
  document.addEventListener('contextmenu', (event) => {
    hideTextContextMenu();
    const editable = editableElement(event.target);
    if (editable) {
      event.preventDefault();
      showTextContextMenu(event, '', 'edit', editable);
      return;
    }
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.toString().trim()) return;
    const range = selection.getRangeAt(0);
    const startNode = range.startContainer.nodeType === Node.ELEMENT_NODE
      ? range.startContainer : range.startContainer.parentElement;
    const endNode = range.endContainer.nodeType === Node.ELEMENT_NODE
      ? range.endContainer : range.endContainer.parentElement;
    const messageBody = startNode?.closest('.message-body');
    if (!messageBody || endNode?.closest('.message-body') !== messageBody) return;
    event.preventDefault();
    showTextContextMenu(event, selection.toString(), 'selection', null);
  });
  document.addEventListener('pointerdown', (event) => {
    if (!event.target.closest?.('#textContextMenu')) hideTextContextMenu();
  });
  const textContextMenu = ensureContextMenu();
  textContextMenu.addEventListener('mousedown', (event) => {
    // 阻止菜单按钮抢走文本框焦点，保持选中状态可见。
    if (event.target.closest?.('[data-context-action]')) event.preventDefault();
  });
  textContextMenu.addEventListener('click', (event) => {
    const button = event.target.closest('[data-context-action]');
    if (button) runTextContextAction(button.dataset.contextAction);
  });
  textContextMenu.addEventListener('keydown', (event) => {
    const buttons = [...textContextMenu.querySelectorAll('button:not(:disabled)')];
    const index = buttons.indexOf(document.activeElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      hideTextContextMenu();
      contextMenuPreviousFocus?.focus?.({ preventScroll: true });
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const offset = event.key === 'ArrowDown' ? 1 : -1;
      buttons[(index + offset + buttons.length) % buttons.length]?.focus();
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      buttons[event.key === 'Home' ? 0 : buttons.length - 1]?.focus();
    }
  });
  window.addEventListener('blur', hideTextContextMenu);
  window.addEventListener('resize', hideTextContextMenu);
  window.addEventListener('scroll', hideTextContextMenu, true);
  // 大图右键 → 复制图片剪贴板（仅 pywebview 窗口自绘菜单；真实浏览器保留原生“复制图片”菜单）。
  document.addEventListener('contextmenu', (event) => {
    if (!isPywebview()) return;
    if (!event.target.closest?.('#imageLightboxImg')) return;
    event.preventDefault();
    showImageContextMenu(event);
  });
  document.addEventListener('pointerdown', (event) => {
    if (!event.target.closest?.('#imageContextMenu')) hideImageContextMenu();
  });
  const imageMenu = ensureImageContextMenu();
  imageMenu.addEventListener('contextmenu', (event) => event.preventDefault());
  imageMenu.addEventListener('click', (event) => {
    const button = event.target.closest('[data-image-context-action]');
    if (button) runImageContextAction(button.dataset.imageContextAction);
  });
  imageMenu.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { event.preventDefault(); hideImageContextMenu(); }
  });
  window.addEventListener('blur', hideImageContextMenu);
  window.addEventListener('resize', hideImageContextMenu);
  window.addEventListener('scroll', hideImageContextMenu, true);
  $('#authForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await authenticate($('#tokenInput').value.trim());
      $('#authDialog').close();
      await initialize();
    } catch (error) {
      $('#authError').textContent = error.message;
    }
  });
  $('#newChatButton').addEventListener('click', () => createConversation('', '', true));
  $('#openCurrentConversationSettings')?.addEventListener('click', () => {
    if (state.conversationId) openConversationSettings(state.conversationId);
    else toast('请先打开或新建一个对话');
  });
  $('#modelSelect').addEventListener('change', saveModelSelection);
  $('#unloadModel').addEventListener('click', unloadCurrentModel);
  $('#agentSelect').addEventListener('change', saveAgentSelection);
  $('#openSkills').addEventListener('click', () => $('#skillsDialog').showModal());
  $('#openTasks').addEventListener('click', () => $('#tasksDialog').showModal());
  // 显式刷新按钮：适配 EXE 内嵌 pywebview 无法使用 F5 的场景，EXE 与浏览器通用。
  $('#reloadPage')?.addEventListener('click', () => reloadPage());
  $('#clearTerminalTasks').addEventListener('click', clearTerminalTasks);
  $('#clearConversationMessages').addEventListener('click', clearConversationMessages);
  $('#activeTaskBar').addEventListener('click', (event) => {
    if (event.target.closest('[data-open-tasks]')) $('#tasksDialog').showModal();
  });
  $('#permissionModeSwitch').addEventListener('click', (event) => {
    const button = event.target.closest('[data-permission-mode]');
    if (button) switchPermissionMode(button.dataset.permissionMode);
  });
  $('#taskList').addEventListener('click', (event) => {
    const item = event.target.closest('[data-task-id]');
    if (!item) return;
    const task = state.tasks.find((value) => value.id === item.dataset.taskId);
    if (task) { $('#tasksDialog').close(); openConversation(task.conversation_id); }
  });
  $('#mcpStatus').addEventListener('click', () => {
    $('#settingsDialog').showModal();
    switchSettingsTab('connections');
  });
  // 顶栏「更多」溢出菜单：切换开合，点击外部关闭。
  $('#topbarMoreButton')?.addEventListener('click', (event) => {
    event.stopPropagation();
    const menu = $('#topbarOverflowMenu');
    if (!menu) return;
    menu.hidden = !menu.hidden;
    $('#topbarMoreButton').setAttribute('aria-expanded', String(!menu.hidden));
  });
  document.addEventListener('click', (event) => {
    const wrap = $('#topbarOverflowMenu');
    if (!wrap || wrap.hidden) return;
    if (wrap.contains(event.target)) return;
    if (event.target.closest?.('#topbarMoreButton')) return;
    wrap.hidden = true;
    $('#topbarMoreButton')?.setAttribute('aria-expanded', 'false');
  });
  $('#saveMcpServer')?.addEventListener('click', saveMcpServer);
  $('#openSettings').addEventListener('click', () => {
    $('#settingsDialog').showModal();
    loadStorageStats();
    refreshImageCacheSize();
  });
  $$('[data-close]').forEach((button) => button.addEventListener('click', () => $(`#${button.dataset.close}`).close()));
  $('#conversationSettingsForm').addEventListener('submit', saveConversationSettings);
  $('#conversationPromptPresetSelect')?.addEventListener('change', (event) => applyConversationPromptPreset(event.target.value));
  $('#importCharacterCardBtn')?.addEventListener('click', () => $('#characterCardFileInput')?.click());
  $('#characterCardFileInput')?.addEventListener('change', (event) => {
    const file = event.target.files?.[0];
    if (file) importCharacterCard(file);
    event.target.value = '';
  });
  $('#addConversationPromptPreset')?.addEventListener('click', () => openConversationPromptPresetForm());
  $('#cancelConversationPromptPreset')?.addEventListener('click', closeConversationPromptPresetForm);
  $('#conversationPromptPresetForm')?.addEventListener('submit', saveConversationPromptPreset);
  $('#conversationPromptPresetSearch')?.addEventListener('input', renderConversationPromptPresets);
  $('#conversationPromptPresetList')?.addEventListener('click', async (event) => {
    const edit = event.target.closest('[data-conversation-preset-edit]');
    if (edit) { openConversationPromptPresetForm(edit.dataset.conversationPresetEdit); return; }
    const remove = event.target.closest('[data-conversation-preset-delete]');
    if (!remove || !confirm('确定删除这个快捷提示词吗？')) return;
    try { await api(`/api/conversation-prompt-presets/${encodeURIComponent(remove.dataset.conversationPresetDelete)}`, { method: 'DELETE' }); await loadConversationPromptPresets(); toast('已删除快捷提示词'); } catch (error) { toast(`删除失败：${error.message}`); }
  });
  $('#importPresetCharacterCard')?.addEventListener('click', () => $('#presetCharacterCardFileInput')?.click());
  $('#presetCharacterCardFileInput')?.addEventListener('change', (event) => { const file = event.target.files?.[0]; if (file) importConversationPromptPresetCard(file); event.target.value = ''; });
  $('#skillSearch').addEventListener('input', (event) => renderSkills(event.target.value));
  $('#composerForm').addEventListener('submit', (event) => {
    event.preventDefault();
    if (state.chatRunId || state.abortController) cancelCurrentRun();
    else sendMessage();
  });
  $('#messageInput').addEventListener('input', () => { resizeTextarea(); renderInputMirror(); updateSkillPopup(); updateSendButtonState(); });
  $('#messageInput').addEventListener('select', updateSkillPopup);
  $('#messageInput').addEventListener('click', updateSkillPopup);
  $('#messageInput').addEventListener('focus', updateSkillPopup);
  $('#messageInput').addEventListener('scroll', () => { const mirror = $('#inputMirror'); if (mirror) mirror.scrollTop = $('#messageInput').scrollTop; positionSkillPopup(); });
  window.addEventListener('resize', positionSkillPopup);
  $('#messageInput').addEventListener('keydown', (event) => {
    if (popupState.open) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        moveSkillPopupSelection(event.key === 'ArrowDown' ? 1 : -1);
        return;
      }
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        const sel = popupState.items[popupState.selectedIndex];
        if (sel) commitSkillSelection(sel);
        return;
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        hideSkillPopup();
        return;
      }
    }
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (state.chatRunId || state.abortController) {
        toast('回复进行中，请等待完成或先点击停止');
      } else {
        sendMessage();
      }
    }
  });
  $('#skillPopup').addEventListener('mousedown', (event) => event.preventDefault());
  $('#skillPopup').addEventListener('click', (event) => {
    const button = event.target.closest('[data-skill-index]');
    if (!button) return;
    const sel = popupState.items[Number(button.dataset.skillIndex)];
    if (sel) commitSkillSelection(sel);
  });
  $('#skillPopup').addEventListener('mouseover', (event) => {
    const button = event.target.closest('[data-skill-index]');
    if (!button) return;
    const idx = Number(button.dataset.skillIndex);
    if (idx !== popupState.selectedIndex) setSkillPopupSelection(idx);
  });
  $('#skillList').addEventListener('click', (event) => {
    const button = event.target.closest('[data-skill-insert]');
    if (!button) return;
    const skill = skillList().find((s) => s.id === button.dataset.skillInsert);
    if (!skill) return;
    insertSkillRefAtCursor(skill);
    renderInputMirror();
    $('#skillsDialog').close();
    $('#messageInput').focus();
  });
  $('#attachButton').addEventListener('click', () => $('#fileInput').click());
  $('#lightweightToolsToggle')?.addEventListener('change', (event) => toggleLightweightFeature('tools', event.target.checked));
  $('#lightweightSkillsToggle')?.addEventListener('change', (event) => toggleLightweightFeature('skills', event.target.checked));
  $('#richTextToggle')?.addEventListener('change', (event) => toggleRichText(event.target.checked));
  // composer-meta「轻量」折叠面板开合
  $('#composerMetaMoreButton')?.addEventListener('click', (event) => {
    event.stopPropagation();
    const panel = $('#composerMetaPanel');
    if (!panel) return;
    panel.hidden = !panel.hidden;
    $('#composerMetaMoreButton').setAttribute('aria-expanded', String(!panel.hidden));
  });
  document.addEventListener('click', (event) => {
    const panel = $('#composerMetaPanel');
    if (!panel || panel.hidden) return;
    if (panel.contains(event.target)) return;
    if (event.target.closest?.('#composerMetaMoreButton')) return;
    panel.hidden = true;
    $('#composerMetaMoreButton')?.setAttribute('aria-expanded', 'false');
  });
  $('#fileInput').addEventListener('change', (event) => { uploadFiles([...event.target.files]); event.target.value = ''; });
  const composerWrap = document.querySelector('.composer-wrap');
  if (composerWrap) {
    composerWrap.addEventListener('dragover', (event) => {
      const types = event.dataTransfer?.types || [];
      const hasFiles = types.includes('Files');
      const hasPath = types.includes('application/x-naiba-file-path');
      if (!hasFiles && !hasPath) return;
      event.preventDefault();
      composerWrap.classList.add('dragover');
    });
    composerWrap.addEventListener('dragleave', (event) => {
      if (event.relatedTarget && composerWrap.contains(event.relatedTarget)) return;
      composerWrap.classList.remove('dragover');
    });
    composerWrap.addEventListener('drop', (event) => {
      const droppedPath = event.dataTransfer?.getData('application/x-naiba-file-path')
        || event.dataTransfer?.getData('text/uri-list');
      if (!event.dataTransfer?.files?.length && !droppedPath) return;
      event.preventDefault();
      composerWrap.classList.remove('dragover');
      if (event.dataTransfer.files?.length) uploadFiles([...event.dataTransfer.files]);
      else {
        const path = String(droppedPath).split('\n').find((item) => item && !item.startsWith('#')) || '';
        if (path) {
          const name = path.split(/[\\/]/).pop() || 'image';
          state.pendingFiles.push({ name, path, size: 0 });
          renderPendingFiles();
        }
      }
    });
  }
  // 记录用户是否停留在底部：流式输出时只有跟随底部才自动滚动，上滑阅读则不抢滚动。
  $('#messages').addEventListener('scroll', () => {
    setStickToBottom(isNearBottom());
  }, { passive: true });
  $('#messages').addEventListener('dragstart', (event) => {
    // 缩略图（带 data-large-url）由全局 dragstart 统一以"大图 URL"拖动，这里跳过避免重复设置。
    if (event.target.closest?.('[data-large-url]')) return;
    const image = event.target.closest?.('.attachment-image img, .media-image-link img');
    if (!image || !event.dataTransfer) return;
    const source = image.closest('a')?.href || image.src;
    event.dataTransfer.effectAllowed = 'copy';
    event.dataTransfer.setData('text/uri-list', source);
    event.dataTransfer.setData('text/plain', source);
    const cached = draggedFileCache.get(source);
    if (cached && event.dataTransfer.items?.add) {
      try { event.dataTransfer.items.add(cached); } catch (_error) { /* browser may reject cross-origin items */ }
    }
    if (cached) {
      try { event.dataTransfer.setData('DownloadURL', `${cached.type || 'application/octet-stream'}:${cached.name}:${source}`); } catch (_error) { /* optional Chrome hint */ }
    }
    const path = decodeURIComponent(new URL(source, location.href).searchParams.get('path') || '');
    if (path) event.dataTransfer.setData('application/x-naiba-file-path', path);
  });
  $('#deepReasoningButton').addEventListener('click', toggleDeepReasoning);
  $('#reasoningMenu')?.addEventListener('click', async (event) => {
    const effort = event.target.closest?.('[data-reasoning-effort]')?.dataset.reasoningEffort;
    if (!effort || !state.conversationId || state.chatRunId || state.abortController) return;
    const previous = state.reasoningEffort || 'off';
    state.reasoningEffort = effort;
    state.deepReasoningEnabled = effort !== 'off';
    updateDeepReasoningButton();
    try {
      await api(`/api/conversations/${state.conversationId}/settings`, {
        method: 'POST', body: { reasoning_effort: effort, deep_reasoning_enabled: state.deepReasoningEnabled },
      });
      $('#reasoningMenu').hidden = true;
      toast(effort === 'auto' ? '思考强度：跟随 API（自动）' : `思考强度：${effort}`);
    } catch (error) {
      state.reasoningEffort = previous;
      state.deepReasoningEnabled = previous !== 'off';
      updateDeepReasoningButton();
      toast(`思考设置保存失败：${error.message}`);
    }
  });
  $('#contextUsageButton').addEventListener('click', toggleContextUsagePopover);
  $('#contextUsagePopover').addEventListener('click', (event) => event.stopPropagation());
  window.addEventListener('resize', positionContextUsagePopover);
  window.addEventListener('scroll', positionContextUsagePopover, true);
  document.addEventListener('click', closeContextUsagePopover);
  $('#messageInput').addEventListener('paste', handlePasteImage);
  $('#saveVision').addEventListener('click', saveVisionSettings);
  $('#testVision').addEventListener('click', testVisionConnection);
  $('#visionProvider').addEventListener('change', () => saveVisionSettings({ quiet: true }));
  $('#addVisionProvider').addEventListener('click', openVisionProviderForm);
  $('#deleteVisionProvider').addEventListener('click', deleteVisionProvider);
  $('#saveSearch').addEventListener('click', saveSearchSettings);
  $('#testSearch').addEventListener('click', testSearchConnection);
  $('#searchProfileSelect').addEventListener('change', (event) => {
    const profile = searchProfiles().find((item) => item.id === event.target.value) || {};
    renderSearchProfileFields(profile);
    $('#deleteSearchProfile').disabled = !event.target.value;
    if (event.target.value) {
      const profiles = searchProfiles();
      persistSearchProfiles(profiles, event.target.value, true).catch((error) => toast(`搜索 API 切换失败：${error.message}`));
    }
  });
  $('#addSearchProfile').addEventListener('click', addSearchProfile);
  $('#deleteSearchProfile').addEventListener('click', deleteSearchProfile);
  $('#pendingFiles').addEventListener('click', (event) => {
    const button = event.target.closest('[data-remove-file]');
    if (!button) return;
    const index = Number(button.dataset.removeFile);
    const [chip] = state.pendingFiles.splice(index, 1);
    // 上传中 → 中止 XHR；已完成但未发送 → 删除宿主缓存文件（未被引用时）。
    if (chip?.cancel) chip.cancel();
    if (chip?.path && !chip.uploading) {
      api('/api/uploads/delete', { method: 'POST', body: { path: chip.path } }).catch(() => { /* 有引用/删除失败时保留文件，由清理机制回收 */ });
    }
    renderPendingFiles();
  });

  $('#messages').addEventListener('click', async (event) => {
    const codeCopyButton = event.target.closest('[data-copy-code]');
    if (codeCopyButton) {
      const code = codeCopyButton.closest('.code-block')?.querySelector('code');
      if (!code) return;
      try {
        await copyText(code.textContent);
        codeCopyButton.textContent = '已复制';
        clearTimeout(codeCopyButton.copyResetTimer);
        codeCopyButton.copyResetTimer = setTimeout(() => {
          if (codeCopyButton.isConnected) codeCopyButton.textContent = '复制';
        }, 1500);
        toast('已复制代码');
      } catch (error) {
        toast(`复制失败：${error.message}`);
      }
      return;
    }
    const copyButton = event.target.closest('[data-copy-message]');
    if (copyButton) {
      const text = copyButton.closest('.message-body').querySelector('.answer-content')?.textContent || '';
      try {
        await copyText(text.trim());
        toast('已复制回复');
      } catch (error) {
        toast(`复制失败：${error.message}`);
      }
      return;
    }
    const openFileButton = event.target.closest('[data-open-file]');
    if (openFileButton) {
      openFilePanel(openFileButton.dataset.openFile);
      return;
    }
    const branchButton = event.target.closest('[data-branch-message]');
    if (branchButton) {
      branchMessage(branchButton.closest('.message-row'));
      return;
    }
    const editButton = event.target.closest('[data-edit-message]');
    if (editButton) {
      startEditMessage(editButton.closest('.message-row'));
      return;
    }
  });
  $$('.starter-grid button').forEach((button) => button.addEventListener('click', () => {
    if (button.dataset.installSkill) startSkillInstall();
    else if (button.dataset.editSkill) startSkillEdit();
    else if (button.id === 'starterAddBtn') openStarterPromptDialog();
    else if (button.dataset.prompt != null) sendMessage(button.dataset.prompt);
  }));
  $('#saveStarterPrompt').addEventListener('click', saveStarterPrompt);
  $('#copyAddress').addEventListener('click', async () => {
    if (!state.bootstrap?.lan_enabled || !state.bootstrap?.lan_url) return;
    try {
      await copyText(state.bootstrap.lan_url);
      toast('手机访问地址已复制');
    } catch (error) {
      toast(`复制失败：${error.message}`);
    }
  });
  $('#enableLanAccess').addEventListener('click', enableLanAccess);
  $('#openSidebar').addEventListener('click', openSidebar);
  $('#closeSidebar').addEventListener('click', closeSidebar);
  $('#sidebarBackdrop').addEventListener('click', closeSidebar);
  $('#sidebarWorkspaceTree').addEventListener('click', onSidebarTreeClick);
  // 侧栏虚拟化：滚动时按窗口重绘可视行
  $('#sidebarWorkspaceTree').addEventListener('scroll', () => {
    if (sidebarScrollRaf) return;
    setSidebarScrollRaf(requestAnimationFrame(() => {
      setSidebarScrollRaf(0);
      const tree = $('#sidebarWorkspaceTree');
      if (tree && sidebarRowCache.length) renderSidebarWindow(tree.scrollTop);
    }));
  }, { passive: true });
  // 右侧文件面板：标签页 / 正文操作 / 关闭 / Esc / 窗口宽度
  $('#fileTabs').addEventListener('click', (event) => {
    const closeBtn = event.target.closest('[data-file-tab-close]');
    if (closeBtn) { removeFileTab(closeBtn.dataset.fileTabClose); return; }
    const tabEl = event.target.closest('[data-file-tab]');
    if (tabEl) activateFileTab(tabEl.dataset.fileTab);
  });
  $('#filePanelBody').addEventListener('click', async (event) => {
    if (event.target.closest('[data-file-edit]')) { startFileEdit(filePanelState.activeKey); return; }
    if (event.target.closest('[data-file-edit-cancel]')) { cancelFileEdit(filePanelState.activeKey); return; }
    if (event.target.closest('[data-file-save]')) { saveFileTab(filePanelState.activeKey); return; }
    const codeButton = event.target.closest('[data-copy-code]');
    if (codeButton) {
      const code = codeButton.closest('.code-block')?.querySelector('code');
      if (!code) return;
      try {
        await copyText(code.textContent);
        codeButton.textContent = '已复制';
        clearTimeout(codeButton.copyResetTimer);
        codeButton.copyResetTimer = setTimeout(() => {
          if (codeButton.isConnected) codeButton.textContent = '复制';
        }, 1500);
        toast('已复制代码');
      } catch (error) {
        toast(`复制失败：${error.message}`);
      }
      return;
    }
    const previewImage = event.target.closest('.file-image-wrap img[data-large-url]');
    if (previewImage) openImageLightbox(previewImage.dataset.largeUrl);
  });
  const closeFilePanelButton = $('#closeFilePanel');
  if (closeFilePanelButton) closeFilePanelButton.addEventListener('click', () => closeFilePanel());
  const collapseSidebarButton = $('#collapseSidebar');
  if (collapseSidebarButton) collapseSidebarButton.addEventListener('click', () => setLeftSidebarCollapsed(true));
  const expandSidebarButton = $('#expandSidebar');
  if (expandSidebarButton) expandSidebarButton.addEventListener('click', () => setLeftSidebarCollapsed(false));
  const openFileTabsButton = $('#openFileTabs');
  if (openFileTabsButton) openFileTabsButton.addEventListener('click', reopenFilePanel);
  window.addEventListener('resize', () => {
    if (filePanelUsable()) {
      if (filePanelState.open) applyFilePanelOpenClass();
    } else if (filePanelState.open) {
      closeFilePanel(); // 窄屏收起右侧栏；保留标签，回到宽屏可用顶栏「文件 N」重开
    }
    if (!sidebarDesktop()) $('#appShell')?.classList.remove('sidebar-collapsed');
    updateFileTabsButton();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !filePanelState.open || !filePanelUsable()) return;
    if (document.querySelector('dialog[open]')) return;
    const tab = activeFileTab();
    if (tab && tab.editing) { event.preventDefault(); cancelFileEdit(tab.key); return; }
    closeFilePanel();
  });
  // 侧栏宽度可调：拖动 resizer，限制在 [170, 窗口30%]，并持久化
  const sidebarResizer = $('#sidebarResizer');
  if (sidebarResizer) {
    const clampSidebarW = (w) => Math.max(170, Math.min(Math.max(170, window.innerWidth * 0.3), w));
    sidebarResizer.addEventListener('mousedown', (event) => {
      event.preventDefault();
      sidebarResizer.classList.add('dragging');
      const shellLeft = (document.querySelector('.app-shell')?.getBoundingClientRect().left || 0);
      const onMove = (e) => {
        document.documentElement.style.setProperty('--sidebar-w', clampSidebarW(e.clientX - shellLeft) + 'px');
      };
      const onUp = () => {
        sidebarResizer.classList.remove('dragging');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        const w = document.documentElement.style.getPropertyValue('--sidebar-w');
        localStorage.setItem('naibaChatSidebarW', w);
        renderSidebar();
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }
  const filePanelResizer = $('#filePanelResizer');
  if (filePanelResizer) filePanelResizer.addEventListener('mousedown', (event) => {
    if (!filePanelState.open) return; event.preventDefault(); filePanelResizer.classList.add('dragging');
    const move = (e) => { const w = Math.max(280, Math.min(Math.floor(window.innerWidth * 0.5), window.innerWidth - e.clientX)); $('#appShell')?.style.setProperty('--file-panel-w', `${w}px`); };
    const up = () => { filePanelResizer.classList.remove('dragging'); document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up); const w = parseFloat(getComputedStyle($('#appShell')).getPropertyValue('--file-panel-w')); if (Number.isFinite(w)) localStorage.setItem('naibaChatFilePanelW', String(Math.round(w))); };
    document.addEventListener('mousemove', move); document.addEventListener('mouseup', up);
  });
  // 窗口大小变化时，把侧栏宽度压回窗口 30% 上限，并重绘虚拟列表
  const recalcSidebarW = () => {
    const cur = parseFloat(document.documentElement.style.getPropertyValue('--sidebar-w') || 0) || 272;
    const maxW = Math.max(170, window.innerWidth * 0.3);
    if (cur > maxW) {
      document.documentElement.style.setProperty('--sidebar-w', maxW + 'px');
      localStorage.setItem('naibaChatSidebarW', maxW + 'px');
    }
    renderSidebar();
    if (filePanelState.open) applyFilePanelOpenClass();
  };
  window.addEventListener('resize', recalcSidebarW);
  $('#addWorkspace').addEventListener('click', createWorkspace);
  $('#workspaceSort').addEventListener('click', () => {
    state.workspaceSort = state.workspaceSort === 'updated' ? 'name' : 'updated';
    renderSidebar();
    toast(state.workspaceSort === 'name' ? '已按名称排序' : '已按时间排序');
  });
  $('#workspaceSearch').addEventListener('click', () => {
    const input = $('#workspaceSearchInput');
    if (!input) return;
    input.hidden = !input.hidden;
    if (!input.hidden) input.focus();
    else { input.value = ''; state.workspaceSearch = ''; renderSidebar(); }
  });
  $('#workspaceSearchInput').addEventListener('input', (event) => {
    state.workspaceSearch = event.target.value;
    renderSidebar();
  });
  $('#composerWorkspaceSelect').addEventListener('change', onComposerWorkspaceChange);
  $('#saveWorkspace').addEventListener('click', saveWorkspaceSettings);
  $('#browseWorkspace')?.addEventListener('click', () => pickWorkspace('workspaceDialogInput'));
  $('#browseWorkspaceSettings')?.addEventListener('click', () => pickWorkspace('workspaceDir'));
  $('#workspaceRefresh')?.addEventListener('click', () => loadWorkspaceTree(state.workspaceBrowsePath || ''));
  $('#workspaceUp')?.addEventListener('click', () => {
    const current = String(state.workspaceBrowsePath || '');
    const parent = current.replace(/[\\/][^\\/]+[\\/]?$/, '');
    if (parent && parent !== current) loadWorkspaceTree(parent);
  });
  $$('.settings-nav button').forEach((button) => button.addEventListener('click', () => switchSettingsTab(button.dataset.settingsTab)));
  $('#addProvider').addEventListener('click', addProvider);
  $('#providerSelect').addEventListener('change', (event) => {
    const provider = state.bootstrap.providers.find((item) => item.id === event.target.value);
    showProviderForm(provider || {});
  });
  $$('[data-provider-kind]').forEach((button) => button.addEventListener('click', () => {
    if (state.providerEditing || state.providerKindTab === button.dataset.providerKind) return;
    state.providerKindTab = button.dataset.providerKind;
    renderProviders();
  }));
  $('#providerForm').addEventListener('submit', saveProvider);
  $('#editProvider').addEventListener('click', editProvider);
  $('#cancelProvider').addEventListener('click', cancelProviderEdit);
  $('#testProvider').addEventListener('click', testProvider);
  $('#loadProviderModels').addEventListener('click', () => loadProviderModels());
  $('#unloadProviderModel').addEventListener('click', unloadConfiguredProviderModel);
  $('#providerFormat').addEventListener('change', () => {
    syncProviderKindOptions();
    updateProviderFormatGuide();
    updateProviderContextField();
  });
  $('#providerModel').addEventListener('change', () => {
    toggleCustomModel();
    applyProviderModelCapabilities();
  });
  $('#providerSupportsImages').addEventListener('change', updateProviderVisionHint);
  $('#toggleProviderKey').addEventListener('click', toggleProviderKey);
  $('#providerApiKey').addEventListener('input', (event) => {
    if (event.target.value) $('#providerKeyStatus').textContent = '待保存';
  });
  $('#deleteProvider').addEventListener('click', async () => {
    const providerId = $('#providerId').value;
    if (!providerId) return;
    const provider = state.bootstrap.providers.find((item) => item.id === providerId);
    if (!confirm(`删除供应商"${provider?.name || ''}"？`)) return;
    await api(`/api/providers/${encodeURIComponent(providerId)}`, { method: 'DELETE' });
    const data = await api('/api/bootstrap');
    state.bootstrap = { ...state.bootstrap, ...data };
    $('#providerId').value = '';
    renderProviders();
    populateModels();
    populateVisionSettings();
    toast('供应商已删除');
  });
  $('#addAgent').addEventListener('click', () => showAgentForm(null));
  $('#agentList').addEventListener('click', (event) => {
    const editButton = event.target.closest('[data-agent-edit]');
    if (editButton) {
      const agent = (state.bootstrap?.agents || []).find((item) => item.id === editButton.dataset.agentEdit);
      showAgentForm(agent || {});
      return;
    }
    const deleteButton = event.target.closest('[data-agent-delete]');
    if (deleteButton) deleteAgent(deleteButton.dataset.agentDelete);
  });
  $('#agentSkillList').addEventListener('change', (event) => {
    if (event.target.type !== 'checkbox') return;
    state.agentFormSkillIds = event.target.checked
      ? [...new Set([...state.agentFormSkillIds, event.target.value])]
      : state.agentFormSkillIds.filter((id) => id !== event.target.value);
  });
  $('#cancelAgent').addEventListener('click', hideAgentForm);
  $('#saveAgentForm').addEventListener('click', saveAgentForm);
  $('#toggleAllToolGroups')?.addEventListener('click', toggleAllToolGroups);
  // 工具预设下拉框 / 自定义模板（芯片行事件委托，✕ 删除、点芯片复刻）。
  $('#agentToolPresetSelect')?.addEventListener('change', (event) => onToolPresetSelect(event.target.value));
  $('#agentToolTemplateSave')?.addEventListener('click', () => {
    const template = collectTemplateFromCurrent();
    toast(`已存为模板「${template.name}」，点上方模板芯片即可一键复刻`);
  });
  $('#agentToolTemplateName')?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      $('#agentToolTemplateSave')?.click();
    }
  });
  $('#agentToolTemplates')?.addEventListener('click', (event) => {
    const del = event.target.closest('[data-template-del]');
    if (del) {
      deleteToolTemplate(del.dataset.templateDel);
      return;
    }
    const apply = event.target.closest('[data-template-apply]');
    if (apply) applyToolTemplate(apply.dataset.templateApply);
  });
  $('#saveRuntime').addEventListener('click', saveRuntimeSettings);
  document.querySelectorAll('input[name="proxyMode"]').forEach((radio) => {
    radio.addEventListener('change', renderProxyRows);
  });
  $('#cleanImageCache')?.addEventListener('click', cleanImageCache);
  $('#refreshStorageStats')?.addEventListener('click', loadStorageStats);
  $('#compactDatabase')?.addEventListener('click', compactDatabase);
  $('#imageUploadOriginal')?.addEventListener('change', renderImageCompressRow);
  $('#imageLightboxClose')?.addEventListener('click', closeImageLightbox);
  $('#imageLightbox')?.addEventListener('click', (event) => { if (event.target === event.currentTarget) closeImageLightbox(); });
  $('#saveToken').addEventListener('click', saveAccessToken);
  $('#checkUpdate').addEventListener('click', checkUpdate);
  $('#installUpdate').addEventListener('click', installUpdate);
  $('#updateVersionSelect').addEventListener('change', () => renderUpdateStatus(state.bootstrap.update || {}));
  $('#openSkillImport').addEventListener('click', () => {
    setSkillImportStatus('');
    $('#skillImportDialog').showModal();
  });
  $('#skillImportFolder').addEventListener('click', () => $('#skillImportFolderInput').click());
  $('#skillImportFiles').addEventListener('click', () => $('#skillImportFileInput').click());
  $('#skillImportFolderInput').addEventListener('change', (event) => { skillImportFolderFiles(event.target.files); event.target.value = ''; });
  $('#skillImportFileInput').addEventListener('change', (event) => {
    const file = event.target.files[0];
    event.target.value = '';
    if (!file) return;
    const lower = file.name.toLowerCase();
    if (lower.endsWith('.zip')) skillImportZipFile(file);
    else if (lower.endsWith('.md')) skillImportMdFile(file);
    else setSkillImportStatus('仅支持 .zip 或 .md 文件', 'error');
  });
  const dropZone = $('#skillDropZone');
  if (dropZone) {
    dropZone.addEventListener('dragover', (event) => { event.preventDefault(); dropZone.classList.add('dragover'); });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
    dropZone.addEventListener('drop', (event) => {
      event.preventDefault();
      dropZone.classList.remove('dragover');
      const dt = event.dataTransfer;
      if (!dt) return;
      const folderEntry = [...dt.items].find((it) => it.kind === 'file' && it.webkitGetAsEntry && it.webkitGetAsEntry() && it.webkitGetAsEntry().isDirectory);
      if (folderEntry && folderEntry.webkitGetAsEntry) {
        readDirectoryEntry(folderEntry.webkitGetAsEntry()).then((files) => skillImportFolderFiles(files));
        return;
      }
      const files = [...dt.files];
      const zip = files.find((f) => f.name.toLowerCase().endsWith('.zip'));
      const md = files.find((f) => f.name.toLowerCase().endsWith('.md'));
      if (files.length === 1 && (zip || md)) {
        if (zip) skillImportZipFile(zip); else skillImportMdFile(md);
      } else if (files.length) {
        skillImportFolderFiles(files);
      }
    });
  }
  $('#backupData').addEventListener('click', backupData);
  $('#applyDataDir').addEventListener('click', applyAndMigrateDataDir);
}

export function setSkillImportStatus(message, kind) {
  const el = $('#skillImportStatus');
  if (!el) return;
  el.textContent = message;
  el.className = 'skill-import-status' + (kind ? ' ' + kind : '');
}

export function hasSkillFrontmatter(text) {
  const m = text.match(/^---\s*\n([\s\S]*?)\n---/);
  if (!m) return false;
  const block = m[1];
  return /^\s*name\s*:/im.test(block) && /^\s*description\s*:/im.test(block);
}

export async function skillImportFolderFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  if (files.length > 2000) { setSkillImportStatus('文件夹内文件数量过多（超过 2000）', 'error'); return; }
  const totalSize = files.reduce((sum, f) => sum + f.size, 0);
  if (totalSize > 80 * 1024 * 1024) { setSkillImportStatus('文件夹总大小不能超过 80 MB', 'error'); return; }
  setSkillImportStatus(`正在上传 ${files.length} 个文件…`);
  try {
    const payload = [];
    for (const file of files) {
      const data = await readAsDataUrl(file);
      payload.push({ path: file._relPath || file.webkitRelativePath || file.name, data });
    }
    const result = await api('/api/skills/install_folder', { method: 'POST', body: { files: payload } });
    if (result.configured) state.skillDirs = result.configured;
    renderInstalledSkills(result.skills || []);
    renderHiddenSkills(result.hidden_skills || []);
    const unhiddenTag = (result.unhidden && result.unhidden.length) ? ` · 已安装并取消隐藏` : '';
    setSkillImportStatus(`已安装 ${result.files} 个文件到 ${result.dir}${unhiddenTag}`, 'ok');
  } catch (error) {
    setSkillImportStatus(`安装失败：${error.message}`, 'error');
  }
}

export async function skillImportZipFile(file) {
  setSkillImportStatus(`正在上传 ${file.name}…`);
  try {
    const data = await readAsDataUrl(file);
    const result = await api('/api/skills/install', { method: 'POST', body: { name: file.name, data } });
    if (result.configured) state.skillDirs = result.configured;
    renderInstalledSkills(result.skills || []);
    renderHiddenSkills(result.hidden_skills || []);
    const unhiddenTag = (result.unhidden && result.unhidden.length) ? ' · 已安装并取消隐藏' : '';
    setSkillImportStatus(`已安装到 ${result.dir}${unhiddenTag}`, 'ok');
  } catch (error) {
    setSkillImportStatus(`安装失败：${error.message}`, 'error');
  }
}

export async function skillImportMdFile(file) {
  setSkillImportStatus(`正在读取 ${file.name}…`);
  try {
    const text = await file.text();
    if (!hasSkillFrontmatter(text)) {
      setSkillImportStatus('该 .md 缺少 name + description 的 YAML 头，无法作为 Skill 安装。', 'error');
      return;
    }
    const data = await readAsDataUrl(file);
    const result = await api('/api/skills/install_folder', { method: 'POST', body: { files: [{ path: 'SKILL.md', data }] } });
    if (result.configured) state.skillDirs = result.configured;
    renderInstalledSkills(result.skills || []);
    renderHiddenSkills(result.hidden_skills || []);
    const unhiddenTag = (result.unhidden && result.unhidden.length) ? ' · 已安装并取消隐藏' : '';
    setSkillImportStatus(`已作为 Skill 安装到 ${result.dir}${unhiddenTag}`, 'ok');
  } catch (error) {
    setSkillImportStatus(`安装失败：${error.message}`, 'error');
  }
}

export function readDirectoryEntry(entry) {
  const files = [];
  const walk = (ent, prefix) => new Promise((res) => {
    if (ent.isFile) {
      ent.file((file) => { file._relPath = prefix + file.name; files.push(file); res(); });
    } else if (ent.isDirectory) {
      const reader = ent.createReader();
      const readBatch = () => reader.readEntries((batch) => {
        if (!batch.length) { res(); return; }
        Promise.all(batch.map((b) => walk(b, prefix + ent.name + '/'))).then(readBatch);
      });
      readBatch();
    } else {
      res();
    }
  });
  return walk(entry, '').then(() => files);
}

export async function loadInstalledSkills(showToast) {
  try {
    const data = await api('/api/skills/scan', { method: 'POST', body: {} });
    if (data.configured) state.skillDirs = data.configured;
    renderInstalledSkills(data.skills || []);
    renderHiddenSkills(data.hidden_skills || []);
    if (showToast) toast('已重新扫描 Skill');
  } catch (error) {
    toast(`扫描失败：${error.message}`);
  }
}

export let lastInstalledSkills = [];

export function isSkillBuiltin(skill) {
  return skill.source === 'builtin';
}

export function renderInstalledSkills(skills) {
  lastInstalledSkills = skills || [];
  if (state.bootstrap) {
    state.bootstrap.skills = lastInstalledSkills;
    const available = new Set(lastInstalledSkills.map((skill) => skill.id));
    state.selectedSkills = state.selectedSkills.filter((id) => available.has(id));
    localStorage.setItem('naibaChatSkillIds', JSON.stringify(state.selectedSkills));
    renderSkills($('#skillSearch')?.value || '');
    renderAgentSkillPicker();
  }
  const list = $('#installedSkillList');
  list.innerHTML = '';
  $('#installedSkillCount').textContent = String(skills.length);
  if (!skills.length) {
    list.innerHTML = '<div class="connection-item"><small>未加载任何 Skill</small></div>';
    return;
  }
  skills.forEach((skill) => {
    const item = document.createElement('div');
    item.className = 'skill-item connection-item';
    const info = document.createElement('div');
    const b = document.createElement('b');
    b.textContent = skill.name;
    const small = document.createElement('small');
    small.textContent = skill.description || skill.path;
    small.className = 'desc';
    info.append(b, small);
    item.append(info);
    if (skill.source === 'builtin' || skill.source === 'external') {
      const tag = document.createElement('span');
      tag.className = 'skill-tag';
      tag.textContent = skill.source === 'builtin' ? '内置' : '外部';
      item.append(tag);
    }
    const del = document.createElement('button');
    del.className = 'skill-delete';
    del.type = 'button';
    del.title = skill.source === 'managed' ? '删除 Skill（移动到回收目录）' : '隐藏 Skill（可恢复原文件）';
    del.textContent = '删除';
    del.addEventListener('click', () => deleteInstalledSkill(skill));
    item.append(del);
    list.append(item);
  });
}

export async function deleteInstalledSkill(skill) {
  const refs = (state.bootstrap.agents || [])
    .filter((a) => (a.skill_ids || []).map(String).includes(String(skill.id)))
    .map((a) => a.name);
  const dir = skill.root || skill.path || '';
  const msg = `删除 Skill「${skill.name}」？\n目录：${dir}\n${refs.length ? `被以下 Agent 引用：${refs.join('、')}（引用将被移除）` : '未被任何 Agent 引用'}`;
  if (!confirm(msg)) return;
  try {
    const result = await api(`/api/skills/${encodeURIComponent(skill.id)}`, { method: 'DELETE' });
    state.selectedSkills = state.selectedSkills.filter((id) => id !== skill.id);
    localStorage.setItem('naibaChatSkillIds', JSON.stringify(state.selectedSkills));
    if (result.skills) state.bootstrap.skills = result.skills;
    if (result.agents) state.bootstrap.agents = result.agents;
    renderInstalledSkills(result.skills || lastInstalledSkills.filter((s) => s.id !== skill.id));
    renderSkills($('#skillSearch')?.value || '');
    renderHiddenSkills(result.hidden_skills || []);
    toast(result.hidden ? '已删除 Skill（原文件保留并隐藏）' : `已删除，回收位置：${result.recycled_to || '未知'}`);
  } catch (error) {
    toast(`删除失败：${error.message}`);
  }
}

export function renderHiddenSkills(hiddenSkills) {
  const list = $('#hiddenSkillList');
  if (!list) return;
  const items = Array.isArray(hiddenSkills) ? hiddenSkills : [];
  const count = $('#hiddenSkillCount');
  if (count) count.textContent = String(items.length);
  list.innerHTML = '';
  if (!items.length) {
    list.innerHTML = '<small class="hint">无已隐藏的 Skill</small>';
    return;
  }
  items.forEach((skill) => {
    const item = document.createElement('div');
    item.className = 'skill-item connection-item';
    const info = document.createElement('div');
    const b = document.createElement('b');
    b.textContent = skill.name;
    const small = document.createElement('small');
    small.textContent = skill.description || skill.path;
    small.className = 'desc';
    info.append(b, small);
    item.append(info);
    const unhide = document.createElement('button');
    unhide.className = 'skill-delete';
    unhide.type = 'button';
    unhide.title = '取消隐藏，恢复为可用的 Skill';
    unhide.textContent = '取消隐藏';
    unhide.addEventListener('click', () => unhideSkill(skill.id));
    item.append(unhide);
    list.append(item);
  });
}

export async function unhideSkill(skillId) {
  if (!skillId) return;
  try {
    const result = await api('/api/skills/unhide', { method: 'POST', body: { skill_id: skillId } });
    if (state.bootstrap && result.skills) state.bootstrap.skills = result.skills;
    renderInstalledSkills(result.skills || []);
    renderHiddenSkills(result.hidden_skills || []);
    renderSkills($('#skillSearch')?.value || '');
    toast('已取消隐藏该 Skill');
  } catch (error) {
    toast(`取消隐藏失败：${error.message}`);
  }
}

export function renderDataMigration() {
  const m = state.bootstrap?.data_migration || {};
  const configured = m.configured_data_dir || state.bootstrap?.settings?.resolved_data_dir || m.data_dir || '';
  if ($('#dataDir') && document.activeElement !== $('#dataDir')) $('#dataDir').value = configured;
  $('#migrationDbVersion').textContent = m.db_version != null ? String(m.db_version) : '-';
  $('#migrationDataDir').textContent = m.restart_required
    ? `${m.data_dir || '-'}（重启后切换到 ${configured}）`
    : (configured || m.data_dir || '-');
  const skillsDirs = Array.isArray(m.resolved_skills_dirs) ? m.resolved_skills_dirs : [];
  $('#migrationSkillsDir').textContent = skillsDirs.length ? skillsDirs.join('；') : '-';
  $('#migrationHealthy').textContent = m.healthy === true ? '✓ 健康' : (m.healthy === false ? '✗ 异常' : '-');
  $('#migrationApplied').textContent = Array.isArray(m.applied_versions)
    ? (m.applied_versions.length ? m.applied_versions.join(', ') : '无')
    : '-';
  $('#migrationBackup').textContent = m.backup_location || '-';
}

export async function applyAndMigrateDataDir() {
  const value = $('#dataDir')?.value.trim() || '';
  if (!value) {
    $('#migrationMessage').textContent = '请先填写目标数据目录';
    return;
  }
  if (!confirm('将当前数据库、上传文件与 Skills 目录一并复制到新目录，并完成结构迁移。完成后需要重启，继续？')) return;
  const btn = $('#applyDataDir');
  const previousText = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '迁移中…'; }
  try {
    const result = await api('/api/migration/move-data', { method: 'POST', body: { data_dir: value } });
    if (!result.ok) {
      $('#migrationMessage').textContent = `迁移失败：${result.error || '未知错误'}`;
      return;
    }
    const target = result.target_data_dir || value;
    const skills = result.target_skills_dir || (result.resolved_skills_dirs && result.resolved_skills_dirs[0]) || '';
    $('#migrationMessage').textContent = `数据库与 Skills 已复制到新目录（数据：${target}${skills ? `；Skills：${skills}` : ''}），请完全退出并重新启动 NaibaChat 生效。`;
    state.bootstrap.data_migration = result;
    renderDataMigration();
  } catch (error) {
    $('#migrationMessage').textContent = `迁移失败：${error.message}`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = previousText; }
  }
}

export async function loadDataMigrationHealth() {
  try {
    const m = await api('/api/migration/health');
    state.bootstrap.data_migration = m;
    renderDataMigration();
  } catch (error) {
    toast(`读取迁移状态失败：${error.message}`);
  }
}

export async function backupData() {
  try {
    const r = await api('/api/migration/backup', { method: 'POST', body: {} });
    if (r.error) { $('#migrationMessage').textContent = '备份失败：' + r.error; return; }
    const files = Array.isArray(r.files) ? `（${r.files.length} 个文件）` : '';
    $('#migrationMessage').textContent = `已备份到：${r.backup_dir || ''}${files}`;
    if (r.backup_location) { state.bootstrap.data_migration = state.bootstrap.data_migration || {}; state.bootstrap.data_migration.backup_location = r.backup_location; }
    renderDataMigration();
  } catch (error) {
    $('#migrationMessage').textContent = '备份失败：' + error.message;
  }
}

export function switchSettingsTab(name) {
  $$('.settings-nav button').forEach((button) => button.classList.toggle('active', button.dataset.settingsTab === name));
  $$('[data-settings-panel]').forEach((panel) => { panel.hidden = panel.dataset.settingsPanel !== name; });
  if (name === 'agent') renderAgentManager();
  if (name === 'skills') loadInstalledSkills(false);
  if (name === 'conversation-prompts') loadConversationPromptPresets();
  if (name === 'connections') loadMcpServers();
  if (name === 'datamigration') loadDataMigrationHealth();
  if (name === 'updates') api('/api/update').then((status) => {
    state.bootstrap.update = status;
    renderUpdateStatus(status);
  }).catch((error) => toast(`读取更新状态失败：${error.message}`));
}

bindEvents();
initialize();
// 首屏输入框为空：发送按钮从加载起就是灰暗的不可发送态。
updateSendButtonState();
restoreLeftSidebarCollapse();
updateFileTabsButton();
