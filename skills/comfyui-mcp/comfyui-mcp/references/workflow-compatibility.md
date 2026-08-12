# Workflow Compatibility And Adaptation

Read this reference when selecting, importing, converting, or debugging a workflow.

## Compatibility Levels

| Status | Meaning | Action |
|---|---|---|
| `ready` + `explicit` | API graph with reviewed metadata bindings | Validate against live ComfyUI, then run |
| `ready` + `inferred` | API graph; common parameters are inferred by node type and sampler links | Validate; use `extra_inputs` for ambiguity |
| `needs_api_export` | UI/canvas JSON with `nodes[]` and `links[]` | Open in ComfyUI and export API format |
| `invalid_metadata` | Sidecar exists but references missing nodes/inputs or has invalid structure | Repair the sidecar |
| `invalid` | Not a supported ComfyUI API graph | Re-export or repair the JSON |

Local compatibility does not prove runtime compatibility. `validate_workflow` must also confirm that the target ComfyUI has every node type and referenced model asset.

## Export A Custom Workflow

1. Keep the normal UI workflow for future editing.
2. Open the workflow in the exact ComfyUI installation that will run it.
3. Ensure every branch intended for execution reaches an output node such as `SaveImage`.
4. Enable the developer option that exposes **Save (API Format)** or **Export (API)** when needed.
5. Export a second JSON. A valid top level resembles:

```json
{
  "3": {
    "class_type": "KSampler",
    "inputs": {
      "seed": 0,
      "positive": ["6", 0]
    }
  }
}
```

Do not use a top level containing `nodes`, `links`, canvas positions, or `widgets_values` as the MCP execution file. UI widget arrays vary across node versions and cannot be converted generically without the node definitions loaded by ComfyUI.

## Inspect And Install

Inspect without changing files:

```powershell
python scripts/workflow_tool.py inspect "D:\workflows\portrait_api.json"
```

Review `detected_parameter_map`, `class_types`, `output_nodes`, and `warnings`. Multiple candidate prompt or sampler nodes require manual mapping review.

Install into this skill:

```powershell
python scripts/workflow_tool.py install "D:\workflows\portrait_api.json" --name portrait
```

Use `--workflows-dir` only when the MCP server is configured to use a different workflow directory. Installation creates:

- `portrait.json`: the API graph;
- `portrait.meta.json`: parameter bindings and inspection information.

Run `list_workflows` after installation, then `validate_workflow("portrait")`.

Before queueing any run, call `get_workflow_requirements("portrait")`. The response lists required files and text values, the node/input binding for each, and any saved defaults. Ask the user to provide or confirm those values. A workflow with a saved `LoadImage` filename still requires confirmation; do not silently reuse another person's image. For image requirements, also require `available_in_comfyui_input: true`; otherwise ask for an upload/path and stage the selected file under ComfyUI's `input/` directory.

## Metadata Schema

Use a sidecar named `<workflow>.meta.json`:

```json
{
  "schema_version": 1,
  "display_name": "Portrait",
  "description": "SDXL portrait generation",
  "parameter_map": {
    "prompt": [{"node_id": "6", "input": "text"}],
    "negative_prompt": [{"node_id": "7", "input": "text"}],
    "width": [{"node_id": "5", "input": "width"}],
    "height": [{"node_id": "5", "input": "height"}],
    "seed": [{"node_id": "3", "input": "seed"}],
    "steps": [{"node_id": "3", "input": "steps"}],
    "cfg": [{"node_id": "3", "input": "cfg"}],
    "model": [{"node_id": "4", "input": "ckpt_name"}]
  },
  "input_requirements": [
    {
      "id": "source_image",
      "label": "Source image (required)",
      "type": "image",
      "node_id": "10",
      "input": "image",
      "required": true,
      "confirm_default": true
    },
    {
      "id": "edit_prompt",
      "label": "Edit prompt (required)",
      "type": "text",
      "public_parameter": "prompt",
      "node_id": "19",
      "input": "prompt",
      "required": true,
      "confirm_default": true
    }
  }
}
```

Each parameter maps to one or more `{node_id, input}` objects. Valid public parameters are:

`prompt`, `negative_prompt`, `width`, `height`, `steps`, `cfg`, `seed`, `denoise`, `batch_size`, and `model`.

Bindings are literal. Map every node that should receive a value, and omit nodes that must preserve their saved setting. `extra_inputs` is applied after metadata and therefore wins on conflicts.

`input_requirements` is the user-facing preflight contract. Use it for every required file, prompt, or other value that the assistant must collect before execution:

- `type: image` means the user must provide a filename available under ComfyUI's `input/` directory;
- `type: text` means ask for the exact prompt or instruction;
- `public_parameter` connects a requirement to a `run_workflow` argument such as `prompt`;
- `confirm_default: true` prevents an embedded saved value from being used without user confirmation.

After the user confirms saved values, pass their requirement IDs to `run_workflow` as a JSON array in `confirmed_default_ids`, or pass every value explicitly. Keep `confirm_defaults: true` only as a compatibility option after the user accepts every listed default. A missing or unconfirmed required value returns `status: needs_user_input` and does not submit `/prompt`.

Before submission, summarize the effective image filenames, prompt, model, dimensions, steps, CFG, and seed policy. Saved defaults require confirmation; values explicitly supplied in the current user request do not require a second confirmation.

## Complex Workflows

Use explicit metadata for workflows containing any of the following:

- more than one sampler or generation branch;
- SDXL dual prompt encoders, regional prompting, or conditioning combiners;
- upscalers, detailers, ControlNet, IPAdapter, or video nodes;
- custom prompt nodes whose class type is not `CLIPTextEncode`;
- multiple image sizes or seeds serving different stages.

The automatic installer maps direct positive/negative `CLIPTextEncode` references from the first sampler it can identify and maps common fields on common core nodes. Treat generated mappings as a draft when warnings are present.

For a field not represented by a public parameter, pass an exact override:

```json
{
  "extra_inputs": "{\"42\": {\"image\": \"input.png\", \"strength\": 0.65}}"
}
```

## Runtime Validation

`validate_workflow` checks:

- workflow and metadata structure;
- node class types against live `/object_info`;
- known model selector values such as `ckpt_name`, `lora_name`, `vae_name`, and `control_net_name`;
- presence of a known output node.

An unavailable custom node is not fixed by rewriting JSON. Install the matching custom-node package and its Python dependencies into the target ComfyUI environment, restart ComfyUI, then validate again.

An unavailable model should be installed under the matching ComfyUI model directory or changed in the workflow/metadata. Do not substitute a model silently because model architecture and node inputs may be incompatible.
