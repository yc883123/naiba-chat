# H3 提示词编写规则（Ref2VA · 多片段序列）

> 适用项目：海螺 H3 提示词工程（本项目全局生效）
> 最后更新：2026-08-27（合并音画同步规范）

---

## 一、Subject 编号规则

### 1.1 编号原则：每段独立，按出现顺序

**Subject 编号在每个片段文件内独立计算，从 1 开始顺序递增，不接受全局资产库编号。**

```
正确示例（第1章-片段3）：
Subject 1 = 秦西西
Subject 2 = 于闻闻
Subject 3 = 顾昊
Subject 4 = 李大伟
Subject 5 = 张雨
Subject 6 = 昏暗大厅
Subject 7 = 银手环

错误示例（全局资产库编号）：
Subject 25 = 王婉
Subject 11 = 登记处大厅
Subject 7 = 银手环
```

### 1.2 编号顺序：按实体在片段中首次出现的逻辑顺序

- 主角/主要人物在前
- 配角/次要人物随后
- 场景/环境在人物之后
- 道具/物体在最后

### 1.3 每个 Subject 编号唯一

**同一个 Subject 编号不得对应多个实体。** 如果一段中有 5 个实体，Subject 编号必须是 1, 2, 3, 4, 5，不能全部用 Subject 2。

---

## 二、Picture 编号规则

### 2.1 独立于 Subject 编号

**Picture 编号与 Subject 编号完全独立，各编各的。**

```
正确示例：
Subject 1 = 秦西西    Picture 1
Subject 2 = 于闻闻    Picture 2
Subject 3 = 顾昊      Picture 3
Subject 4 = 李大伟    （无 Picture）
Subject 5 = 张雨      （无 Picture）
Subject 6 = 昏暗大厅  Picture 4
Subject 7 = 银手环    （无 Picture）
```

### 2.2 连续编号，不跳号

Picture 编号必须是 1, 2, 3, 4... 连续递增，**不允许跳号**（如 1, 3, 4, 6）。

### 2.3 仅分配给有上传参考图的实体

- 有上传 PNG 参考图的人物/场景 → 分配 Picture 编号
- 无参考图的实体（纯文字描述）→ 不分配 Picture 编号
- **场景也需 Picture 编号**（如 `Picture 4 昏暗大厅`）

---

## 三、上传模板前缀

### 3.1 格式（必须使用）

**每个片段的提示词代码块前必须标注以下上传指引行（英中两版同；位置要求见 3.3，必须写在代码块外）：**

无声线参考片段：

```
|本段上传（按此顺序）：<Picture 1> 实体名A、<Picture 2> 实体名B、<Picture 3> 实体名C；仅用于人物身份、服装与材质参考。|
```

含声线参考片段（Audio 列在全部 Picture 之后）：

```
|本段上传（按此顺序）：<Picture 1> 秦西西、<Picture 2> 于闻闻、<Picture 3> 顾昊、<Picture 4> S2哥特式副本城堡（夜）、<Audio 1> 秦西西声线、<Audio 2> 于闻闻声线；仅用于人物身份、服装、材质与人声音色参考。|
```

### 3.2 命名规范

- 使用中文短名（如"秦西西"、"昏暗大厅"），**不要用英文长描述**
- 按 Picture 编号顺序排列；Audio 列在全部 Picture 之后，编号与 `subject_definitions` 的 `<Audio N>` 一致（详见 7.7 前缀完整性）
- 声线文件命名"角色名+声线"（如"秦西西声线"）
- 实体名之间用中文顿号 "、" 分隔
- 末尾固定：无音频用"；仅用于人物身份、服装与材质参考。"；含音频用"；仅用于人物身份、服装、材质与人声音色参考。"

### 3.3 位置（强制：必须写在代码块外）

上传前缀**不是提示词内容**，它只是给操作者的"本段图片/音频按什么顺序上传"的指引。

- 必须写在提示词代码块**外面**：位于版本标题下方、```` ```text ```` 代码块上方，单独一行
- **禁止**放入提示词代码块内，也**禁止**进入 `subject_definitions` 等任何字段
- 中英两版各写一行，放在各自版本标题下方
- **脚本按代码块提取提示词，前缀一旦进入代码块会导致解析/识别失败**
- 交付或复制提示词时，只复制代码块内的六字段内容，不含此行

正确示例：

````markdown
## P05 Ref2VA（中文）

|本段上传（按此顺序）：<Picture 1> 秦西西、<Picture 2> 于闻闻；仅用于人物身份、服装与材质参考。|

```text
subject_definitions:
...
```
````

错误示例（禁止）：

````markdown
```text
|本段上传（按此顺序）：<Picture 1> 秦西西、<Picture 2> 于闻闻；仅用于人物身份、服装与材质参考。|

subject_definitions:
...
```
````

---

## 四、六字段结构

### 4.1 标准字段

| 字段 | 说明 |
|------|------|
| `subject_definitions` | 每段独立顺序编号，包含实体描述与 Picture 绑定 |
| `summary` | 以方括号任务类型开头（可按实际情况组合，如 `[reference generation + audio reference]`），引用 Subject 编号描述剧情 |
| `retention_analysis` | 每个 Subject 单独列出，标注 H3 规定的关系值：`fully_preserved`、`partially_preserved`、`attribute_transfer` 或 `weak_reference` |
| `detailed_description` | 含 `[Shot N]` 分镜标记，Subject 引用用编号 |
| `overall_soundscape` | 环境声，无 Subject 引用 |
| `non_diegetic_music` | 非叙事性音乐，通常 N/A |

### 4.2 Subject 定义行格式

```
<Subject N> is [entity description] in <Picture M>, [optional: additional attributes].
```

- 无 Picture 的实体：`<Subject N> is [entity description].`
- 有 Picture 的实体：`<Subject N> is [entity description] in <Picture M>.`

### 4.3 禁止写来源资产文件名

**`subject_definitions` 中只保留 `in <Picture M>` 绑定，禁止写入 `from the source asset "xxx.png"` 或"（来源资产"xxx.png"）"等来源文件名描述。**

- 正确：`<Subject 1> is Qin Xixi in <Picture 1>, a young woman with ...`
- 正确（中文）：`<Subject 1> 是秦西西，在 <Picture 1>，一名 ... 的年轻女子`
- 错误：`<Subject 1> is Qin Xixi in <Picture 1> from the source asset "秦西西（第一套）.png", ...`
- 错误（中文）：`<Subject 1> 是秦西西，在 <Picture 1>（来源资产"秦西西（第一套）.png"），...`

---

## 五、中英文双版本

### 5.1 每个片段必须包含

1. **英文 H3 Prompt**（Ref2VA 六字段）— 代码块前含上传指引行
2. **中文 H3 Prompt**（Ref2VA 六字段 · 中文版）— 代码块前含上传指引行
3. **中文剧情概要** — 简要分镜描述
4. **场景图提示词**（Scene Prompt）— 中英双语

### 5.2 中英文 Subject 编号一致

中文版的 Subject 编号必须与英文版完全一致（同一实体的 Subject 编号在英中两版中相同）。

---

## 六、场景图提示词

### 6.1 场景对应

场景图提示词中 "本片段场景对应" 需指向该片段中场景实体的 Subject 编号。

```
> 场景：律所内景（昏） ｜ 本片段场景对应：<Subject 6>（同场景片段共用同一张，编号以各片段内为准）
```

### 6.2 场景图提示词独立

场景图提示词是独立部分（用于生成静态参考图），包含中英双语描述，不计入六字段时间轴。

---

## 七、音画同步规范（Ref2VA / 六字段）

> 依据：h3-prompt-writing skill 的 `references/ref-en.txt` 与 `references/base-en.txt`
> 目的：避免生成时声音与画面错位，确保台词/音效稳定卡在对应镜头与动作上。

### 7.1 首镜不写时间戳（强制）

`[Shot 1]` 固定无时间戳；仅从 `[Shot 2]` 起写 `[Shot N] At MM:SS.mmm` 标记切点，且时间严格递增、落在要求总时长内。

- 依据：ref-en.txt 5.1「`[Shot 1]` marks the opening shot and has no timestamp」；base-en.txt 4.2「Do not add a timestamp to the first shot」
- 反例：`[Shot 1] At 00:00.000, ...`
- 正例：`[Shot 1] ...`　/　`[Shot 2] At 00:04.000, the camera cuts to ...`

### 7.2 人物发声必绑 `(Sx)`（强制）

凡有具体人物（在屏或画外）发声，都写 `<Subject N> (Sx)`；`Sx` 编号按实际发声顺序分配，且每次发声处复用同一编号；从不发声的角色不分配编号。

- 依据：base-en.txt 4.4「subjects who speak... use stable IDs such as (S1) and (S2)... characters who never vocalize receive no speaker ID」；ref-en.txt 5.4「Assign (Sx) once according to the order of actual vocal events... Reuse the corresponding ID at every actual vocal event」
- **`(Sx)` 只用于实际语音声源**：`detailed_description` 的每个实际发声处必须带 `(Sx)`；`subject_definitions` 中只有明确对应目标说话者或独立语音源的 `<Audio N>` 才带同一 `(Sx)`。非语音背景音乐、环境音和普通音效的 Audio 定义不添加 `(Sx)`。`retention_analysis` 与 `summary` 均不带（含 Audio 条目）。若多个片段作为同一个连续视频生成，S 编号在连续视频范围内保持一致；若各片段独立生成，则每段按本段实际发声顺序从 S1 重新分配。

### 7.3 非人物独立声源也绑 `(Sx)`（强制）

由具体声源发出的系统播报、机械播报、画外解说、广播或其他独立语音源同样要分配并复用 `(Sx)`；这类声源不定义 `<Subject N>`，直接用稳定的声音描述 + `(Sx)` 即可。纯提示音、铃声、激活音、脚步、碰撞等非语音音效不分配 `(Sx)`，只在对应镜头中描述。

- 依据：ref-en.txt 5.4「If a concrete person, character, narrator, or other independent vocal source produces the voice, assign and reuse (Sx)... When the speaker does not correspond to a defined subject, use a stable voice description followed by (Sx)」
- 正例：`an off-screen mechanical voice (S4), flat and synthetic, announces in sync, <d>[中文] 欢迎玩家进入副本《冷静离婚》。</d>`

> 编号纪律（强制统一）：`(Sx)` **按目标生成范围内实际发声的先后顺序分配**，与 `<Subject N>` 的角色顺序互不绑定。若片段独立生成，则按该片段的实际发声顺序从 S1 开始；若多个片段作为同一连续视频生成，则跨片段延续同一套 S 编号。人物与独立语音源共用同一套发声顺序连续编号；不发声角色和纯音效不分配 `(Sx)`。切勿把 S 编号写成"跟随角色固定"（如误以为 S1 必是秦西西）。

### 7.4 台词/音效的时间关系用自然语言锚定（推荐）

台词、音效与画面动作的先后关系，用自然语言描写（"紧接着 / 在……的瞬间 / 同一刻 / 最终"），不写硬编码秒数，避免与切点时间戳 `[Shot N] At MM:SS.mmm` 混淆。

- 依据：规范中唯一时间戳格式就是切点；台词内时间关系用自然语言或 `<scenetrans>` / `<cutoff>` 表达（base-en.txt 4.4）
- 反例：`his line starting at 00:08.300`
- 正例：`asking the instant she stops at her side`

### 7.5 单镜头多事件按播放顺序描述（强制）

一个镜头内多步事件（如 发送→机械音→白光→纯白）按实际发生先后逐一描写，用连接词锁定顺序；不拆成额外的独立时间点。

- 依据：ref-en.txt 5.1「describes visuals, actions, sound, and dialogue shot by shot in target-video playback order」

### 7.6 同步音效留在 detailed_description（强制）

与某镜头同步的声音事件（激活声、脚步、撞击、机械音等）写明触发动作，留在对应镜头内；持续环境音/氛围音才归入 `overall_soundscape`。

- 依据：ref-en.txt 6「sound events synchronized to a particular shot remain in detailed_description」；base-en.txt 4.6「Dialogue, singing, and diegetic music already belong in the multimodal description and should not be repeated here」
- 正例：`The wristband gives a faint activation chime in the same moment <Subject 1> (S1) touches it`

### 7.7 上传前缀必须列全所有参考资产（强制）

上传前缀需列出本段**实际上传的全部参考文件**（Picture 与 Audio），按实际上传顺序排列，编号与 `subject_definitions` 中的标签一一对应。凡 `subject_definitions` 定义了 `<Audio N>`，前缀必须写明对应声线文件；反之，前缀列出的资产必须在 `subject_definitions` 有对应定义。

- 目的：避免操作时漏传音频导致音色参考失效，或漏传图片导致人物/场景锚点丢失
- 正例：`|本段上传（按此顺序）：<Picture 1> 秦西西、<Picture 2> 于闻闻、<Picture 3> 顾昊、<Picture 4> S2哥特式副本城堡（夜）、<Audio 1> 秦西西声线、<Audio 2> 于闻闻声线；仅用于人物身份、服装、材质与人声音色参考。|`
- 注意：无台词/无声线参考的片段可不列 Audio；`<Audio N>` 与前缀"两处同现"——要么都有、要么都无

### 7.8 detailed_description 内用简写锚定已有 Picture 的 Subject（强制）

`subject_definitions` 已通过 `in <Picture M>` 把"Subject = 角色名 = 参考图外观"绑定。若 Picture 仅用于角色、场景、服装或材质参考，`detailed_description` 每镜头提到该 Subject 时，直接用 `<Subject N>`（必要时加 `(Sx)`）锚定，禁止完整复述服装/发型/配饰——除非该外观细节本身就是本镜头的关键动作点（如"她撩起长发"）。若 Picture 被指定为首帧、末帧、关键帧或构图锚点，则允许在对应镜头自然引用 `<Picture M>`，并明确其帧锚点作用。

- 目的：避免"无名外观实体 + 台词揭示身份"的二次绑定，防止模型把同一角色解析成两个对象（角色分裂）。
- 反例：`<Subject 2> (S2), the woman in a black cropped moto leather jacket over a white cropped tank, black leather shorts, black knee-high boots, a choker, black wavy hair, and a white round wristband, turns toward <Subject 1> and says "我叫于闻闻"...`（长串外观 + 台词自称，易被拆成两人）
- 正例：`<Subject 2> (S2) turns toward <Subject 1> and says "我叫于闻闻"...`（身份在 `subject_definitions` 锚点处锁定，分镜内只用裸 `<Subject N>`，不重复外观）
- 依据：`subject_definitions` 已写 `<Subject 2> is Yu Wenwen in <Picture 2>`，普通角色分镜无需重复外观；只有 Picture 作为具体帧或构图锚点时，才在对应镜头引用 Picture。

### 7.9 不发声的角色不分配 `(Sx)`（强制）

`(Sx)` 仅分配给**实际发声**（说话/唱歌/画外音/独立声源）的实体。全程只点头/做动作/无台词的角色，不分配 `(Sx)`，也不在 `subject_definitions` 写 speaker ID。

- 依据：base-en.txt 4.4「characters who never vocalize receive no speaker ID」；ref-en.txt 5.4「Assign (Sx) once according to the order of actual vocal events」
- 反例：秦西西本段只点头不说话，却写 `<Subject 1> (S3)`
- 正例：秦西西全程 `<Subject 1>`，发声的顾昊 `<Subject 3> (S1)`、于闻闻 `<Subject 2> (S2)`

### 7.10 detailed_description 内 Picture 的使用边界（强制）

当 Picture 仅用于角色、场景、服装、材质或风格参考时，`detailed_description` 的 `[Shot N]` 分镜中不得重复使用 `<Picture M>` 标签（包括 `in <Picture M>`、`the woman in <Picture 2>` 等变体），一律使用裸 `<Subject N>`（发声处加 `(Sx)`）锚定。**例外：**当 Picture 明确作为首帧、末帧、关键帧、编辑帧或构图锚点时，允许在对应镜头引用 `<Picture M>`，并写清楚其帧/构图作用；不得把 Picture 与同一 Subject 写成两个独立实体。

- 目的：避免本地模型把同一 Subject 与 Picture 误解析为两个独立实体，同时保留 Picture 作为帧锚点的能力。
- 依据：ref-en.txt 允许 Picture 作为 first frame、keyframe、last frame 或 composition anchor；普通角色/场景参考则通过 `subject_definitions` 绑定，分镜内用 `<Subject N>` 复用。
- 反例：`<Subject 2> (S2), the woman in <Picture 2>, turns toward <Subject 1>...`（Picture 仅作外观来源时重复引用）；`<Subject 2> (S2) 在 <Picture 2> 中的女子，侧身面向 Subject 1...`
- 正例：`<Subject 2> (S2) turns toward <Subject 1>...`（普通角色参考）；`[Shot 1] begins from <Picture 2>, the locked first frame, then <Subject 2> turns toward <Subject 1>...`（Picture 作为首帧锚点）。
- 与 7.8 的关系：7.8 负责避免重复外观；7.10 规定普通参考 Picture 不在分镜重复，帧/构图锚点 Picture 可在对应镜头明确引用。

---

## 八、常见错误

| 错误 | 修正 |
|------|------|
| Subject 编号基于全局资产库 | 每段独立从 1 开始 |
| 多个实体共用一个 Subject 编号 | 每个实体独立编号 |
| Picture 跳号（1, 3, 4, 6...） | 连续编号（1, 2, 3, 4...） |
| Picture 编号与 Subject 编号绑定 | 两者独立编号 |
| 缺少上传模板前缀 | 每个英文 Prompt 必须添加 |
| 上传前缀使用英文长描述 | 用中文短名 |
| 上传前缀写进提示词代码块内 | 前缀写在代码块外，代码块内只保留六字段；否则脚本提取提示词会失败 |
| 场景不加 Picture 编号 | 场景也需 Picture 编号 |
| 中文版 Subject 编号与英文版不一致 | 必须一致 |
| subject_definitions 写来源资产文件名 | 只保留 `in <Picture M>`，禁写 `from the source asset` / 来源资产 |
| 首镜带时间戳 `[Shot 1] At 00:00.000` | 首镜固定无时间戳，从 Shot 2 起写 `At` |
| 人物发声未绑 `(Sx)` | 每次发声写 `<Subject N> (Sx)`，编号复用 |
| 独立播报/解说声源未绑 `(Sx)` | 语音类独立声源用稳定声音描述 + `(Sx)`；纯音效不绑 |
| `retention_analysis` / `summary` 内写 `(Sx)` | 两处均不带（含 Audio 条目） |
| 台词/音效写硬编码秒数 | 用自然语言锚定时间关系 |
| 同步音效只写在 `overall_soundscape` | 同步事件留在 `detailed_description` |
| 前缀漏列 Audio 声线文件 | 前缀与 `subject_definitions` 两处同现 |
| detailed_description 完整复述已有 Picture 角色的外观 | 只在 `subject_definitions` 绑定外观，分镜内用裸 `<Subject N>` |
| 仅作外观参考的 `<Picture M>` 在 detailed_description 中被重复引用 | 普通分镜用裸 `<Subject N>`；仅当 Picture 是首帧、末帧、关键帧或构图锚点时在对应镜头引用 |
| 不发声角色分配了 `(Sx)` | 只有实际发声的实体才绑 `(Sx)` |

---

## 九、快速检查清单

修改或新建片段时，逐项确认：

- [ ] Subject 编号从 1 开始，按出现顺序递增
- [ ] 每个 Subject 编号唯一，无重复
- [ ] Picture 编号从 1 开始，连续无跳号
- [ ] Picture 仅分配给有上传参考图的实体
- [ ] 场景实体有 Picture 编号
- [ ] 英文 Prompt 代码块外有上传模板前缀（前缀未进入代码块）
- [ ] 上传前缀使用中文短名
- [ ] 上传前缀列全本段所有 Picture 与 Audio，编号与 `subject_definitions` 一一对应
- [ ] 中英文 Subject 编号一致
- [ ] subject_definitions 只写 `in <Picture M>`，无来源资产文件名
- [ ] 场景图提示词中 "本片段场景对应" 指向正确的 Subject 编号
- [ ] 无全局资产库编号残留（如 Subject 25, Subject 11 等）
- [ ] `[Shot 1]` 无时间戳，后续镜头 `At` 时间严格递增且落在总时长内
- [ ] 每个发声人物都绑 `(Sx)`，编号全程复用一致
- [ ] 不发声角色（只点头/动作）不分配 `(Sx)`
- [ ] 独立播报/解说/广播等语音声源绑 `(Sx)`；纯提示音、铃声、脚步、碰撞等音效不绑
- [ ] 仅作外观参考的 Picture Subject 在 detailed_description 内用裸 `<Subject N>` 锚定且未完整复述外观；若 Picture 是帧/构图锚点，已明确写出其锚点作用
- [ ] 仅作外观参考的 Picture 未在 detailed_description 重复出现；作为首帧、末帧、关键帧或构图锚点时，已明确写出其 Picture 作用
- [ ] `retention_analysis` 与 `summary` 内无 `(Sx)`（含 Audio 条目）
- [ ] 台词/音效时间关系用自然语言，无硬编码秒数
- [ ] 同步音效留在 `detailed_description`，持续环境音在 `overall_soundscape`
