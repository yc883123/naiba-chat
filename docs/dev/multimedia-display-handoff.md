# 多媒体显示途径与待改进清单（交接文档）

> **性质**：阶段交接文档，供下一阶段（多媒体显示优化）开工用；**使命完成即删**（与 `docs/dev/` 既有惯例一致）。
> **权威现状说明**仍以 `项目维护说明（修改代码前必读）.md` 为准；本文只补充"多媒体显示"这一专题的现状、缺陷与改正建议。
> **写作时点**：2026-09-08，master=`16230f5`，版本号仍为 `2.0.0-beta`。
> **证据口径**：本文所有"现状"均为**读代码确证**（附文件:行号）；"缺陷"中仅"GIF 缩略图 404"是**真机冒烟实测**（`.tmptest/lightbox_smoke.cjs` 观察到 4 个 `_thumb.webp` 404），其余为代码推演，尚未逐个实测。

---

## 一、两条完全独立的显示链路

### A. 用户上传的附件（发送即显示、可持久化）

| 步骤 | 位置 | 说明 |
|---|---|---|
| 1. 选择/拖入/粘贴 | `public/js/15-bind-events.js`（`#attachButton`/`#fileInput`/`.composer-wrap` drop）、`12-chat-input.js::handlePasteImage` | 三条入口最终都调 `uploadFiles()` |
| 2. 上传 | `public/js/10-upload.js::uploadFiles/uploadOne` → `POST /api/uploads` | XHR multipart 流式、真实进度、单文件取消、并发上限 3、80MB 前置校验 |
| 3. 落盘 | `naiba/http.py::_upload_request`（按 Content-Length 精确读流 + python_multipart）→ `app._upload_spooled` → `naiba/storage/media.py::store_uploaded_file` | 图片压缩/缩略图、内容 sha256 去重、分日目录 `data/uploads/YYYY-MM-DD/` 原子落盘 |
| 4. 待发送 chips | `public/js/10-upload.js::renderPendingFiles` | 图片显示缩略图，**其它类型只显示文件名**；移除 chip 调 `POST /api/uploads/delete`（有引用则拒删） |
| 5. 发送 | `public/js/11-run-stream.js::sendChatMessage` | `attachments = [{name,path,size,thumb_path}]` 随 `POST /api/chat` 提交 |
| 6. 落库 | `naiba/storage/store.py::create_chat_run` | 写入**用户消息** `metadata.attachments` |
| 7. 渲染 | `public/js/04-messages.js::uploadedFileMarkup`（第 176 行） | 立即渲染（乐观）+ 重开会话按 metadata 重渲染 |

### B. 工具/模型产出的媒体（**整轮结束时一次性**出现）

| 步骤 | 位置 | 说明 |
|---|---|---|
| 1. 提取时机 | `naiba/run/chat.py` 三处：正常 `:656`、中止 `:907`、失败 `:1070` | 均在**收尾**调用 `extract_attachments(tool_runs, …)`；**流式过程中不产生媒体卡** |
| 2. 扫描与缓存 | `naiba/core/attachments.py::extract_attachments`（第 118 行起） | 递归扫本轮 tool_runs 的 `result`（优先 `json.loads`，失败回退正则抓 `盘符:\...` 与 `http(s)://`）；支持 ComfyUI `/view?filename=x.png` 形态；本机文件与本地 ComfyUI URL 一律**由宿主缓存**到 `data/generated/<sha16>_<name>`（已在 uploads 的直接复用），并补缩略图 `<主图 stem>_thumb.webp` |
| 3. 去重与上限 | 同上，第 268-302 行 | 先按文件名去重（**优先保留带 thumb_path 的版本**），再按文件内容 sha256 去重，**单条消息最多 20 个**（`if len(final) >= 20: break`） |
| 4. 落库 | `run/chat.py` 收尾 metadata | 写入**助手消息** `metadata.attachments` |
| 5. 渲染 | `public/js/12-chat-input.js::handleDoneEvent`（`:705`）→ `04-messages.js::messageElement` → `03-media.js::mediaMarkup`（`:526`） | 用完整消息**整体替换**流式行，此时媒体卡出现 |
| 6. 与"修改文件"互斥 | `naiba/core/file_changes.py:41` | 多媒体产物**不进**"修改文件"总结与右侧文件面板，避免两套入口 |

**枚举类工具门禁**（决定"列目录时是否把图片当附件显示"）：

- `naiba/core/attachments.py::ENUMERATION_TOOLS = {list_directory, search_files, grep}`（`grep` 是 `search_files` 的查询层别名）；
- 命中枚举类工具时，仅当 `_image_intent(message)` 为真才产出附件——即用户本轮消息**同时**命中"图片词"（`_IMAGE_MEDIA_TERM_RE`）与"查看动作词"（`_IMAGE_VIEW_ACTION_RE`，如列出/看看/查找）；
- 入口在 `run/chat.py:658` 的 `allow_enumerated_media=_image_intent(message)`（**只对正常完成路径生效**，中止/失败路径不带该参数）。

### C. 别把"显示"和"进模型上下文"混为一谈

| 场景 | 显示 | 模型上下文 |
|---|---|---|
| 用户上传图片 | 用户气泡缩略图 + 灯箱 | 多模态模型：`build_model_history` 以 content 块注入；文本模型：改写为**路径占位**，由模型按需调 `vision_analyze` |
| 工具产出的图片 | 助手气泡媒体卡（整轮结束） | **只有模型主动调 `vision_analyze` 才进上下文**；单批 ≤4 张，超限在注入文本里标注"共 N 张/已展示前 M 张" |
| 工具结果文本 | 工具块（`display_tool_run`） | `model_visible_result` 按工具脱敏：vision 装载形态只留 `note`+图片名（剥离 path/thumb/尺寸）；`vision_image_ops` 产物路径**保留**（模型引用凭据） |

---

## 二、当前支持的多媒体类型

| 层 | 名单 | 位置 |
|---|---|---|
| 后端"媒体附件"识别 | 图片 `.png .jpg .jpeg .webp .gif`；视频 `.mp4 .webm .mov .m4v .ogv`；音频 `.wav .mp3 .m4a .ogg .flac` | `core/attachments.py::MEDIA_PRODUCT_EXTS`（第 27 行）与 `extract_attachments` 内 `extensions`（第 132 行，两处需同步） |
| 上传压缩 + 缩略图 | **只有** `.png .jpg .jpeg .webp` | `storage/media.py::IMAGE_SUFFIXES`（第 30 行）；GIF 走"原样存盘、无缩略图"分支（`_process_uploaded_image` 第 279 行） |
| 前端图片判定 | 含 `.gif` | `04-messages.js:180`、`03-media.js:534`、`10-upload.js:93` |
| 前端视频/音频判定 | 与后端一致 | `03-media.js:540-541` |
| `/api/file` MIME 兜底 | webp/avif/gif/mp4/webm/mov/ogg/oga/ogv/m4a/wav/flac/svg | `http.py::_MEDIA_MIME_FALLBACK`（第 43 行） |
| 视觉工具侧图片后缀 | `.png .jpg .jpeg .webp .gif` | `vision/runtime.py::IMAGE_SUFFIXES`（第 57 行） |

**展示样式一览**

| 位置 | 图片 | 视频 | 音频 | 其它 |
|---|---|---|---|---|
| 用户气泡 `uploadedFileMarkup` | `<figure class="attachment-image">`：缩略图 + figcaption 文件名，点击开灯箱 | **仅文件名 chip** | **仅文件名 chip** | 仅文件名 chip |
| 助手气泡 `mediaMarkup` | `.media-item`：缩略图 + 悬停 `↩` 复用按钮，点击开灯箱 | `<video controls playsinline preload="metadata">` | `<audio controls preload="metadata">` | `<a class="file-chip">` 链接 |

容器统一 `.media-grid`（`styles.css:510`，`auto-fit minmax(180px,1fr)`；图片最大高 200px / 视频 520px）；图片 URL 统一 `/api/file?token=&path=`（`http.py::_serve_local_file`，第 911 行；允许根 = 会话工作区 + 数据目录；ComfyUI `/view` 由服务端代理；`Cache-Control: private, max-age=3600`）。

**图片灯箱**（本轮新增，`03-media.js`）：列表 = `#messages img[data-large-url]` 按 DOM 顺序去重（= 历史出现顺序）；左右按钮 / ←→ 键 / 左右半屏点击翻页（首尾循环）、底部 `N / M` 计数；滚轮以光标为锚点缩放（1–6 倍）、缩放后拖动平移（仅当图片大于视口）、双击复位、触屏单指平移/双指捏合/双击切换；单张图或输入区附件/文件面板图片不显示切换控件。

---

## 三、已发现的缺陷（含证据与影响面）

> 优先级建议见第四节。每项给出：现象 → 证据 → 影响 → 建议改正 → 验证方式。

### D1. 用户上传的视频/音频**不能播放**（只有文件名）

- **证据**：`04-messages.js::uploadedFileMarkup`（第 180-186 行）只对图片做特殊渲染，其余一律 `<span class="file-chip">${name}</span>`；`<video>/<audio>` 只在助手侧 `mediaMarkup`（`03-media.js:540-541`）出现。
- **影响**：用户上传 MP4/MP3 后无法在会话里预览/播放，只能靠文件名判断；与助手侧体验不一致。
- **建议**：把 `mediaMarkup` 里的媒体判定抽成公共函数（如 `mediaKind(source, name) -> 'image'|'video'|'audio'|'other'`），用户气泡对视频/音频渲染播放器（不带 `↩` 复用按钮），图片保持现有 figure 结构。
- **验证**：扩展 `.tmptest/lightbox_smoke.cjs` 或新增冒烟——上传 mp4/mp3 后断言 `#messages .message-row.user video/audio` 存在且 `src` 正确。

### D2. GIF 缩略图必然 404（破图，无兜底）

- **证据**：`IMAGE_SUFFIXES` 不含 `.gif` → `_process_uploaded_image` 对 GIF 直接 `return data, None, b""`（`storage/media.py:279`）→ 上传响应 `thumb_path: ""`；而前端把 `.gif` 当图片（`04-messages.js:180`、`10-upload.js:93`），`attachmentThumbUrl` 在无 `thumb_path` 时按 `<主图 stem>_thumb.webp` 推导（`03-media.js:15-27`）→ 404；`<img>` 无 `onerror` 兜底。
- **实测**：`.tmptest/lightbox_smoke.cjs` 首轮曾观察到 4 个 `_thumb.webp` 404（当时主因是缩略图命名不一致，已修；GIF 属同一条 404 通道，尚未单独实测）。
- **影响**：GIF 在待发送 chip 与消息气泡里都是破图（浏览器显示 alt 文字）。
- **建议**（二选一或并用）：① 前端兜底——`attachmentThumbUrl` 在推导路径不可用时回退主图 URL，并给缩略图 `<img>` 加 `onerror` 回退主图（3~5 行，立刻止血）；② 后端给 GIF 生成首帧 WebP 缩略图（不压缩原图、保动画），复用 `_ensure_webp_thumb` 的写盘逻辑。
- **验证**：单测 `test_upload_system.py` 加"GIF 上传返回非空 thumb_path 或前端可回退"；浏览器冒烟断言 GIF 气泡 `img.naturalWidth > 0`。

### D3. `/api/file` 不支持 Range（大视频/音频不可 seek、整包进内存）

- **证据**：`http.py::_serve_local_file`（第 947-960 行）`data = path.read_bytes()` 后一次性 `Content-Length` 返回，无 `Accept-Ranges`/`206`。
- **影响**：`<video preload="metadata">` 实际会拉全量；大文件（几十~几百 MB）占用服务端内存与首屏时间；进度条拖动可能失效。
- **建议**：实现单区间 Range（`Range: bytes=a-b` → `206` + `Content-Range` + `Accept-Ranges: bytes`，无 Range 时保持 200）；ComfyUI 代理分支可保持现状（远端不支持 Range）。
- **验证**：单测（`tests/` 新增）断言 206/Content-Range/边界（`bytes=0-`、越界 416）；浏览器冒烟里对视频 `seek` 后断言 `currentTime` 生效。

### D4. 媒体卡只在**整轮结束**时出现（流式中途看不到）

- **证据**：`extract_attachments` 只在 `run/chat.py` 的三处收尾调用（`:656/:907/:1070`）；流式期间 `tool_result` 事件只渲染文本工具块（`03-media.js::toolRunMarkup`）。
- **影响**：生图/生视频任务进行中，用户看不到"刚生成的那张"；长任务体感差。
- **建议**：若要做，优先在 `tool_result` 事件里携带**已缓存的媒体元数据**（后端在工具结果落事件时顺带提取一次，复用 `extract_attachments` 的候选扫描），前端就地渲染缩略图并**标记"待定稿"**，终态再以完整 metadata 覆盖；需防重复渲染与事件体积膨胀。
- **风险**：会改变事件负载（契约变更，需先改 `core/contracts.py` 的 `EVENT_PAYLOAD_KEYS`）；建议列为**最后一项**。
- **验证**：`test_events_contract` 守门 + 浏览器冒烟观察中途出现。

### D5. 单条消息 20 个附件是**静默截断**（无标注）

- **证据**：`attachments.py` 末尾 `if len(final) >= 20: break`（第 300-301 行）。
- **影响**：批量生成超过 20 张时，多余的不显示也不提示——与维护说明 §九.24「给模型看的截断必须带标记（静默截断=幻觉误导源）」同一类问题，只是这次坑的是**用户**。
- **建议**：附件列表本身无法自述"被截断"，需在消息层面补一条标注（例如 `metadata.attachments_truncated: {shown: 20, total: N}`，前端在媒体网格下方显示"共 N 张，仅显示前 20 张"）；同时考虑把上限提高或改为按类型分桶。
- **验证**：单测断言截断时返回 `truncated` 信息；前端冒烟断言标注文案出现。

### D6. 助手侧媒体**不显示文件名**

- **证据**：`mediaMarkup` 图片只有 `alt`（第 538 行），视频/音频无任何标签（第 540-541 行）；用户侧图片反而有 `figcaption`（`04-messages.js:184`）。
- **影响**：一张图/一段视频在气泡里没有可读标识，用户难以与"修改文件"或工具块里的路径对应。
- **建议**：给 `.media-item` 增加可选 `figcaption`（文件名，长名截断 + title 全文），视频/音频下方同样加一行文件名。
- **验证**：浏览器冒烟断言 `figcaption` 文本 = 附件名。

### D7. 枚举门禁可能反直觉（"列出目录里的图片"却没缩略图）

- **证据**：`_image_intent` 要求"图片词 ∩ 查看动作词"（`attachments.py:101`）；用户说"把目录里的图片列出来给我看"→ 命中；但"列出目录内容"（含图片）→ 不命中 → 图片只以路径形式留在工具结果里。
- **影响**：用户可能困惑"明明列出了图，为什么没有预览"。
- **建议**：放宽为"用户消息含图片词即可"或"枚举结果全部是图片时自动显示"，同时保留"不因一次目录列举把不相干图片全拉进来"的初衷（可用数量阈值，如 ≤8 张才显示）。
- **验证**：`tests/test_attachments*.py`（若无则新增）覆盖三种输入；冒烟里发一句"列出当前目录"看是否出现缩略图。

### D8. 相邻小问题（顺手可清）

- `styles.css:513-516` 的 `.media-image-link` / `.media-failed` 是**死样式**（JS 已不使用）；
- 用户上传的**非媒体**文件（pdf/doc/zip）在气泡里同样只是文件名 chip（无链接、不可点开）——若希望"点开预览"，可复用右侧文件面板的 `_conv_file_open`；
- `MEDIA_PRODUCT_EXTS`（第 27 行）与 `extract_attachments` 内的 `extensions`（第 132 行）是**两份手写同源名单**，应合并为一个常量（同类漂移风险，参考维护说明 §九.30）。

---

## 四、建议的开工顺序

| 顺序 | 项 | 理由 | 预估面 |
|---|---|---|---|
| 1 | **D2 GIF 破图** | 明确 bug，3~5 行前端兜底即可止血，风险最低 | 前端 1 文件 |
| 2 | **D3 Range 支持** | 视频/音频可用性的硬前提；后端单点改动，有单测可写 | `http.py` 1 处 |
| 3 | **D1 用户侧音视频播放** | 体验一致性；抽公共 `mediaKind()` 后两端共用 | 前端 2 文件 |
| 4 | **D5 截断标注** | 与既有教训直接冲突，改动小、收益明确 | 后端+前端 |
| 5 | **D6 文件名标签** | 纯展示，低风险 | 前端 1 文件 |
| 6 | **D7 门禁放宽** | 需要与用户确认口径（是否接受"目录里所有图都显示"） | 后端 1 文件 |
| 7 | **D4 流式媒体** | 涉事件契约与重复渲染，风险最高，放最后 | 后端+前端+契约 |
| — | D8 顺手清理 | 随任一相关改动一并做 | 小 |

**开工前请先确认**：D7 的显示口径、D5 的截断上限（保持 20 还是提高）、D4 是否要做（涉契约）。

---

## 五、复用资产（下一阶段可直接用）

| 资产 | 用途 |
|---|---|
| `.tmptest/lightbox_smoke.cjs` | 自建 3 图会话（含大图/小图）、断言灯箱切换与缩放；改媒体渲染时可直接扩展 |
| `.tmptest/seed_usage_message.py` + `.tmptest/usage_rate_smoke.cjs` | 直接向 DB 播种"带 metadata 的助手消息"再浏览器断言——**验证纯渲染改动的最快通道**（无需模型） |
| `tests/test_upload_system.py` | 上传/去重/缩略图/清理守门，D1/D2 改动的落点 |
| `tests/test_tool_registry_shape.py::ToolNameSetFreshnessTests` | 工具名硬编码集合守门（D7 若涉及枚举名单需同步） |
| 维护说明 §九.24 / §九.30 | "静默截断"与"硬编码名单漂移"两条教训，本清单 D5/D8 直接相关 |
