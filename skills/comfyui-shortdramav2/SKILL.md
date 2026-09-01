---
name: comfyui-shortdrama
description: 通用短剧批量生成模板：用 ComfyUI + MiniMax H3 Ref2VA 把任意单元短剧剧本（markdown，每段英文 H3 提示词 + <Picture N>/<Audio N> 参考标签）批量转成 15 秒视频片段。任何人准备自己的角色图/声音、填一份 config.json 即可复用"解析 → 映射 → 建工作流 → 入队 → 轮询 → 收集"流水线。
---

# 短剧 Ref2VA 批量自动化（通用模板）

一套可复用的短剧自动化模板：把「分好段的短剧剧本」批量变成「15 秒视频片段」。只靠 ComfyUI HTTP API（`/prompt` + `/history` + `/view`），资产/地址/命名全部走 `config.json`，换剧不换代码。

> 先启动 ComfyUI，再运行脚本。此 Skill 不依赖 MCP，也不会注册或安装
> MCP；没有加载本 Skill 时，仍可用 NaibaChat 的通用 `http_request` 或
> `pwsh`/`comfy` CLI 直接调用 ComfyUI。

## 什么时候用

- 剧本是单元 markdown（`单元0X_*.md`），每段 `## 第N段 …（0–15 秒）`，含 `**H3 提示词**` 的 ` ```text ``` ` 代码块（中文或英文）和 `<Picture N>` / `<Audio N>` 参考标签。
- 要批量生成 15s 片段，且角色形象/音色跨段一致。
- 后端是 ComfyUI + MiniMax H3 Ref2VA。

## 剧本格式要求（脚本按此解析）

解析行为以 `scripts/generate_unit.py` 的 `parse_markdown()` 为准，写剧本时必须满足：

1. **提示词有效内容必须从 `subject_definitions:` 开始**。脚本只提取 ` ```text ` 代码块中从该字段开始到代码块结束的内容；即使代码块前面误放了上传前缀或说明文字，也不会把它们送进 ComfyUI。找不到该字段时会跳过该段。
2. **上传模板前缀（`|本段上传（按此顺序）：…|`）必须写在代码块外面**。脚本把 ` ```text ` 块内全部内容原样送进 ComfyUI 提示词框，前缀一旦进块就成了提示词的一部分。
3. **`<Picture N>` / `<Audio N>` 标签必须出现在提示词正文中**（通常在 `subject_definitions` 的 `in <Picture N>` 与 `<Audio N> is …` 行）。脚本靠"标签是否出现在提示词里"决定是否挂载对应槽位；标签只写在前缀里会导致图片/音轨全部不挂载。
4. 段标题需能被识别为提示词段：`## 第N段 …`、`## 片段 NN｜…`、`## P0X-N Ref2VA（English）`、`## 二、P0X Ref2VA 六字段版（英文 …）` 等。中文和英文版本都会解析；同一段同时存在两种语言时，运行脚本会让用户选择提交语言。代码块内容从 `subject_definitions:` 开始截取，代码块前或块内误放的“本段上传……”前缀不会送入 ComfyUI。

## 准备（新项目一次性）

1. ComfyUI + MiniMax H3 模型（Ref2VA 扩散模型、Qwen3VL 文本编码器、视频/音频 VAE）。
2. 导入模板 `assets/MiniMaxH3-1采TE加速.json`（脚本按 class_type / ref 槽位自动探测节点，不依赖固定节点 id）。**使用本 Skill 前，先询问用户是否有其他已经跑通的 MiniMaxH3 工作流可以提供；若有，优先以其为准（改用该工作流即可，脚本会自动适配，只需满足下方"换工作流的适配要求"）。**
3. 角色定妆图/参考音放进 ComfyUI `input\`。
4. 复制 `config.example.json` → `config.json`，填地址、模板、前缀、图/音映射。
5. 写剧本 markdown（见下）。

## 参考标签 → 槽位映射（关键）

1-based 序号 → 0-based 槽位。**节点 id 不固定**：脚本按 class_type（`MiniMaxH3ReferenceToVideo` / `SaveVideo`）和 `ref_images.*` / `ref_audios.*` 槽位名自动探测模板节点，用户换工作流也能适配。

| 标签 | 槽位 |
|---|---|
| `<Picture 1>` | `ref_images.ref_image_0`（"ref 00"） |
| `<Picture 2>` | `ref_images.ref_image_1` |
| `<Picture 3>` | `ref_images.ref_image_2` |
| `<Picture 4>` | `ref_images.ref_image_3` |
| `<Picture 5>` | `ref_images.ref_image_4` |
| `<Picture 6>` | `ref_images.ref_image_5` |
| `<Picture 7>` | `ref_images.ref_image_6` |
| `<Picture 8>` | `ref_images.ref_image_7` |
| `<Picture 9>` | `ref_images.ref_image_8` |
| `<Audio 1>` | `ref_audios.ref_audio_0` |
| `<Audio 2>` | `ref_audios.ref_audio_1` |
| `<Audio 3>` | `ref_audios.ref_audio_2` |

自带模板 `assets/MiniMaxH3-1采TE加速.json` 已配好 **9 图槽 + 3 音槽**（`ref_image_0`–`ref_image_8`、`ref_audio_0`–`ref_audio_2`），节点 390–394 为占位图、395 为占位音，跑脚本时会被 config 里的文件名覆盖。

某段提示词出现 `<Picture N>` 且 config 配了文件名才挂对应槽位；没出现的槽位/节点删掉。台词/外貌描述逐字保留。

### 换工作流的适配要求（用户提供其他 MiniMaxH3 工作流时）

新工作流必须是 **API 格式**（`POST /prompt` 提交的 `{节点id: {class_type, inputs}}` JSON，即 ComfyUI「Export (API)」导出的格式），不是 UI 界面导出的 workflow 格式。脚本自动探测，但新工作流必须满足以下结构，否则会报错提示：

- 含 `MiniMaxH3ReferenceToVideo` 节点：提示词写它的 `prompt` 输入（内联字符串），或 `text` 槽位所连的上游文本节点；参考槽位按 `ref_images.ref_image_N` / `ref_audios.ref_audio_N` 命名，加载器（`LoadImage` / `LoadAudio`）直接连到槽位。
- 含 `SaveVideo` 节点：输出文件名前缀写它的 `filename_prefix`。
- 参考槽位数应 ≥ config 里实际用到的 `pictures` / `audios` 最大编号（自带模板 9 图 3 音；模板槽位不够时，超出部分不挂载）。

## config.json 字段

`comfyui_url`、`template_file`、`output_prefix`、`unit_glob`（剧本文件匹配，默认 `单元{unit:02d}_*.md`）、`pictures {"1".."9"}`、`audios {"1".."3"}`。空字符串 = 不启用该槽位。LoadAudio 支持 `.wav`/`.mp3`，文件名必须与 ComfyUI `input\` 一致。

### 按段覆盖映射（segment_pictures / segment_audios）

默认 `pictures`/`audios` 是**整单元一套全局映射**，前提是「角色/音色跨段一致」（同一个编号在每段都指同一资产）。若剧本是**每段各自重新编号**（例如 P05：Picture 3 在段1 是城堡场景、段2 是顾昊、段4 是江归；Audio 1 在段1 是秦西西、段3 是王婉），全局映射会挂错，需要用按段覆盖：

- `segment_pictures`：`{"段号": {"编号": "文件名"}}`，仅覆盖该段对应编号的图槽。
- `segment_audios`：同上，覆盖音槽。
- 未覆盖的编号回退到全局 `pictures` / `audios`。脚本 `seg_cfg()` 按段合并后挂载，build 与 submit 都生效。

示例（P05 实战配置，已实测五段全部挂载正确）：

```json
{
  "pictures": {
    "1": "秦西西（第一套）.png",
    "2": "于闻闻（第一套）.png",
    "3": "顾昊（第一套）.png",
    "4": "王婉（第一章BOSS）.png",
    "5": "江归（第一套）.png"
  },
  "audios": {
    "1": "秦西西.mp3",
    "2": "于闻闻.flac",
    "3": "王婉.wav"
  },
  "segment_pictures": {
    "1": { "3": "S2_哥特式副本集合大厅（夜）.png" },
    "2": { "4": "S3_哥特式离婚登记处（惨白） .png", "5": "王婉（第一章BOSS）.png" },
    "3": { "3": "S3_哥特式离婚登记处（惨白） .png", "4": "王婉（第一章BOSS）.png" },
    "4": { "3": "江归（第一套）.png" },
    "5": {}
  },
  "segment_audios": {
    "1": { "1": "秦西西.mp3", "2": "于闻闻.flac" },
    "2": {},
    "3": { "1": "王婉.wav", "2": "于闻闻.flac" },
    "4": { "1": "于闻闻.flac" },
    "5": { "1": "秦西西.mp3" }
  }
}
```

注意：文件名必须与 ComfyUI `input\` 里的实际文件名**逐字符一致**（含空格，如 `S3_哥特式离婚登记处（惨白） .png` 中 `.png` 前的空格）。

## 脚本（scripts/，先 `cp config.example.json config.json`）

- `generate_unit.py <单元号> [build|submit] [段号...]` — 解析→建 JSON→可选提交+轮询。`build` 只生成不提交。
- `submit_unit.py <单元号> [段号...]` — 读 `0X单元工作流\0X-*.json`，fire-all 入队 + 轮询。
- `fix_audio2.py <单元号> [音轨2文件名]` — 给含 `<Audio 2>` 的段补第二音轨（新脚本已内置，这是兜底）。

脚本与剧本 md、config.json、模板放同目录运行；先设 `PYTHONIOENCODING=utf-8`、`PYTHONUTF8=1`。

## 稳健性

- **先 fire-all 再轮询**：整单元先 POST 进队列再逐个 poll；会话中断/后台任务丢失不影响，ComfyUI 继续跑，恢复后重跑 `submit_unit.py` 或看 `/queue`、`/history`、输出目录。
- 每单元前缀隔离；提交前必查 `GET /system_stats`。

## 已知坑

- 资产缺失先问用户，别用别的音色顶替。
- `<Picture 1>` 必须进 `ref_image_0`（"ref 00"），别贴错。
- **剧本若每段重编号（同名标签不同段指不同资产），必须用 `segment_pictures`/`segment_audios` 按段覆盖**，否则全局映射会挂错且不报错。
- 覆盖写 `.py` 可能报 `ReplaceFileW EACCES`；写新文件或对 JSON 后处理绕开。
- ComfyUI v0.33.1 `/models` 返回 list 不是 dict；模型存在性查文件系统。
- `Get-CimInstance Win32_Process` 可能被拒，用 `Get-Process`/`netstat`。

## 校验与交付

- ffprobe 校验时长 ≈ 15.08s、含音视频流。
- 复制到成品目录统一命名 `单元号-段号.mp4`，交付清单写清单元/段数/时长/第二音轨段。
