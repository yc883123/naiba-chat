---
name: shortdramav2-rh
description: "短剧单元批量生成（云端版）：以 comfyui-shortdramav2 的剧本解析与 <Picture N>/<Audio N> 标签映射逻辑为基础，通过 RunningHub AI 应用（webappId）上传参考资产、组装 nodeInfoList、提交 MiniMax H3 Ref2VA 工作流并下载视频。适用于已把短剧工作流发布为 RunningHub AI 应用、需要按单元/分段批量出片的场景。触发词：短剧批量、单元生成、RunningHub 短剧、Ref2VA、上传资产、nodeInfoList、AI 应用提交。"
homepage: https://www.runninghub.ai
metadata:
  {
    "openclaw":
      {
        "emoji": "🎬",
        "requires": { "bins": ["python3"] },
        "primaryEnv": "RUNNINGHUB_API_KEY"
      }
  }
---

# ShortDrama v2 (RunningHub) Skill

运行脚本（Python 3 标准库，无需 curl）：
- `python {baseDir}/scripts/generate_unit_rh.py --info [WEBAPP_ID]` — 探测 AI 应用节点并打印映射
- `python {baseDir}/scripts/generate_unit_rh.py <单元号> [build|submit] [段号...]`
- 共享库：`{baseDir}/scripts/rh_client.py`（自包含 RunningHub 客户端）

## 定位

本包是 `comfyui-shortdramav2` 的 **RunningHub 云端版**：剧本解析、段号/语言识别、
`<Picture N>` / `<Audio N>` 标签映射逻辑与本地版完全一致，仅把「构建工作流 JSON +
提交本地 ComfyUI」替换为 **上传参考资产 → 组装 nodeInfoList → 提交 AI 应用 →
轮询 → 下载视频**。两个包互不影响，本地版继续可用。

## 使用流程（先做一次性配置）

1. **确认 AI 应用已发布**：在 RunningHub 网页把 MiniMax H3 Ref2VA 短剧工作流发布为
   AI 应用（公开可访问），并**先在网页成功运行一次**（首次需跑通，之后才能 API 调用）。
   记下应用 ID（URL 形如 `https://www.runninghub.ai/ai-detail/<webappId>`）。
2. **配置 `config.json`**：复制 `config.example.json` → `config.json`，填写：
   - `webapp_id`：上一步的应用 ID
   - `site`：`ai`（runninghub.ai）/ `cn`（runninghub.cn）。**两个站点账号与 Key 不通用**。
   - `rh_api_key`：留空则依次回退环境变量 `RUNNINGHUB_API_KEY` →
     `~/.openclaw/openclaw.json` 的 `skills.entries.runninghub.apiKey`
   - `instance_type`：`default`（24G）/ `plus`（48G，更稳但更贵），默认 `default`
   - `pictures` / `audios`：1-based 编号 → `asset_dir` 下文件名（也支持绝对路径）
   - `segment_pictures` / `segment_audios`：按段覆盖（键为段号）
3. **放置资产**：参考图/音频放入 `asset_dir`（默认包内 `assets/`）。
4. **放置剧本**：单元 markdown 放入包根目录，文件名符合 `unit_glob`（默认 `单元{unit:02d}_*.md`）。

## 剧本格式要求（与 comfyui-shortdramav2 相同）

- 每个段落用 `## ` 开头，标题含「第N段 / 片段 NN / P05-5」等可识别段号。
- 段内含 `**H3 提示词**`（或 `**H3 Ref2VA 提示词**` 等），后跟 ```text 代码块。
- 代码块第一行必须是 `subject_definitions:`（字段名后可带空格），提示词内用
  `<Picture N>` / `<Audio N>` 标签引用角色图与角色音。
- 中英文提示词并存时脚本会交互询问提交哪种语言。

## 节点映射（自动探测 + 手动覆盖）

提交依赖「提示词写入哪个节点、参考图/音写入哪个槽位」，脚本自动探测，结果存到
`NN单元输出/rh_nodes.json` 供核对：

- 提示词节点：优先 `MiniMaxH3` 系列节点的 `prompt` 字段，回退任意 STRING 字段。
- 参考槽位：`fieldName` 匹配 `ref_images.ref_image_(\d+)`（IMAGE）/
  `ref_audios.ref_audio_(\d+)`（AUDIO），槽位号 = 匹配数字 + 1（0-based → 1-based 标签）。
- **探测不对/应用结构特殊时**，在 `config.json` 的 `runninghub` 段覆盖：
  `prompt_node: "nodeId:fieldName"`、`ref_images: {"1": "nodeId:fieldName"}`、`ref_audios: {...}`。
  用 `--info` 先看真实节点列表。

## 命令用法

```powershell
$env:PYTHONIOENCODING = "utf-8"
python {baseDir}/scripts/generate_unit_rh.py 01              # 提交全部段并等待下载
python {baseDir}/scripts/generate_unit_rh.py 01 build        # 只组装 nodeInfoList 落盘，不提交
python {baseDir}/scripts/generate_unit_rh.py 01 submit 3     # 只提交第 3 段（测试/续跑）
python {baseDir}/scripts/generate_unit_rh.py --info 2093984571330498561   # 探测应用节点
```

可选参数：`--site ai|cn`、`--api-key KEY`、`--instance-type default|plus`（优先级高于 config）。

输出目录：`NN单元输出/`，含每段 `NN-XX.nodeinfo.json`（组装结果，便于排查）、
`NN-XX.<ext>`（下载视频，MOV 容器自动修复为 MP4）、`rh_nodes.json`（节点映射）、
`rh_uploads.json`（上传 fileName 缓存，24h 内复用、过期自动重传）。

## RunningHub 注意事项

- **应用必须先网页跑通一次**，否则 `apiCallDemo` 返回无节点（NO_NODES）。
- 上传链接仅 **1 天**有效；脚本按资产绝对路径去重上传并缓存，超 24h 自动重传。
- 单任务通常 **1–10+ 分钟**（48G `plus` 实例更稳定）；提交前先告知用户耐心等待。
- 每段独立 taskId，单段失败不影响其他段；失败会打印错误码与修复步骤。
- 消耗金额与耗时在成功后打印（`[消耗] ¥x` / `[耗时] xs`），回复用户时以「花了 ¥x」呈现。

## 常见错误速查

| 错误码 | 含义 / 修复 |
|---|---|
| NO_API_KEY | 未配置 Key：填 `rh_api_key`、设 `RUNNINGHUB_API_KEY` 或 openclaw.json |
| APP_INFO_FAILED | webappId 错误或应用不可公开访问 |
| NO_NODES | 应用没在网页跑通过，先去网页跑一次 |
| NO_PROMPT_NODE / NODE_CONFIG | 探测不到提示词节点/配置槽位不存在：用 `--info` 核对后配置 `runninghub` 覆盖 |
| UPLOAD_FAILED | 上传失败：检查文件存在、网络、余额 |
| NODE_ERRORS | 工作流节点报错：多为参数/资产格式问题 |
| INSUFFICIENT_BALANCE | 余额不足，需充值 |
| TASK_FAILED / TASK_TIMEOUT | 任务失败/超时：看网页端详情 |
