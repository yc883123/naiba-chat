# ComfyUI HTTP API 速查

ComfyUI 内置 HTTP 服务（默认 `http://127.0.0.1:8188`），本 MCP 技能即基于此 API 封装。

## 关键端点

### POST /prompt
提交一个工作流（节点图）执行。
```json
{ "prompt": { "<node_id>": { "class_type": "...", "inputs": { ... } } }, "client_id": "<uuid>" }
```
返回：`{ "prompt_id": "..." }`

### GET /history/{prompt_id}
查询某次提交的执行历史与输出。出现 `prompt_id` 即表示完成。
```json
{ "<prompt_id>": { "outputs": { "<node_id>": { "images": [ { "filename": "x.png", "subfolder": "", "type": "output" } ] } } } }
```

### GET /view
下载生成的图像：
```
/view?filename=<filename>&subfolder=<subfolder>&type=output
```

### GET /object_info
查询所有节点类型的输入/输出定义，及可选项（如 `CheckpointLoaderSimple` 的 `ckpt_name` 列表）。

### GET /system_stats
系统/显存状态。

## 工作流 JSON 格式
必须是 ComfyUI 中「Save (API Format)」导出的格式：
```json
{
  "10": { "class_type": "CLIPTextEncode", "inputs": { "text": "positive", "clip": ["6", 0] } },
  "6":  { "class_type": "EmptyLatentImage", "inputs": { "width": 1024, "height": 1024, "batch_size": 1 } },
  "3":  { "class_type": "KSampler", "inputs": { "steps": 25, "cfg": 7, "seed": 0, "denoise": 1.0, "model": ["4",0], "positive": ["10",0], "negative": ["11",0], "latent_image": ["6",0] } }
}
```
字段间依赖通过 `["node_id", output_index]` 引用其他节点的输出。

## 常见 class_type
| class_type | 作用 |
|------------|------|
| CheckpointLoaderSimple | 加载大模型 (ckpt_name) |
| CLIPTextEncode | 文本编码 (text) |
| EmptyLatentImage | 空潜空间 (width/height/batch_size) |
| KSampler | 采样 (steps/cfg/seed/denoise) |
| VAEDecode | 潜变量解码为图像 |
| SaveImage | 保存图像到 output |
| LoraLoader | 加载 LoRA (lora_name/strength) |
