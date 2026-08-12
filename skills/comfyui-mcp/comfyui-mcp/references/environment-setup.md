# Environment Setup

Use this reference when the MCP server is not registered, its Python process cannot start, or ComfyUI paths are unknown.

## NaibaChat Automatic Registration

NaibaChat has a native `register_mcp` tool. Run `scripts/install_naiba.ps1` without arguments, then pass its `registration` result to that tool. The script discovers ComfyUI from its running Python process, so launcher-managed installations are supported, and installs the bounded MCP dependency only when it is missing. NaibaChat persists bundled MCP files outside PyInstaller's temporary directory and connects the service immediately.

Do not edit `config.json` directly and do not write another MCP client's settings. Manual configuration below applies only to hosts that do not expose `register_mcp`.

## Required Values

Collect these values before editing an MCP client configuration:

| Value | Portable Windows example | Required |
|---|---|---|
| ComfyUI root | `D:\AI\ComfyUI_windows_portable\ComfyUI` | Yes for setup diagnostics |
| Python | `D:\AI\ComfyUI_windows_portable\python_embeded` or its `python.exe` | Yes |
| ComfyUI URL | `http://127.0.0.1:8188` | Yes; default only after verification |
| Workflow directory | `<skill>\workflows` | Optional; defaults to the skill directory |

The ComfyUI root must contain `main.py`. Accept either the embedded Python directory or the full executable path. Portable distributions commonly spell the folder `python_embeded` with one `d`; do not silently change it to `python_embedded`.

If the user has already provided a path, validate it before asking again. If paths are unknown, ask:

> 请提供 ComfyUI 根目录（包含 main.py）以及嵌入式 Python 目录或 python.exe 的完整路径；如果服务不是 http://127.0.0.1:8188，也请提供实际地址。

## Generate And Validate Configuration

Run the setup helper with the supplied embedded interpreter; the helper itself only uses the standard library:

```powershell
& "D:\AI\ComfyUI_windows_portable\python_embeded\python.exe" scripts\configure_mcp.py `
  --comfyui-root "D:\AI\ComfyUI_windows_portable\ComfyUI" `
  --python "D:\AI\ComfyUI_windows_portable\python_embeded" `
  --url "http://127.0.0.1:8188"
```

The result contains:

- `ready`: whether paths, the `mcp` import, and URL checks passed;
- `issues`: concrete blockers;
- `config`: the `mcpServers.comfyui` object to merge into the client configuration;
- `install_command`: the exact command to install `mcp` when required.

Do not replace an entire user configuration with the generated snippet. Merge only the `comfyui` entry into its existing `mcpServers` object.

The effective configuration has this shape:

```json
{
  "mcpServers": {
    "comfyui": {
      "command": "D:\\AI\\ComfyUI_windows_portable\\python_embeded\\python.exe",
      "args": ["D:\\skills\\comfyui-mcp\\scripts\\comfyui_mcp_server.py"],
      "env": {
        "COMFYUI_ROOT": "D:\\AI\\ComfyUI_windows_portable\\ComfyUI",
        "COMFYUI_URL": "http://127.0.0.1:8188",
        "COMFYUI_WORKFLOWS_DIR": "D:\\skills\\comfyui-mcp\\workflows"
      }
    }
  }
}
```

## Install The Server Dependency

Install into the same interpreter used by `command`:

```powershell
& "D:\AI\ComfyUI_windows_portable\python_embeded\python.exe" -m pip install "mcp>=1.2.0,<2"
```

If the embedded interpreter has no pip, bootstrap pip using the distribution's supported method or use a separate venv for the MCP server. A separate venv is valid because this MCP server communicates with ComfyUI over HTTP; it does not import ComfyUI internals.

## Start And Verify

1. Start ComfyUI with its normal launcher and confirm the configured URL opens.
2. Restart the MCP client after changing its configuration.
3. Call `get_environment`.
4. Require `comfyui_reachable: true` before workflow validation or execution.

Interpret common failures:

| Symptom | Action |
|---|---|
| Python path does not exist | Ask for the portable root and embedded Python path again |
| `No module named mcp` | Install `mcp` into the configured `command` interpreter |
| Connection refused on 8188 | Start ComfyUI or correct `COMFYUI_URL` |
| MCP starts but no workflows appear | Correct `COMFYUI_WORKFLOWS_DIR` |
| Custom nodes are missing | Install them in the target ComfyUI instance, restart ComfyUI, then revalidate |
