// ============================================================
// 13-skill-refs.js —— 拆分自 public/app.js 第 6069-6328 行（阶段 5.1 按域拆分，跨文件引用零改动）
// ============================================================

import { $, escapeHtml, state } from "./01-core.js";
import { markdown } from "./02-markdown.js";
export function skillList() { return Array.isArray(state.bootstrap?.skills) ? state.bootstrap.skills : []; }

// 按 /ref 反查 skill：优先 ref，其次 name，忽略大小写。
export function skillByRef(refText) {
  const key = String(refText || '').trim().toLowerCase();
  if (!key) return null;
  return skillList().find((s) => String(s.ref || '').toLowerCase() === key)
    || skillList().find((s) => String(s.name || '').toLowerCase() === key) || null;
}

// 识别文本里所有 <(^|\s)/ref> 且命中了已安装 skill 的引用。
export function tokenizeSkillRefs(text) {
  const matches = [];
  const re = /(^|\s)\/([^\s/#]+)/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    const name = m[2];
    const skill = skillByRef(name);
    if (!skill) continue;
    const slashAt = m.index + m[1].length;
    matches.push({ start: slashAt, end: slashAt + 1 + name.length, text: '/' + name, skill });
  }
  return matches;
}

// 供镜像层/气泡：把文本转成带 <span class="skill-ref"> 高亮的 HTML。
export function highlightSkillRefsHtml(text) {
  const tokens = tokenizeSkillRefs(text);
  if (!tokens.length) return escapeHtml(text);
  let out = ''; let pos = 0;
  for (const tok of tokens) {
    out += escapeHtml(text.slice(pos, tok.start));
    out += `<span class="skill-ref">${escapeHtml(tok.text)}</span>`;
    pos = tok.end;
  }
  out += escapeHtml(text.slice(pos));
  return out;
}

export function renderInputMirror() {
  const mirror = $('#inputMirror');
  const input = $('#messageInput');
  if (!mirror || !input) return;
  const value = input.value;
  // 空内容时用一个零宽字符撑起镜像层；非空时只放原文本（不额外追加零宽字符，避免影响换行）。
  mirror.innerHTML = value ? highlightSkillRefsHtml(value) : '\u200b';
  mirror.scrollTop = input.scrollTop;
}

// 当前光标所在的那个 <(^|\s)/name…> token；无效时返回 null。
export function currentSlashToken(value, cursor) {
  if (!value || cursor == null) return null;
  const isWS = (ch) => ch === undefined || /\s/.test(ch);
  let i = cursor;
  while (i > 0 && !isWS(value[i - 1])) i--;
  if (i >= cursor || value[i] !== '/') return null;
  if (i > 0 && !isWS(value[i - 1])) return null;
  let j = cursor;
  while (j < value.length && !isWS(value[j])) j++;
  if (cursor < i + 1 || cursor > j) return null;
  return { tokenStart: i, tokenEnd: j, typed: value.slice(i + 1, cursor) };
}

export const popupState = { open: false, selectedIndex: 0, items: [], token: null };

export function positionSkillPopup() {
  const popup = $('#skillPopup');
  const input = $('#messageInput');
  if (!popup || !input || popup.hidden) return;
  const rect = input.getBoundingClientRect();
  const ph = popup.offsetHeight;
  let top = rect.top - ph - 6;
  if (top < 8) top = rect.bottom + 6;
  popup.style.left = `${Math.max(8, rect.left)}px`;
  popup.style.width = `${rect.width}px`;
  popup.style.top = `${top}px`;
}

export function showSkillPopup(items, selectedIndex, token) {
  const popup = $('#skillPopup');
  if (!popup) return;
  popupState.open = true; popupState.items = items; popupState.token = token; popupState.selectedIndex = selectedIndex;
  if (!items.length) {
    popup.innerHTML = '<div class="skill-popup-empty">没有匹配的 Skill</div>';
    popup.hidden = false;
    positionSkillPopup();
    return;
  }
  popup.innerHTML = items.map((s, i) => `
    <button type="button" role="option" class="skill-popup-item${i === selectedIndex ? ' selected' : ''}" data-skill-index="${i}">
      <div class="skill-popup-main"><b>${escapeHtml(s.name)}</b><em class="skill-size">${s.char_count ? ('~' + s.char_count) : ''}</em></div>
      <div class="skill-popup-sub"><span class="skill-popup-ref">/${escapeHtml(s.ref || s.name)}</span><small>${escapeHtml(s.description || '')}</small></div>
    </button>`).join('');
  popup.querySelector('.selected')?.scrollIntoView({ block: 'nearest' });
  popup.hidden = false;
  positionSkillPopup();
}

export function hideSkillPopup() {
  popupState.open = false; popupState.items = []; popupState.token = null; popupState.selectedIndex = 0;
  const popup = $('#skillPopup');
  if (popup) popup.hidden = true;
}

export function setSkillPopupSelection(index) {
  popupState.selectedIndex = index;
  const popup = $('#skillPopup');
  popup?.querySelectorAll('[data-skill-index]').forEach((el) => {
    el.classList.toggle('selected', Number(el.dataset.skillIndex) === index);
  });
  popup?.querySelector('.selected')?.scrollIntoView({ block: 'nearest' });
}

export function updateSkillPopup() {
  const input = $('#messageInput');
  if (!input) return hideSkillPopup();
  const token = currentSlashToken(input.value, input.selectionStart);
  if (!token) return hideSkillPopup();
  const query = token.typed.toLowerCase();
  const items = skillList().filter((s) =>
    !query || `${s.ref || ''} ${s.name || ''} ${s.description || ''}`.toLowerCase().includes(query));
  items.sort((a, b) => {
    const ap = String(a.ref || '').toLowerCase().startsWith(query) ? 0 : 1;
    const bp = String(b.ref || '').toLowerCase().startsWith(query) ? 0 : 1;
    return ap - bp || String(a.ref || '').localeCompare(String(b.ref || ''));
  });
  showSkillPopup(items, 0, token);
}

export function moveSkillPopupSelection(delta) {
  if (!popupState.open || !popupState.items.length) return;
  const n = popupState.items.length;
  setSkillPopupSelection((popupState.selectedIndex + delta + n) % n);
}

export function commitSkillSelection(skill) {
  const input = $('#messageInput');
  const value = input.value;
  const cursor = input.selectionStart;
  const token = currentSlashToken(value, cursor);
  if (!token) { hideSkillPopup(); return; }
  const replacement = '/' + (skill.ref || skill.name) + ' ';
  const newValue = value.slice(0, token.tokenStart) + replacement + value.slice(token.tokenEnd);
  const newCursor = token.tokenStart + replacement.length;
  input.value = newValue;
  input.setSelectionRange(newCursor, newCursor);
  resizeTextarea();
  renderInputMirror();
  hideSkillPopup();
  input.focus();
}

// 在光标处插入一个 skill 引用（顶栏点击 / 预填复用）。
export function insertSkillRefAtCursor(skill) {
  const input = $('#messageInput');
  if (!input) return;
  const refText = '/' + (skill.ref || skill.name);
  const cs = input.selectionStart ?? input.value.length;
  const ce = input.selectionEnd ?? input.value.length;
  const before = input.value.slice(0, cs);
  const after = input.value.slice(ce);
  const needsLeading = cs > 0 && !/\s/.test(input.value[cs - 1]);
  const insertion = (needsLeading ? ' ' : '') + refText + ' ';
  input.value = before + insertion + after;
  const newCursor = before.length + insertion.length;
  input.setSelectionRange(newCursor, newCursor);
  resizeTextarea();
  renderInputMirror();
}

// 新会话：把当前 Agent 预设 skill 以 /ref 引用预填到输入框（用户删掉即不引用，统一途径）。
export function prefillPresetSkillsInComposer(conversation) {
  const input = $('#messageInput');
  if (!input) return;
  const agentId = String(conversation?.agent_id || '');
  const agent = (state.bootstrap?.agents || []).find((a) => a.id === agentId);
  const presetIds = new Set((agent?.skill_ids || []).map(String));
  if (!presetIds.size) return;
  const skills = skillList().filter((s) => presetIds.has(String(s.id)));
  if (!skills.length) return;
  input.value = skills.map((s) => '/' + (s.ref || s.name)).join(' ') + ' ';
  resizeTextarea();
  renderInputMirror();
  input.setSelectionRange(input.value.length, input.value.length);
  input.focus();
}

// 切换 Agent 后：把该 Agent 预设 Skill 以 /ref 追加到输入框末尾（已在框内的跳过，避免重复）。
export function appendPresetSkillsToComposer(agentId) {
  const input = $('#messageInput');
  if (!input) return;
  const agent = (state.bootstrap?.agents || []).find((a) => String(a.id) === String(agentId));
  const presetIds = new Set((agent?.skill_ids || []).map(String));
  if (!presetIds.size) return;
  const skills = skillList().filter((s) => presetIds.has(String(s.id)));
  if (!skills.length) return;
  const existing = input.value.trimEnd();
  const have = new Set(tokenizeSkillRefs(existing).map((t) => String(t.skill.id)));
  const refs = skills.filter((s) => !have.has(String(s.id))).map((s) => '/' + (s.ref || s.name));
  if (!refs.length) return;
  input.value = existing ? existing + ' ' + refs.join(' ') : refs.join(' ');
  resizeTextarea();
  renderInputMirror();
  input.setSelectionRange(input.value.length, input.value.length);
  input.focus();
}

// 解析并返回本消息引用的 skill（去重）。
export function parseSkillReferences(text) {
  const seen = new Set();
  const refs = [];
  for (const tok of tokenizeSkillRefs(text)) {
    if (seen.has(tok.skill.id)) continue;
    seen.add(tok.skill.id);
    refs.push(tok);
  }
  return refs;
}

// 把 /ref 引用从消息文本里剥离（发给模型用）；若剥空则保留原文（纯引用调用场景）。
export function stripSkillReferences(text) {
  const tokens = tokenizeSkillRefs(text);
  if (!tokens.length) return text;
  let out = ''; let pos = 0;
  for (const tok of tokens) {
    out += text.slice(pos, tok.start);
    pos = tok.end;
  }
  out += text.slice(pos);
  const cleaned = out.replace(/\s+/g, ' ').trim();
  return cleaned || text;
}

// 用户气泡：优先显示 display_content（含 /ref），并对命中 skill 的引用高亮；保留 markdown。
export function renderUserContent(text) {
  const tokens = tokenizeSkillRefs(text);
  if (!tokens.length) return markdown(text);
  let protectedText = ''; let pos = 0; let idx = 0;
  const mapping = [];
  for (const tok of tokens) {
    protectedText += text.slice(pos, tok.start);
    const ph = `@@SKILLREF${idx++}@@`;
    mapping.push({ ph, text: tok.text });
    protectedText += ph;
    pos = tok.end;
  }
  protectedText += text.slice(pos);
  let html = markdown(protectedText);
  for (const m of mapping) html = html.split(m.ph).join(`<span class="skill-ref">${escapeHtml(m.text)}</span>`);
  return html;
}


export function resizeTextarea() {
  const input = $('#messageInput');
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
}

// ---- 右侧文件面板（消息末尾“修改文件”摘要 → 查看 / 富文本编辑）----
