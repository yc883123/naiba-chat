# Official `comfy-mcp` Backend

This backend is Comfy's first-party local MCP server. It is separate from the NaibaChat server in `scripts/comfyui_mcp_server.py` and should be installed as an external dependency rather than vendored into this skill.

## Requirements

- Python 3.10 or newer
- `comfy-cli` 1.14 or newer on the launched process's `PATH`
- A ComfyUI workspace configured with `comfy install` or `comfy set-default <path>`
- A running ComfyUI for execution, unless the client exposes `launch_comfyui`

Install the server and CLI in the environment used by the MCP client:

```bash
pip install "comfy-cli>=1.14.0" comfy-mcp
```

If `comfy` is not on the MCP client's `PATH`, set `COMFY_BIN` to its absolute path. Do not copy a path from an example; discover it from the user's environment.

## Client registration

The server speaks MCP over stdio. The command is `comfy-mcp`; there is no HTTP URL for this backend.

Claude Desktop or Cursor configuration:

```json
{
  "mcpServers": {
    "comfy-mcp": {
      "command": "comfy-mcp",
      "env": {
        "COMFY_BIN": "C:\\path\\to\\venv\\Scripts\\comfy.exe"
      }
    }
  }
}
```

Claude Code:

```bash
claude mcp add comfy-mcp -e COMFY_BIN=/path/to/venv/bin/comfy -- comfy-mcp
```

Keep this registration separate from the NaibaChat server ID `comfyui`. Restart or reload the external MCP client after changing its configuration.

## Operational contract

Call `server_info` first. If it reports that ComfyUI is not running, ask before calling `launch_comfyui`. For discovery, use:

- `search_templates` and `fetch_template` for runnable template workflows;
- `search_nodes`, `get_node`, and `list_nodes` for installed/custom nodes;
- `search_models` for models available to the local workspace.

Use `validate_workflow` before a first run when it is exposed. Submit with `run_workflow`, preserve the returned `prompt_id` or job ID, and monitor with `job_status`, `wait_for_job`, or `watch_job`. Copy completed files with `fetch_outputs(prompt_id, out_dir)`.

The official server's workflow arguments and result shape are not the same as the NaibaChat metadata contract. Do not pass `extra_inputs`, `confirmed_default_ids`, or a NaibaChat `.meta.json` requirement ID unless the official tool schema explicitly accepts them. Do not call `get_environment`, `list_workflows`, `get_workflow_requirements`, or `get_image` when only the official server is registered.

## Failure handling

- If `comfy-mcp` cannot be spawned, verify the exact executable path and the Python environment, then report the launch error.
- If `server_info` works but execution fails, inspect the selected workspace, node availability, model files, and workflow format before retrying.
- Never silently switch to the NaibaChat backend or an external generation provider. Tell the user which backend failed and what is needed to continue.
