# naiba-chat

一个轻量级的本地 AI 对话助手，支持在线模型 API 调用、技能系统、MCP 工具集成。

## 功能特性

- **在线模型支持**：支持 OpenAI、Claude、Gemini、LM Studio 等多种 API 格式
- **技能系统**：可扩展的技能插件，支持自定义脚本和工具
- **MCP 工具集成**：支持 Model Context Protocol 工具调用
- **Agent 模式**：支持多步推理和工具调用
- **响应式 UI**：支持桌面和移动端访问

## 快速开始

### 方式一：使用预编译版本（推荐）

1. 下载最新的 `naiba-chat.exe` 文件
2. 双击运行，程序会自动打开浏览器
3. 在设置中配置 API 供应商和模型

### 方式二：从源码运行

1. 确保已安装 Python 3.10+
2. 克隆仓库：
   ```bash
   git clone https://github.com/yourusername/naiba-chat.git
   cd naiba-chat
   ```
3. 创建虚拟环境并激活：
   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # Windows
   # 或
   source .venv/bin/activate  # Linux/macOS
   ```
4. 安装依赖（仅 launcher 需要）：
   ```bash
   pip install pywebview pystray pillow
   ```
5. 运行服务器：
   ```bash
   python server.py
   ```
6. 打开浏览器访问 `http://localhost:8765`

## 配置说明

### API 供应商配置

在设置页面中添加 API 供应商：
- **名称**：供应商显示名称
- **API URL**：API 端点地址
- **API Key**：API 密钥
- **请求格式**：选择对应的 API 格式
- **模型名称**：使用的模型名称

### 技能系统

技能文件位于 `skills/` 目录，每个技能是一个文件夹，包含：
- `SKILL.md`：技能描述文件
- 脚本文件：Python 或 PowerShell 脚本

### MCP 服务

支持 Model Context Protocol 工具调用，可在设置中添加 MCP 服务器。

## 项目结构

```
naiba-chat/
├── server.py          # 主服务器
├── config.json        # 配置文件（自动生成）
├── storage.py         # 数据存储
├── chat.py            # 对话管理
├── skills/            # 技能目录
├── public/            # 前端文件
│   ├── index.html
│   ├── app.js
│   └── styles.css
└── README.md
```

## 开发说明

### 添加新技能

1. 在 `skills/` 目录创建新文件夹
2. 创建 `SKILL.md` 文件描述技能
3. 添加脚本文件
4. 重启服务或点击"重新扫描"

### 自定义 MCP 服务

在设置中添加 MCP 服务器配置，支持 stdio 和 HTTP 两种连接方式。

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request！
