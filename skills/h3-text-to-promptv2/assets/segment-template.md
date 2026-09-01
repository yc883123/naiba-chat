# H3 片段提示词模板（Ref2VA 六字段）

> 复制本模板，按 `references/h3-rules.md` 规范填充。占位符用 `<...>` 表示。
> **上传指引行（`|本段上传（按此顺序）：...|`）不是提示词内容，必须放在代码块外面**，代码块内只保留六字段，否则脚本提取提示词会失败。

---

## 英文 H3 Prompt

|本段上传（按此顺序）：<Picture 1> 中文短名A、<Picture 2> 中文短名B；仅用于人物身份、服装与材质参考。|

```text
subject_definitions:
<Subject 1> is [entity description] in <Picture 1>, [optional attributes].
<Subject 2> is [entity description] in <Picture 2>, [optional attributes].
<Subject 3> is [scene/environment description] in <Picture 3>.
<Subject 4> is [prop description].

summary:
[reference generation] <Subject 1> ... <Subject 2> ...

retention_analysis:
<Subject 1>: fully_preserved
<Subject 2>: partially_preserved
<Subject 3>: attribute_transfer
<Subject 4>: weak_reference

detailed_description:
[Shot 1] <Subject 1> ... <Subject 2> (S2) ...
[Shot 2] At 00:04.000, the camera cuts to ... <Subject 1> (S1) says ...

overall_soundscape:
[ambient sound description, no Subject reference]

non_diegetic_music:
N/A
```

---

## 中文 H3 Prompt

|本段上传（按此顺序）：<Picture 1> 中文短名A、<Picture 2> 中文短名B；仅用于人物身份、服装与材质参考。|

```text
subject_definitions:
<Subject 1> 是[实体描述]，在 <Picture 1>，[可选属性]。
<Subject 2> 是[实体描述]，在 <Picture 2>，[可选属性]。
<Subject 3> 是[场景描述]，在 <Picture 3>。
<Subject 4> 是[道具描述]。

summary:
[参考生成] <Subject 1> …… <Subject 2> ……

retention_analysis:
<Subject 1>: fully_preserved
<Subject 2>: partially_preserved
<Subject 3>: attribute_transfer
<Subject 4>: weak_reference

detailed_description:
[Shot 1] <Subject 1> …… <Subject 2> (S2) ……
[Shot 2] At 00:04.000，镜头切到 …… <Subject 1> (S1) 说 ……

overall_soundscape:
[环境声描述，无 Subject 引用]

non_diegetic_music:
N/A
```

---

## 中文剧情概要

> 简要分镜描述：本段讲述……[按镜头简述剧情与对白]。

---

## 场景图提示词（Scene Prompt）

> 场景：[场景名] ｜ 本片段场景对应：<Subject 3>（同场景片段共用同一张，编号以各片段内为准）

**中文描述：**
[场景的中英双语静态参考图描述，含风格、光线、构图、材质]

**English description:**
[English version of the scene reference image description]
