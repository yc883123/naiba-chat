---
name: comfyui-mcp
description: Configure and operate a local or portable ComfyUI instance through MCP, including environment/path discovery, embedded-Python setup, workflow compatibility checks, API-format workflow installation, parameter mapping, execution, and output retrieval. Use when a user asks to connect an AI assistant to ComfyUI, run or troubleshoot a ComfyUI workflow, determine which bundled or custom workflows are usable, or adapt an existing ComfyUI workflow for this skill.
---

# ComfyUI MCP

Use this skill to connect an MCP client to ComfyUI's HTTP API and run validated API-format workflows. Do not claim that an arbitrary ComfyUI UI JSON can be converted reliably.

## Provider Boundary

- Treat an image or video generation request under this skill as authorization only for the configured ComfyUI instance.
- Never call a built-in image tool, OpenAI Images API, or any other external generation/editing service for previews, references, fallback, or final output unless the user explicitly requests that provider in the current turn.
- If ComfyUI is unavailable or no compatible workflow can run, stop and report the blocker. Do not silently fall back to another provider.

## Choose The Phase

1. If the ComfyUI MCP tools are already available, call `get_environment`, then `list_workflows`. Do not ask for installation paths when the configured server is healthy.
2. If the server is not configured or cannot start, complete **Environment Setup**.
3. If the user wants to run a bundled workflow, complete **Workflow Gate**, then run it.
4. If the user supplies a custom workflow or asks how to adapt one, complete **Custom Workflow Preparation** before running it.

## Environment Setup

### NaibaChat

When the available tools include `register_mcp`, complete setup without asking the user to edit any configuration file:

1. Run `scripts/install_naiba.ps1` with `run_skill_script` and no arguments. It discovers the ComfyUI instance started by the user's normal launcher, validates its HTTP API, installs the bounded MCP dependency only when missing, and prepares the server registration.
2. Pass the returned `registration` object unchanged to `register_mcp`.
3. Call `call_mcp` with server `comfyui` and tool `get_environment`, then call `list_workflows`.
4. Report success only after `get_environment` confirms `comfyui_reachable: true`.

Never inspect or edit Cline, Claude Desktop, Codex, VS Code, or another client's MCP settings when running inside NaibaChat. Never tell a NaibaChat user to merge JSON or restart the client. If automatic registration fails, report the exact script or tool error.

### Other MCP Clients

Never invent, assume, or copy paths from examples.

1. Inspect the current MCP configuration and any paths already supplied by the user.
2. For a typical Windows portable installation, locate:
   - the ComfyUI root containing `main.py`, such as `...\ComfyUI`;
   - the embedded interpreter, supplied either as `...\python_embeded` or its `python.exe`;
   - the ComfyUI URL, normally `http://127.0.0.1:8188`.
3. If either path cannot be determined safely, ask the user for both paths in one concise question. Also ask for the URL only when the default is not confirmed or does not respond.
4. Read [references/environment-setup.md](references/environment-setup.md), then run `scripts/configure_mcp.py` to validate the paths and generate the MCP configuration.
5. Use the selected interpreter as the MCP server `command`. Install `mcp` into that exact interpreter when the script reports it missing.
6. After registration or configuration changes, restart the MCP client and call `get_environment` before attempting a workflow.

`COMFYUI_ROOT` helps diagnostics and workflow discovery but does not launch ComfyUI. The server communicates with the running instance through `COMFYUI_URL`.

## Workflow Gate

Do not select a workflow from its filename alone.

1. Call `list_workflows` and report the returned compatibility status.
2. Only pass a workflow to `run_workflow` when its local status is `ready`.
3. Call `validate_workflow` before the first run on a machine. Run only when `ready` is `true`; otherwise report missing node types, models, LoRAs, VAEs, or other assets.
4. When model discovery is needed, call `list_models` with `kind`, a narrow `query`, and a small `limit`. Do not request the legacy unfiltered inventory during normal operation.
5. Call `get_workflow_requirements` before every run. Show the user each required input, its node/input binding, the saved default (if any), and the public parameters that will be applied.
6. Ask the user to provide or explicitly confirm every required/default item, including source images, edit prompts, model, size, sampler, and output branch when declared by metadata. Do not silently use a saved image or prompt from another user's workflow.
7. For each `type: image` requirement, ask the user to upload an image or provide its local path. Place the selected file under the configured ComfyUI `input/` directory and pass its ComfyUI-relative filename through `extra_inputs`. Never substitute an unrelated saved image.
8. Present a concise pre-run summary containing the selected image(s), exact prompt, model, width/height, steps, CFG, seed policy, and relevant workflow-specific options. Ask only about missing or saved-default values; do not re-ask values the user explicitly supplied in the current request.
9. Only call `run_workflow` after the user supplies those values. Pass explicit values through `prompt`, `extra_inputs`, and numeric arguments. Pass accepted saved-default requirement IDs through `confirmed_default_ids`. Use legacy `confirm_defaults: true` only after the user accepts every listed default.
10. Never run entries marked `needs_api_export`, `invalid`, or `invalid_metadata`.

Bundled workflow status:

| File | Purpose | MCP status |
|---|---|---|
| `txt2img_basic.json` | Basic SDXL text-to-image example | Ready after its checkpoint and core nodes validate |
| `txt2img_basic_ui.json` | Editable ComfyUI canvas source for the example | Not runnable; export API format first |

A workflow is usable only when all of these are true:

- it is ComfyUI API format: `{node_id: {class_type, inputs}}`;
- every referenced node type is installed in the target ComfyUI instance;
- referenced model assets exist under the target ComfyUI installation;
- it has an output node that produces an image, audio, or video artifact supported by the workflow;
- dynamic values are either mapped in a sidecar metadata file or supplied through `extra_inputs`.

## Custom Workflow Preparation

Read [references/workflow-compatibility.md](references/workflow-compatibility.md) in full when adapting, inspecting, or troubleshooting a user workflow.

1. Preserve the user's editable UI workflow as the source of truth.
2. Export a separate API-format JSON from ComfyUI. If API export is unavailable, enable the ComfyUI developer option that exposes **Save (API Format)** or **Export (API)**.
3. Run `scripts/workflow_tool.py inspect <api-workflow.json>`.
4. Resolve structural errors and review every detected parameter binding, especially when the graph contains multiple samplers, prompt encoders, detailers, or custom nodes.
5. Run `scripts/workflow_tool.py install <api-workflow.json> --name <name>` to copy it into `workflows/` and generate `<name>.meta.json`.
6. Edit the generated metadata when automatic binding is ambiguous. Use explicit `node_id` and `input` pairs; do not depend on node ordering.
7. Call `list_workflows`, then `validate_workflow`, and only then use `run_workflow`.

Do not hand-convert `widgets_values` from a complex UI JSON. Widget layouts are node-version-specific and are not a stable API schema.

## Run A Workflow

1. Use `workflow_name` for an installed workflow or `workflow_json` for an already validated API graph.
2. First call `get_workflow_requirements`. Treat a `needs_user_input` response as a pause to ask the user, not as an execution error.
3. Supply only parameters requested by the user. Explicit metadata bindings take precedence over inference.
4. Use `extra_inputs` for exact overrides in this form:

```json
{"12": {"strength_model": 0.8}, "25": {"filename_prefix": "project/run"}}
```

5. Preserve the returned `prompt_id`. Report generated local paths or URLs and any timeout/error detail.
6. Use `get_image` to check a known `prompt_id` again after a client-side timeout.

## MCP Tools

| Tool | Use |
|---|---|
| `get_environment` | Show URL, interpreter, configured paths, and ComfyUI reachability |
| `list_models` | Search one model kind with query/pagination; no arguments preserve the legacy full inventory |
| `list_workflows` | Classify local workflow files and show parameter mapping mode |
| `get_workflow_requirements` | Show required files, prompts, saved defaults, and current parameter bindings before a run |
| `validate_workflow` | Compare one API graph against the live node and model inventory |
| `run_workflow` | Submit a validated workflow, apply parameters, wait, and download outputs |
| `get_image` | Retrieve image URLs for an existing `prompt_id` |

## Resources

- [references/environment-setup.md](references/environment-setup.md): portable/venv path intake, configuration generation, and startup diagnosis
- [references/workflow-compatibility.md](references/workflow-compatibility.md): compatibility levels, API export, metadata schema, and custom workflow adaptation
- [references/comfyui_api.md](references/comfyui_api.md): ComfyUI HTTP API summary
- `scripts/configure_mcp.py`: validate environment paths and print an MCP client configuration
- `scripts/workflow_tool.py`: inspect and install API-format workflows
- `scripts/comfyui_mcp_server.py`: MCP server
