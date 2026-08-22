---
name: comfyui-llama-model-bridge
description: 让 ComfyUI 模型目录中的 GGUF、Safetensors 语言模型与视觉 mmproj 在 llama.cpp、Ollama、LM Studio 和 Naiba-chat 之间安全互通或完成兼容性判断。用于配置或排查共享模型目录、本地 API、硬链接导入、Modelfile、Safetensors 转换、视觉能力识别、模型迁移、重复占用磁盘、运行端选型与效率问题。
---

# ComfyUI 模型与 Llama 模型互通

让 llama.cpp、Ollama、LM Studio 和 Naiba-chat 尽量复用用户已有的 ComfyUI GGUF 或 Safetensors 语言模型，并正确处理视觉投影文件；同时把不能作为聊天模型的扩散权重明确标记出来。

## 工作流程

1. 确认 ComfyUI 的模型根目录和用户希望接入的运行端。
2. 运行 `python scripts/inspect_model_compatibility.py --root <模型目录>` 做只读盘点。脚本会扫描 GGUF 和 Safetensors：区分 GGUF 主模型/mmproj，并读取 Safetensors header 判断语言模型、视觉语言模型、扩散模型或未知格式。需要机器可读结果时添加 `--json`。
3. 先检查文件名、大小、路径和运行实例，再决定接入方式；不要在未验证前删除、移动或覆盖模型。
4. 按运行端接入：
   - llama.cpp：直接指向共享目录中的主 GGUF；视觉模型同时传入匹配的 mmproj。
   - LM Studio：优先使用 `lms import --hard-link` 建立索引，避免复制大型模型；确认只有预期实例在运行。
   - Ollama：官方模型优先使用 `ollama pull`；手工导入 GGUF 时使用 Modelfile，并为视觉模型明确配置匹配的 `ADAPTER`。不要把 `OLLAMA_MODELS` 误解为散装 GGUF 扫描目录。
   - Safetensors：不要改扩展名冒充 GGUF。先确认它是语言模型而不是 ComfyUI 扩散模型；llama.cpp/LM Studio 通常需要标准配置和 tokenizer 后转换为 GGUF，Ollama 只有在目标架构与权重格式明确受支持时才尝试导入。
5. 在 Naiba-chat 中配置对应的本地 API 地址和模型名。
6. 先查询运行端能力接口，再用真实小图片完成视觉验证；不要只根据模型名称或旁边存在 mmproj 判断视觉可用。
7. 检查模型是否重复占盘、是否误加载多个实例，以及上下文或并发设置是否导致性能异常。
8. 给出可回退、可验证的操作步骤；涉及删除、覆盖或大规模迁移前必须让用户确认精确目标。

需要同时查看运行端状态时，按实际启用的服务追加 `--llama-url`、`--ollama-url` 或 `--lmstudio-url`。脚本只查询接口，不会加载、导入或删除模型。

## 运行端选择与实测依据

在比较三种运行端、解释性能差异、推荐 Naiba-chat 配置、处理视觉模型或共享目录问题时，读取 [本地模型运行端推荐新版.md](references/本地模型运行端推荐新版.md)。该文档包含本机第二轮实测、三端视觉能力检测、LM Studio 重复实例陷阱、Ollama 官方模型与手工 GGUF 的区别，以及磁盘共享建议。

引用性能数字时注明它们来自特定机器和测试条件，不把不同参数规模的模型直接横向比较。

## 安全要求

- 默认只做只读检查，不删除任何模型。
- 不创建第二份大型模型，除非用户明确接受额外磁盘占用。
- 不把单个 Safetensors 权重直接当成可运行模型；缺少 `config.json`、tokenizer、processor 或视觉组件时，先报告缺失项。
- 不对 Safetensors、CKPT、PT、VAE、LoRA 等文件执行未经确认的转换；扩散模型权重不能变成 Naiba-chat 聊天模型。
- 在 Windows 上优先用硬链接复用 LM Studio 模型，并验证链接确实建立成功。
- 不修改 Naiba-chat 的 Agent 能力、`tool_scope` 或固定 `skill_ids`；此 Skill 作为默认内置 Skill 独立工作。
