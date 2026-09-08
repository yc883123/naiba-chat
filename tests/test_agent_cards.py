# -*- coding: utf-8 -*-
"""Agent 设置页（卡片网格 + 点开才弹出的设置弹层）与「下线内置 Agent」的守门。

背景（两条一起做的改动）：
1. 四个内置 Agent 预设（dsh-standard / dsh-code / dsh-minimal / dsh-cordis）按用户要求下线：
   `built_in_agents()` 清单置空，**机制保留**（built_in 标记 + 不可删守卫 + 前端「内置」徽标）；
   用户配置里遗留的旧内置副本由 `_migrate_agent_builtin_flags()` 摘掉标记，变成普通可删 Agent。
2. Agent 管理页改成与 API 供应商页同款：卡片网格（一行最多三张）+ 末尾「新增 Agent」卡片 +
   卡片右上角 × 删除 + 点卡片才弹出顶层 `<dialog id="agentDialog">`（字段/工具集/Skill 选择器
   保持原样）。

关键不变量：
- 内置清单为空 → 不再注入任何内置 Agent，所有 Agent 都可删除，`upsert_agent` 不再打 built_in；
- 遗留副本只摘标记、不动内容（用户改过的名称/提示词/工具集不丢）；
- 卡片容器 `#agentCards`，旧 `#agentList`/`#addAgent`/`.agent-item*` 连绑定一起消失；
- 表单整体搬进 `#agentDialog`，字段 id 与文案逐字不变；
- 网格三列 + 窄屏 2/1 列。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from naiba.config import ConfigStore, built_in_agent_ids, built_in_agents  # noqa: E402

LEGACY_SELECTORS = ("#agentList", "#addAgent", "agent-item", "agent-manager", "data-agent-edit")

FORM_FIELD_IDS = (
    "agentFormId",
    "agentName",
    "agentSystemPromptEdit",
    "agentPromptPresetSelect",
    "importAgentCharacterCard",
    "agentCharacterCardFileInput",
    "pickAgentAvatar",
    "agentAvatarFileInput",
    "agentAvatarPreview",
    "agentSkillList",
    "agentToolPresetState",
    "agentToolCount",
    "toggleAllToolGroups",
    "agentToolPresetSelect",
    "agentToolTemplateName",
    "agentToolTemplateSave",
    "agentToolTemplateRow",
    "agentToolTemplates",
    "agentToolUnknownHint",
    "agentToolScope",
    "agentError",
    "cancelAgent",
    "saveAgentForm",
)


class BuiltInAgentsRetiredTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _store(self, payload=None) -> ConfigStore:
        if payload is not None:
            self.config_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        return ConfigStore(self.config_path)

    def test_built_in_list_is_empty_but_mechanism_kept(self) -> None:
        self.assertEqual(built_in_agents(), [])
        self.assertEqual(built_in_agent_ids(), set())

    def test_no_built_in_agent_is_injected(self) -> None:
        store = self._store({"agents": [{"id": "mine", "name": "我的 Agent", "system_prompt": "", "skill_ids": []}]})
        ids = [agent.get("id") for agent in store.public_agents()]
        self.assertEqual(ids, ["mine"])

    def test_legacy_built_in_flag_is_stripped_but_content_kept(self) -> None:
        store = self._store({
            "agents": [
                {
                    "id": "dsh-standard",
                    "name": "dsh-standard（全能）",
                    "system_prompt": "用户改过的提示词",
                    "skill_ids": ["s1"],
                    "tool_scope": ["read_file"],
                    "built_in": True,
                }
            ]
        })
        agent = store.public_agents()[0]
        self.assertNotIn("built_in", agent, "遗留副本必须变成普通 Agent（否则前端仍隐藏删除按钮）")
        self.assertEqual(agent["name"], "dsh-standard（全能）")
        self.assertEqual(agent["system_prompt"], "用户改过的提示词")
        self.assertEqual(agent["tool_scope"], ["read_file"])
        # 落盘也要清掉，避免下次启动重复迁移。
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("built_in", saved["agents"][0])

    def test_former_built_in_agent_is_deletable(self) -> None:
        store = self._store({
            "agents": [
                {"id": "dsh-code", "name": "dsh-code（编程）", "system_prompt": "", "skill_ids": [], "built_in": True},
                {"id": "keep", "name": "保留", "system_prompt": "", "skill_ids": []},
            ],
            "default_agent_id": "keep",
        })
        self.assertTrue(store.delete_agent("dsh-code"), "内置清单为空后旧内置 id 必须可删")
        self.assertEqual([agent.get("id") for agent in store.public_agents()], ["keep"])

    def test_upsert_agent_never_marks_built_in(self) -> None:
        store = self._store({})
        saved = store.upsert_agent({"id": "dsh-standard", "name": "再来一个", "system_prompt": "", "skill_ids": []})
        self.assertNotIn("built_in", saved)
        self.assertNotIn("built_in", store.public_agents()[0])


class ToolGroupCatalogTests(unittest.TestCase):
    """工具集分类目录：视觉分组必须有说明，且紧跟在「命令执行」之后。"""

    def test_vision_group_follows_command_group_and_has_desc(self) -> None:
        from naiba.config import TOOL_GROUP_INFO

        names = [name for name, _ in TOOL_GROUP_INFO]
        self.assertIn("视觉", names)
        self.assertEqual(
            names.index("视觉"), names.index("命令执行") + 1,
            "视觉必须排在「命令执行」后面",
        )
        self.assertTrue(dict(TOOL_GROUP_INFO)["视觉"].strip(), "视觉分类必须有说明小字")
        # 旧的「视觉（文本模型）/（视觉模型）」分类名随视觉单入口重构早已退役，别再复活。
        self.assertNotIn("视觉（文本模型）", names)
        self.assertNotIn("视觉（视觉模型）", names)

    def test_tool_group_head_renders_title_and_desc_in_one_line(self) -> None:
        settings = (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")
        body = settings[settings.index("const head = document.createElement('div');"):]
        body = body[: body.index("groupEl.append(head)")]
        self.assertIn("head.append(caret, allCb, title, desc, count)", body,
                      "小字说明必须排在标题之后、计数之前（一行呈现）")
        css = (ROOT / "public/styles.css").read_text(encoding="utf-8")
        head_rule = css[css.index(".agent-tool-group-head {"):]
        head_rule = head_rule[: head_rule.index("}")]
        self.assertIn("auto auto max-content minmax(0, 1fr) auto", head_rule)
        desc_rule = css[css.index(".group-desc {"):]
        desc_rule = desc_rule[: desc_rule.index("}")]
        self.assertIn("white-space: nowrap", desc_rule)
        self.assertIn("text-overflow: ellipsis", desc_rule)
        self.assertNotIn("grid-column", desc_rule, "小字不能再独占第二行")


class AgentCardsMarkupTests(unittest.TestCase):
    def _index(self) -> str:
        return (ROOT / "public/index.html").read_text(encoding="utf-8")

    def _settings(self) -> str:
        return (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")

    def _bind(self) -> str:
        return (ROOT / "public/js/15-bind-events.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (ROOT / "public/styles.css").read_text(encoding="utf-8")

    def test_agent_panel_only_renders_cards(self) -> None:
        index = self._index()
        start = index.index('data-settings-panel="agent"')
        end = index.index('data-settings-panel="runtime"')
        panel = index[start:end]
        self.assertIn('id="agentCards"', panel)
        self.assertNotIn('id="agentForm"', panel, "设置表单不应再常驻在设置页里")
        self.assertNotIn('id="addAgent"', panel)

    def test_form_moved_into_dialog_with_same_fields(self) -> None:
        index = self._index()
        self.assertIn('<dialog id="agentDialog" class="agent-dialog">', index)
        self.assertLess(index.index('id="agentDialog"'), index.index('id="agentForm"'))
        for field_id in FORM_FIELD_IDS:
            with self.subTest(field=field_id):
                self.assertIn(f'id="{field_id}"', index)
        for label in ("名称", "系统提示词（预设与规则）", "固定 Skill", "工具集", "工具预设"):
            with self.subTest(label=label):
                self.assertIn(label, index)

    def test_actions_pinned_to_dialog_footer(self) -> None:
        index = self._index()
        self.assertIn('class="agent-form-footer"', index)
        self.assertLess(index.index('class="agent-form-body"'), index.index('class="agent-form-footer"'))
        self.assertLess(index.index('class="agent-form-footer"'), index.index('id="saveAgentForm"'))
        css = self._css()
        footer = css[css.index(".agent-form-footer {"):]
        footer = footer[: footer.index("}")]
        self.assertIn("border-top", footer)

    def test_legacy_structures_removed_everywhere(self) -> None:
        sources = {
            "public/index.html": self._index(),
            "public/js/09-settings.js": self._settings(),
            "public/js/15-bind-events.js": self._bind(),
        }
        for name, source in sources.items():
            for snippet in LEGACY_SELECTORS:
                with self.subTest(file=name, snippet=snippet):
                    self.assertNotIn(snippet, source)

    def test_card_markup_and_add_card_last(self) -> None:
        source = self._settings()
        for snippet in (
            'class="agent-card',
            'data-agent-card="${id}"',
            'data-agent-delete="${id}"',
            "data-agent-add",
            "agent-card-name",
            "agent-card-meta",
            "agent-card-prompt",
            "agent-card-badge",
            "agent-card-tag",
            "新增 Agent",
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, source)
        self.assertLess(
            source.index("agents.map((agent) => agentCardMarkup(agent, defaultId)).join('')"),
            source.index("agent-card-add"),
            "「新增 Agent」卡片必须排在最后一张",
        )

    def test_built_in_agents_have_no_delete_button(self) -> None:
        source = self._settings()
        self.assertIn("agent.built_in ? '' : `<button class=\"agent-card-delete", source)

    def test_render_does_not_auto_open_form(self) -> None:
        source = self._settings()
        body = source[source.index("export function renderAgentManager()"):]
        body = body[: body.index("\n}")]
        self.assertNotIn("showAgentForm(", body)

    def test_card_click_opens_dialog_with_that_agent(self) -> None:
        source = self._settings()
        self.assertIn("export function openAgentCard(", source)
        body = source[source.index("export function openAgentCard("):]
        body = body[: body.index("\n}")]
        self.assertIn("state.bootstrap?.agents", body)
        self.assertIn("showAgentForm(agent)", body)

    def test_delete_closes_dialog_when_editing_deleted_agent(self) -> None:
        source = self._settings()
        body = source[source.index("export async function deleteAgent("):]
        body = body[: body.index("\n}")]
        self.assertIn("confirm(", body)
        self.assertIn("if ($('#agentFormId').value === agentId) hideAgentForm();", body)

    def test_bindings_delegate_from_card_container(self) -> None:
        bind = self._bind()
        self.assertIn("$('#agentCards').addEventListener('click'", bind)
        self.assertIn("$('#agentCards').addEventListener('keydown'", bind)
        self.assertIn("data-agent-delete", bind)
        self.assertIn("data-agent-add", bind)
        self.assertIn("data-agent-card", bind)
        self.assertIn("openAgentCard(", bind)
        self.assertIn("$('#agentDialog').addEventListener('close'", bind)
        self.assertIn("$('#saveAgentForm').addEventListener('click'", bind)
        self.assertIn("$('#cancelAgent').addEventListener('click'", bind)

    def test_css_three_columns_and_responsive(self) -> None:
        css = self._css()
        cards = css[css.index(".agent-cards {"):]
        cards = cards[: cards.index("}")]
        self.assertIn("repeat(3, minmax(0, 1fr))", cards, "一行最多三张卡片")
        self.assertIn("repeat(2, minmax(0, 1fr))", css, "窄屏应降级为两列")
        delete_rule = css[css.index(".agent-card-delete {"):]
        delete_rule = delete_rule[: delete_rule.index("}")]
        self.assertIn("position: absolute", delete_rule, "× 必须固定在卡片右上角")
        prompt_rule = css[css.index(".agent-card-prompt {"):]
        prompt_rule = prompt_rule[: prompt_rule.index("}")]
        self.assertIn("-webkit-line-clamp: 2", prompt_rule, "提示词摘要必须两行截断")
        dialog = css[css.index(".agent-dialog {"):]
        dialog = dialog[: dialog.index("}")]
        self.assertIn("max-height", dialog, "弹层内部需可滚动，不能溢出视口")

    def test_no_manual_agent_id_field(self) -> None:
        """用户不再手填 Agent ID：表单里不得再有 ID 输入框，保存走隐藏字段。"""
        index = self._index()
        self.assertNotIn('id="agentId"', index)
        self.assertNotIn("英文、数字、下划线或连字符", index)
        source = self._settings()
        self.assertNotIn("#agentId", source)
        body = source[source.index("export async function saveAgentForm()"):]
        body = body[: body.index("\n}")]
        self.assertIn("id: $('#agentFormId').value.trim()", body)
        self.assertIn("保存后自动分配 ID", source)

    def test_avatar_button_next_to_character_card(self) -> None:
        """「自定义头像」按钮必须紧挨「导入角色卡 PNG」，且选图只做预览、保存才上传。"""
        index = self._index()
        self.assertLess(index.index('id="importAgentCharacterCard"'), index.index('id="pickAgentAvatar"'))
        self.assertLess(index.index('id="pickAgentAvatar"'), index.index('id="agentAvatarPreview"'))
        source = self._settings()
        for snippet in ("export function pickAgentAvatar(", "export function handleAgentAvatarFile(",
                        "agentAvatarUrl(", "/api/agents/avatar/", "FormData()"):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, source)
        bind = self._bind()
        self.assertIn("$('#pickAgentAvatar')?.addEventListener('click', pickAgentAvatar)", bind)
        self.assertIn("handleAgentAvatarFile(file)", bind)

    def test_avatar_shown_on_cards_and_messages(self) -> None:
        """头像要落到两处：Agent 卡片（小圆图）与助手消息气泡（替换「AI」圆标）。"""
        source = self._settings()
        self.assertIn('class="agent-card-avatar"', source)
        messages = (ROOT / "public/js/04-messages.js").read_text(encoding="utf-8")
        self.assertIn("export function currentAgentAvatarUrl(", messages)
        self.assertIn('class="message-avatar message-avatar-img"', messages)
        self.assertIn('<div class="message-avatar">AI</div>', messages, "没有头像时必须保留默认 AI 圆标")
        css = self._css()
        self.assertIn(".agent-card-avatar {", css)
        self.assertIn(".agent-avatar-preview {", css)
        avatar_rule = css[css.index(".message-avatar-img {"):]
        avatar_rule = avatar_rule[: avatar_rule.index("}")]
        self.assertIn("object-fit: cover", avatar_rule, "头像必须中心裁切填充，不能拉伸变形")

    def test_skill_picker_uses_cards_and_matches_tool_height(self) -> None:
        """固定 Skill 与工具集同款卡片，且两个列表共用同一高度（用户要求：拉高到一样高）。"""
        source = self._settings()
        body = source[source.index("export function renderAgentSkillPicker()"):]
        body = body[: body.index("\n}")]
        self.assertIn('class="skill-card"', body)
        self.assertNotIn('class="skill-item"', body, "技能页的列表样式不该再被 Agent 弹层复用")
        css = self._css()
        skills_rule = css[css.index(".agent-skills {"):]
        skills_rule = skills_rule[: skills_rule.index("}")]
        self.assertIn("--agent-list-h:", skills_rule, "两个列表必须共用同一个高度变量")
        skill_list = css[css.index(".agent-skills .skill-list {"):]
        skill_list = skill_list[: skill_list.index("}")]
        self.assertIn("height: var(--agent-list-h)", skill_list)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", skill_list)
        card = css[css.index(".agent-skills .skill-card {"):]
        card = card[: card.index("}")]
        self.assertIn("display: flex", card)
        self.assertIn("border: 1px solid var(--line)", card)
        tool_scope = css[css.index("#agentToolScope {"):]
        tool_scope = tool_scope[: tool_scope.index("}")]
        self.assertIn("height: var(--agent-list-h)", tool_scope, "工具集必须用同一高度变量，否则两边不等高")
        self.assertIn("grid-auto-rows: max-content", tool_scope,
                      "固定高度 + 默认 align-content:stretch 会把分组行均摊压扁（实测 19.6px vs 分组头 59px）")

    def test_scrollable_lists_are_not_clipped(self) -> None:
        """固定 Skill / 工具集列表是滚动容器：网格行必须按内容定高，否则被裁掉且点不到。"""
        css = self._css()
        body = css[css.index(".agent-form-body {"):]
        body = body[: body.index("}")]
        self.assertIn("grid-auto-rows: max-content", body,
                      "auto 行按最小内容高度定尺，滚动容器贡献 0 → 行高只剩表头")
        provider_body = css[css.index(".provider-form-body {"):]
        provider_body = provider_body[: provider_body.index("}")]
        self.assertIn("grid-auto-rows: max-content", provider_body)

    def test_expanded_tool_cards_compact_and_distinct_from_parent(self) -> None:
        """展开后的工具卡片：紧凑 + 与父分组区分度（用户实测"太宽松、父子分不清"）。"""
        css = self._css()
        grid = css[css.index(".permission-grid {"):]
        grid = grid[: grid.index("}")]
        self.assertIn("gap: 7px", grid)
        card = css[css.index(".permission-grid > label {"):]
        card = card[: card.index("}")]
        self.assertIn("min-height: 54px", card, "卡片高度要收紧（原 68px）")
        self.assertIn("padding: 8px 10px", card)
        self.assertIn("background: var(--surface)", card, "子卡片白底")
        self.assertIn("border: 1px solid var(--line-strong)", card, "描边要加强，与父区分")
        self.assertIn("box-shadow", card)
        desc = css[css.index(".permission-grid small {"):]
        desc = desc[: desc.index("}")]
        self.assertIn("-webkit-line-clamp: 2", desc, "说明两行截断，保证卡片高度一致")
        expanded = css[css.index(".agent-tool-group .permission-grid {"):]
        expanded = expanded[: expanded.index("}")]
        self.assertIn("background: var(--surface)", expanded, "展开区必须白底")
        head = css[css.index(".agent-tool-group-head {"):]
        head = head[: head.index("}")]
        self.assertNotIn("background:", head, "折叠时分组头保持原样（无灰底）")
        self.assertIn(".agent-tool-group:not(.collapsed) .agent-tool-group-head { background: var(--surface-2); }",
                      css, "只有展开的那一组，分组头才变浅灰条子")
        settings = (ROOT / "public/js/09-settings.js").read_text(encoding="utf-8")
        self.assertIn("label.title = `${tool.name}：${tool.description}`", settings,
                      "说明被截断，完整描述要进 title 悬停可见")

    def test_index_html_still_has_no_duplicate_ids(self) -> None:
        ids = re.findall(r'\sid="([^"]+)"', self._index())
        duplicates = {value for value in ids if ids.count(value) > 1}
        self.assertFalse(duplicates, f"index.html 存在重复 id：{sorted(duplicates)}")


if __name__ == "__main__":
    unittest.main()
