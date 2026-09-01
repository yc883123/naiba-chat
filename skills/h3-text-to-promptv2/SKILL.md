---
name: h3-text-to-prompt
description: This skill should be used when the user provides a piece of text (article, novel excerpt, short story, script, or scene description) and wants it automatically converted into a MiniMax H3 video prompt that strictly follows the H3 prompt-writing rules. It converts arbitrary text into a Ref2VA six-field prompt, handling Subject/Picture numbering, upload-prefix templates, audio-sync (Sx) binding, bilingual output, and scene-prompt generation.
---

# H3 文本转提示词自动转换 Skill

将用户提供的任意文字（文章、小说、短文、剧本、分镜梗概、场景描述）**自动转换为符合《H3 提示词编写规则》的 Ref2VA 多片段六字段提示词**。

本 skill 是"转换流程"：规则本身完整保留在 `references/h3-rules.md`（判卷标准），本文件负责"解题步骤"。生成前**必须读取** `references/h3-rules.md` 全文并严格遵循，尤其注意第九节的快速检查清单。

内置的官方依据文件（`references/` 下）：
- `ref-en.txt`：H3 全参考模式（Ref2VA）输出格式规范，是 `h3-rules.md` 中"依据：ref-en.txt 第 X 条"的来源，定义六字段、四种标签、retention 关系值、Sx 说话人编号等。
- `base-en.txt`：H3 视频提示词基础写法（T2VA/I2VA/FL2VA/L2VA），与 Ref2VA 共享镜头、相机运动、说话人 `<d>` 格式、音景等通用规则。
- `example-segment-P05-1.md`：一份完全合规的真实范例片段，结构与措辞为最终输出范本。

`h3-rules.md` 中引用的 "ref-en.txt / base-en.txt 第 N 条" 均指向本目录内置的对应文件，无需再依赖外部官方 skill 路径。

## 触发场景

- 用户丢来一段文字，要求"写成 H3 提示词 / 转成 Ref2VA / 生成分镜提示词"
- 用户要求"按规则把这段剧情转成六字段"
- 用户要求为某段剧情生成中英双语 H3 Prompt + 场景图提示词

## 输入信息收集

转换前确认以下信息（缺失则合理推断并注明假设）：

1. **源文字**：用户提供的剧情/对白文本
2. **参考资产清单**：本段实际上传了哪些 PNG（人物/场景）与声线音频（Audio）。若无明确清单，按文字中出现的实体推断，并在前缀中只列推断出的项；**严禁虚构不存在的 Picture/Audio 编号**
3. **总时长**：默认按项目模版（5S/10S/15S），或用户指定
4. **是否连续视频**：本段是独立生成，还是与前后片段拼接成同一连续视频（影响 S 编号是否跨段延续）

## 转换流程（逐步执行）

1. **抽取实体并编号**
   - 读取源文字，按"主角→配角→场景→道具"顺序列出本段所有实体
   - 分配 `Subject 1..N`（每段从 1 开始，唯一不重复，见规则一）
   - 对有上传参考图的实体分配 `Picture 1..M`（连续无跳号，独立于 Subject，场景也要，见规则二）

2. **建立 subject_definitions**
   - 每行 `<Subject N> is [描述] in <Picture M>.`（有图）或 `is [描述].`（无图）
   - **禁止写来源文件名**（规则 4.3）

3. **撰写上传模板前缀**（规则三）
   - 无声线：`|本段上传（按此顺序）：<Picture 1> 中文短名、...；仅用于人物身份、服装与材质参考。|`
   - 含声线：Picture 全部列完后，按发声顺序列 `<Audio 1> 角色名声线、...`，末尾改为"；仅用于人物身份、服装、材质与人声音色参考。"
   - 前缀列的资产必须与 `subject_definitions` 两处同现（规则 7.7）
   - **前缀不是提示词内容，必须写在提示词代码块外面**（版本标题下方、代码块上方单独一行）；禁止放入代码块内，否则脚本按代码块提取提示词会失败（规则 3.3）

4. **分配发声编号 (Sx)**（规则七）
   - 按本段实际发声先后顺序分配 S1, S2...；人物与独立语音源共用一套；不发声角色不分配
   - `subject_definitions` 中对应的 `<Audio N>` 绑定同一 `(Sx)`
   - `retention_analysis` 与 `summary` 内**不带** `(Sx)`

5. **撰写六字段**（规则四）
   - `summary`：方括号任务类型开头，用 Subject 编号描述
   - `retention_analysis`：每个 Subject 单独列关系值
   - `detailed_description`：按 `[Shot N]` 分镜，首镜无时间戳，Shot 2 起 `At MM:SS.mmm` 递增；发声处 `<Subject N> (Sx)`；普通参考 Picture 只用裸 `<Subject N>`，不重复外观（规则 7.8/7.10）；同步音效留在镜头内（规则 7.6）
   - `overall_soundscape`：持续环境声
   - `non_diegetic_music`：通常 N/A

6. **生成中英双版本**（规则五）
   - 英文六字段 + 中文六字段，Subject 编号两版一致
   - 前缀两版都加，且都写在各自代码块**外面**

7. **生成中文剧情概要 + 场景图提示词**（规则五/六）
   - 中文剧情概要：简要分镜描述
   - 场景图提示词：中英双语，"本片段场景对应"指向本段场景 Subject 编号

## 输出模板

- 严格使用 `assets/segment-template.md` 的结构与占位符生成最终内容。
- 生成前**先阅读** `references/example-segment-P05-1.md`，这是一份完全合规的真实范例（含上传前缀、Subject/Picture/Audio 绑定、中英双版本、Sx 分配的详细描写、场景图提示词）。其结构、编号风格与措辞是最终输出的标准范本，确保生成物与范例在规范上一致。

## 自检

生成后逐条核对 `references/h3-rules.md` 第九节快速检查清单，全部通过方可交付。
