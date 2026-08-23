---
name: official-comfy-mcp
description: Optional reference for connecting an external MCP client to Comfy's first-party local server. NaibaChat itself uses ComfyUI HTTP/CLI capabilities and does not auto-install or expose MCP from this Skill.
---

# Official comfy-mcp (external client reference)

This document is guidance only. It does not register a server, add tools, or
change NaibaChat permissions. For ordinary NaibaChat conversations use the
generic `http_request` or `run_command` tools against a running ComfyUI.
Use the MCP steps below only when an external MCP host is explicitly being
configured by the user.

Official reference: https://docs.comfy.org/agent-tools/mcp.md#local-comfy-mcp-connection
This Skill covers the **local** connection only. The cloud endpoint
`https://cloud.comfy.org/mcp` is a separate MCP server and must never be
silently substituted for local ComfyUI.

## NaibaChat path (no MCP required)

- Start ComfyUI first and confirm `GET http://127.0.0.1:8188/system_stats`.
- Submit an API-format workflow with `POST /prompt`.
- Poll `GET /history/{prompt_id}` and retrieve files with `GET /view`.
- The same flow can be run from a Skill script or `comfy` CLI. A domain Skill
  may guide this process, but without that Skill the generic HTTP/CLI tools
  remain available.

## External MCP path

- The server is local and operates the user's own ComfyUI workspace and GPU.
- Do not substitute Comfy Cloud, an image API, or another provider.
- If the server or local workflow cannot run, report the blocker and stop.

## Connect and install (external MCP host only)

1. If `server_info` is already available, call it first; never register a second server.
2. Check for Python 3.10+, `comfy-cli>=1.14.0`, `comfy-mcp`, and a local ComfyUI workspace.
3. Install only missing pieces. The normal commands are:
   - `pip install "comfy-cli>=1.14.0"`
   - `comfy install` to create a workspace, or `comfy set-default <path>` for an existing install
   - `pip install comfy-mcp`
4. Register one stdio server with ID `comfy-mcp`, command `comfy-mcp`, no args, and `COMFY_BIN` set to the absolute `comfy` executable only when it is not on the spawned process PATH.
5. Reload/reconnect the MCP process after registration, then call `server_info`. Do not claim the connection is ready until this succeeds.

## Inspect and run

- Call `server_info` before the first execution in a session.
- The server does not launch ComfyUI implicitly. Use `launch_comfyui` only after the user authorizes starting it; otherwise ask the user to start ComfyUI with `comfy launch`.
- At the beginning of a local session call `server_info`; then use `search_templates`, `search_nodes`/`get_node`, and `search_models` against the live install before choosing a workflow.
- Prefer `run_template` for a discovered template. For a file workflow use `run_workflow` with an API-format JSON workflow, never a UI-format graph.
- Call `validate_workflow` before the first run and again after graph, node, or model changes.
- For async execution preserve the returned job/prompt ID, submit once, then use `job_status`, `wait_for_job`, or `watch_job`; finish with `fetch_outputs` and show the actual output paths.
- Ask for confirmation before launching/stopping ComfyUI, downloading models, overwriting workflows, or running other destructive operations.
- Keep user-selected input files and output directories inside the active workspace when possible; do not invent paths.

## Troubleshooting

- Verify the exact `comfy-mcp` executable and Python environment on spawn failure.
- If `server_info` reports no running ComfyUI, verify the configured workspace and offer `launch_comfyui`; do not retry generation blindly.
- If a tool is missing, call `list_tools`/the MCP capability listing and adapt to the installed server version instead of inventing a tool name.
- Verify registration and reload the owning client if `server_info` is unavailable.
- Validate node classes, model files, and API-format structure before retrying a workflow.
- Never switch to an external provider without explicit user approval.
