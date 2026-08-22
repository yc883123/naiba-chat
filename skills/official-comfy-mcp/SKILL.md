---
name: official-comfy-mcp
description: Install, configure, and operate Comfy's first-party local comfy-mcp server in NaibaChat.
mcp_servers: ["comfy-mcp"]
---

# Official comfy-mcp

Use only Comfy's first-party `comfy-mcp` server. Its registration ID is `comfy-mcp`.

## Backend boundary

- The server is local and operates the user's own ComfyUI workspace and GPU.
- Do not substitute Comfy Cloud, an image API, or another provider.
- If the server or local workflow cannot run, report the blocker and stop.

## Connect and install

1. If `server_info` is already available, call it first; never register a second server.
2. In NaibaChat, discover executable paths, install only missing dependencies, register the command, then verify with `server_info`.
3. Requirements: Python 3.10+, `comfy-cli>=1.14.0`, `comfy-mcp`, and a selected local ComfyUI workspace.
4. Register the stdio server with ID `comfy-mcp`, command `comfy-mcp`, and no arguments unless the live installation requires them.

## Inspect and run

- Call `server_info` before the first execution in a session.
- The server does not launch ComfyUI implicitly. Use `launch_comfyui` only after the user authorizes starting it.
- Use template, node, and model discovery tools before selecting a workflow.
- Run only API-format workflows. Validate before the first run and after graph/model changes.
- Confirm all user-controlled values and preserve the returned job ID. Submit only once.
- Use `job_status`/`wait_for_job` and then `fetch_outputs` for asynchronous jobs.

## Troubleshooting

- Verify the exact `comfy-mcp` executable and Python environment on spawn failure.
- Verify registration and reload the owning client if `server_info` is unavailable.
- Validate node classes, model files, and API-format structure before retrying a workflow.
- Never switch to an external provider without explicit user approval.
