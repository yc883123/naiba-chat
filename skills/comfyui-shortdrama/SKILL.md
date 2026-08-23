---
name: comfyui-shortdrama
description: 通用短剧批量生成模板：用 ComfyUI + MiniMax H3 Ref2VA 把任意单元短剧剧本（markdown，每段英文 H3 提示词 + <Picture N>/<Audio N> 参考标签）批量转成 15 秒视频片段。任何人准备自己的角色图/声音、填一份 config.json 即可复用"解析 → 映射 → 建工作流 → 入队 → 轮询 → 收集"流水线。
---

# 短剧 Ref2VA 批量自动化（通用模板）

一套可复用的短剧自动化模板：把「分好段的短剧剧本」批量变成「15 秒视频片段」。只靠 ComfyUI HTTP API（`/prompt` + `/history` + `/view`），资产/地址/命名全部走 `config.json`，换剧不换代码。

> 先启动 ComfyUI，再运行脚本。此 Skill 不依赖 MCP，也不会注册或安装
> MCP；没有加载本 Skill 时，仍可用 NaibaChat 的通用 `http_request` 或
> `run_command`/`comfy` CLI 直接调用 ComfyUI。

## 什么时候用

- 剧本是单元 markdown（`单元0X_*.md`），每段 `## 第N段 …（0–15 秒）`，含 `**H3 提示词**` 的 ` ```text ``` ` 代码块（英文）和 `<Picture N>` / `<Audio N>` 参考标签。
- 要批量生成 15s 片段，且角色形象/音色跨段一致。
- 后端是 ComfyUI + MiniMax H3 Ref2VA。

## 准备（新项目一次性）

1. ComfyUI + MiniMax H3 模型（Ref2VA 扩散模型、Qwen3VL 文本编码器、视频/音频 VAE）。
2. 导入模板 `assets/MiniMaxH3-ref视频自动.json`（固定节点 id，脚本按它填值）。
3. 角色定妆图/参考音放进 ComfyUI `input\`。
4. 复制 `config.example.json` → `config.json`，填地址、模板、前缀、图/音映射。
5. 写剧本 markdown（见下）。

## 参考标签 → 槽位映射（关键）

1-based 序号 → 0-based 槽位：

| 标签 | 槽位 | 模板节点 |
|---|---|---|
| `<Picture 1>` | `ref_images.ref_image_0`（"ref 00"） | 366 |
| `<Picture 2>` | `ref_images.ref_image_1` | 367 |
| `<Picture 3>` | `ref_images.ref_image_2` | 368 |
| `<Audio 1>` | `ref_audios.ref_audio_0` | 371 |
| `<Audio 2>` | `ref_audios.ref_audio_1` | 390（自动新建 LoadAudio） |

某段提示词出现 `<Picture N>` 且 config 配了文件名才挂对应槽位；没出现的槽位/节点删掉。台词/外貌描述逐字保留。

## config.json 字段

`comfyui_url`、`template_file`、`output_prefix`、`unit_glob`（剧本文件匹配，默认 `单元{unit:02d}_*.md`）、`pictures {"1","2","3"}`、`audios {"1","2"}`。空字符串 = 不启用该槽位。LoadAudio 支持 `.wav`/`.mp3`，文件名必须与 ComfyUI `input\` 一致。

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
- 覆盖写 `.py` 可能报 `ReplaceFileW EACCES`；写新文件或对 JSON 后处理绕开。
- ComfyUI v0.33.1 `/models` 返回 list 不是 dict；模型存在性查文件系统。
- `Get-CimInstance Win32_Process` 可能被拒，用 `Get-Process`/`netstat`。

## 校验与交付

- ffprobe 校验时长 ≈ 15.08s、含音视频流。
- 复制到成品目录统一命名 `单元号-段号.mp4`，交付清单写清单元/段数/时长/第二音轨段。
