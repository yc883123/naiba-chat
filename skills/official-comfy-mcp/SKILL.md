---
name: official-comfy-mcp
description: Install, configure, and operate Comfy's first-party local comfy-mcp server in NaibaChat. Use for connecting a local ComfyUI workspace over MCP stdio, launching or inspecting ComfyUI, discovering templates/nodes/models, validating and running API-format workflows, monitoring jobs, and fetching outputs.
---

# 官方comfy-mcp

Use this Skill only for Comfy's first-party `comfy-mcp` server described by the local Comfy MCP connection documentation. Its user-facing Skill name is `官方comfy-mcp`. It is a separate backend from the existing custom `comfyui-mcp` server and must use the MCP registration ID `comfy-mcp`.

## Backend Boundary

- The server is local: it runs `comfy-cli` against the user's own ComfyUI workspace and GPU.
- Do not use the custom `comfyui` MCP server, Comfy Cloud, an image API, or another generation provider as a fallback.
- If the official server cannot start or the local ComfyUI cannot run the requested workflow, report the blocker and stop.

## Detect And Connect

1. If official tools such as `server_info`, `launch_comfyui`, or `fetch_outputs` are already registered, call `server_info` first. Do not install or register a second server.
2. If only the custom `comfyui` server is registered, do not silently substitute it. Configure this official server separately when the user asks for this Skill.
3. In NaibaChat, when `register_mcp` is available, do not ask the user to edit JSON or another client configuration file. Discover executable paths, install only missing dependencies, register the returned command, then verify with `server_info`.

## Installation And Registration

Requirements:

- Python 3.10 or newer;
- `comfy-cli>=1.14.0`;
- `comfy-mcp` from PyPI;
- a ComfyUI workspace created by `comfy install` or selected with `comfy set-default <path>`.

For a missing dependency, use the Python environment that will own `comfy-mcp`:

```powershell
python -m pip install "comfy-cli>=1.14.0" comfy-mcp
```

Do not invent a Python, `comfy`, or workspace path. On Windows, discover the exact executables with `Get-Command comfy,comfy-mcp,python` or an equivalent read-only check. If a path cannot be determined safely, ask the user for it.

Register the official stdio server with a distinct ID:

```json
{
  "id": "comfy-mcp",
  "command": "comfy-mcp",
  "args": [],
  "env": {
    "COMFY_BIN": "C:\\path\\to\\comfy.exe"
  },
  "enabled": true
}
```

Omit `COMFY_BIN` when `comfy` is already on the environment inherited by the MCP process. Pass the registration object unchanged to `register_mcp`, then call the server's `server_info`. Do not claim success until the response confirms that the local ComfyUI is reachable or clearly reports the next required launch step.

## Start And Inspect ComfyUI

- Call `server_info` before every new session's first execution.
- The official server does not launch ComfyUI implicitly. Call `launch_comfyui` only when the user has authorized starting it; otherwise tell the user to start it with `comfy launch`.
- Use `search_templates` and `fetch_template` to locate runnable workflows.
- Use `search_nodes`, `get_node`, and `list_nodes` to inspect installed and custom nodes.
- Use `search_models` to find models in the selected local workspace.
- Use `stop_comfyui` only when the user explicitly asks to stop the local service.

## Validate And Run A Workflow

1. Use an explicit API-format workflow path or fetch a template. Do not run a ComfyUI canvas/UI JSON as if it were API JSON.
2. Call `validate_workflow` when it is exposed, before the first run on a machine and after changing nodes or models. Stop on missing node types, model files, or malformed graph inputs.
3. Confirm the workflow path and all user-controlled values, including prompts, source images, model choices, dimensions, sampler settings, and output location. Never silently reuse an unrelated saved asset.
4. Call the official `run_workflow` with the arguments accepted by that server. Do not pass the custom server's `extra_inputs`, `confirmed_default_ids`, metadata requirement IDs, or `workflow_name` conventions unless the live official tool schema explicitly supports them.
5. Preserve the returned `prompt_id` or job ID. Submit only once for a job.

## Monitor And Fetch Outputs

- Use `job_status`, `wait_for_job`, or `watch_job` for asynchronous work.
- Use `fetch_outputs(prompt_id, out_dir)` to copy completed files to a user-specified directory.
- Report the server ID, workspace, workflow, job ID, output paths, and any timeout or validation error.
- If a client times out while ComfyUI continues running, check the existing job before submitting again.

## Troubleshooting

- Spawn failure: verify the exact `comfy-mcp` executable, Python environment, and `COMFY_BIN`; do not copy example paths.
- `server_info` unavailable: verify the MCP registration and restart/reload the client that owns it.
- ComfyUI unreachable: inspect the selected workspace and use `launch_comfyui` only with user authorization.
- Workflow failure: validate node classes, model files, custom nodes, and API-format structure before retrying.
- Never switch to the custom `comfyui` backend or an external provider without an explicit user request.
