---
name: comfyui-mcp
description: Connect an AI assistant to a local ComfyUI through either the NaibaChat HTTP MCP backend or Comfy's first-party comfy-mcp stdio backend. Use for environment setup, backend selection, workflow validation and execution, model/node/template discovery, output retrieval, and troubleshooting local ComfyUI jobs.
---

# Unified ComfyUI MCP

Use one task-level workflow while selecting the MCP backend that is actually available. Do not merge or replace the two server implementations: they expose different tools and workflow contracts.

## Provider Boundary

- Treat an image or video generation request under this skill as authorization only for the configured ComfyUI instance.
- Never call a built-in image tool, OpenAI Images API, or any other external generation/editing service for previews, references, fallback, or final output unless the user explicitly requests that provider in the current turn.
- If ComfyUI is unavailable or no compatible workflow can run, stop and report the blocker. Do not silently fall back to another provider.

## Select A Backend

Identify the backend from the registered MCP tools; never guess from a server name alone.

| Backend | Detect with | Transport and role |
|---|---|---|
| NaibaChat | `get_environment` and `list_workflows` are available | Custom Python MCP server over the running ComfyUI HTTP API. Preferred inside NaibaChat. |
| Official | `server_info` or `launch_comfyui` is available | Comfy's first-party `comfy-mcp` stdio server backed by `comfy-cli`. |

If both are available, use NaibaChat by default in NaibaChat sessions because it provides metadata-backed parameter and asset gates. Use the official backend when the user explicitly asks for `comfy-mcp`, `comfy-cli`, templates/nodes managed by the official server, or a client where only that server is registered. Keep registrations separate (`comfyui` and `comfy-mcp`); never expose both under one MCP server ID.

Read [references/official-backend.md](references/official-backend.md) when installing, configuring, or troubleshooting the official backend. Read [references/environment-setup.md](references/environment-setup.md) for the custom backend's path and HTTP setup.

## Common Workflow

1. Select the backend using the table above.
2. Check reachability (`get_environment` or `server_info`). Do not submit a job when it is false or unavailable.
3. Discover or confirm the workflow. Use `list_workflows` for the NaibaChat backend; use an explicit workflow path or `search_templates`/`fetch_template` for the official backend.
4. Validate before the first run on a machine. Call `validate_workflow` when the selected backend exposes it. Never run a UI/canvas JSON as if it were API-format JSON.
5. Ask only for missing values and files. Never silently reuse a saved prompt, image, model, LoRA, VAE, or output branch.
6. Submit once, preserve the returned `prompt_id`, wait or poll, then retrieve outputs with `get_image` (NaibaChat) or `fetch_outputs` (official).
7. Report the backend, workflow, prompt ID, generated paths/URLs, and any timeout or validation error.

## NaibaChat Backend

When the available tools include `register_mcp`, complete setup without asking the user to edit any configuration file:

1. Run `scripts/install_naiba.ps1` with `run_skill_script` and no arguments. It discovers the ComfyUI instance started by the user's normal launcher, validates its HTTP API, installs the bounded MCP dependency only when missing, and prepares the server registration.
2. Pass the returned `registration` object unchanged to `register_mcp`.
3. Call `call_mcp` with server `comfyui` and tool `get_environment`, then call `list_workflows`.
4. Report success only after `get_environment` confirms `comfyui_reachable: true`.

Never inspect or edit Cline, Claude Desktop, Codex, VS Code, or another client's MCP settings when running inside NaibaChat. Never tell a NaibaChat user to merge JSON or restart the client. If automatic registration fails, report the exact script or tool error.

For this backend, enforce the workflow gate:

1. Call `list_workflows`; only use entries with status `ready`.
2. Call `validate_workflow` before the first run and stop on missing node types, models, LoRAs, VAEs, or other assets.
3. Call `get_workflow_requirements` before every run. Show each required input, binding, saved default, and public parameter that will be applied.
4. Ask the user to provide or explicitly confirm every required/default item. For image requirements, request an upload or local path, place the file in ComfyUI's `input/`, and pass its relative filename via `extra_inputs`.
5. Only then call `run_workflow`, passing explicit values through `prompt`, `extra_inputs`, numeric arguments, and accepted `confirmed_default_ids`.

The custom server communicates with the running instance through `COMFYUI_URL`; `COMFYUI_ROOT` is for diagnostics and workflow discovery and does not launch ComfyUI.

## Official `comfy-mcp` Backend

Use the official server's native tools and lifecycle. Call `server_info` first; if the user authorizes starting ComfyUI, use `launch_comfyui`, otherwise report that it must be started. Use `search_templates`, `fetch_template`, `search_nodes`, `get_node`, `list_nodes`, and `search_models` for discovery. Use `validate_workflow` before `run_workflow` when present. Retrieve completed files with `fetch_outputs`; use `job_status`, `wait_for_job`, or `watch_job` for asynchronous runs.

Do not assume the official backend understands the NaibaChat metadata sidecars or `extra_inputs` schema. Pass the official tool's documented arguments and preserve its returned job ID. Do not call `get_workflow_requirements` or `get_image` unless those tools are actually registered by the selected server.

## Custom Workflows

For either backend, preserve the user's editable UI workflow as the source of truth and run a separate API-format export. If adapting a workflow for the NaibaChat backend, read [references/workflow-compatibility.md](references/workflow-compatibility.md) in full, then use `scripts/workflow_tool.py inspect` and `install` to create metadata-backed bindings. Do not hand-convert complex `widgets_values`; node versions make that schema unstable.

## Tool Mapping

| Task | NaibaChat | Official |
|---|---|---|
| Environment | `get_environment` | `server_info` |
| Workflow discovery | `list_workflows` | `search_templates`, `fetch_template` |
| Node/model discovery | `list_models` | `search_models`, `search_nodes`, `get_node`, `list_nodes` |
| Validation | `validate_workflow` | `validate_workflow` when available |
| Requirements | `get_workflow_requirements` | Ask from the workflow/tool schema; no Naiba metadata assumption |
| Execution | `run_workflow` | `run_workflow` |
| Wait/status | `wait_for_workflow`, `get_image` | `job_status`, `wait_for_job`, `watch_job` |
| Output retrieval | `get_image` | `fetch_outputs` |

## Resources

- [references/official-backend.md](references/official-backend.md): official `comfy-mcp` installation, client registration, and tool contract
- [references/environment-setup.md](references/environment-setup.md): custom backend path intake and configuration
- [references/workflow-compatibility.md](references/workflow-compatibility.md): API export, compatibility levels, and metadata schema
- [references/comfyui_api.md](references/comfyui_api.md): raw HTTP/API troubleshooting
- `scripts/install_naiba.ps1`: automatic NaibaChat registration
- `scripts/configure_mcp.py`: custom backend configuration for other clients
- `scripts/workflow_tool.py`: inspect/install API-format workflows
- `scripts/comfyui_mcp_server.py`: custom NaibaChat MCP server
