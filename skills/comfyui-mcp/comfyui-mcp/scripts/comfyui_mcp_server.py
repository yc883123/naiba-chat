#!/usr/bin/env python3
"""Expose a running ComfyUI instance as validated MCP tools."""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from mcp.server.fastmcp import FastMCP
try:
    from mcp.types import ToolAnnotations
except ImportError:  # 兼容较旧的 MCP SDK
    from mcp.server.models import ToolAnnotations

# 查询类工具标记为只读（readOnlyHint），run_workflow 等保持有副作用（不标记）。
_READONLY = ToolAnnotations(readOnlyHint=True)


COMFYUI_URL = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")
COMFYUI_ROOT = os.getenv("COMFYUI_ROOT", "")
WORKFLOWS_DIR = Path(
    os.getenv("COMFYUI_WORKFLOWS_DIR", Path(__file__).resolve().parent.parent / "workflows")
).resolve()
TIMEOUT = int(os.getenv("COMFYUI_TIMEOUT", "300"))
OUTPUT_DIR = Path(
    os.getenv("COMFYUI_MCP_OUTPUT_DIR", Path(__file__).resolve().parent.parent / "output")
).resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

PUBLIC_PARAMETERS = {
    "prompt",
    "negative_prompt",
    "width",
    "height",
    "steps",
    "cfg",
    "seed",
    "denoise",
    "batch_size",
    "model",
}
MODEL_KINDS = {
    "checkpoint": ("CheckpointLoaderSimple", "ckpt_name"),
    "lora": ("LoraLoader", "lora_name"),
    "vae": ("VAELoader", "vae_name"),
    "controlnet": ("ControlNetLoader", "control_net_name"),
}

mcp = FastMCP("comfyui")


def _get_json(path: str):
    with urllib.request.urlopen(f"{COMFYUI_URL}{path}", timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(path: str, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{COMFYUI_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ComfyUI returned HTTP {exc.code}: {detail}") from exc


def _download(file_record: dict, destination: Path) -> str:
    query = urllib.parse.urlencode(
        {
            "filename": file_record["filename"],
            "subfolder": file_record.get("subfolder", ""),
            "type": file_record.get("type", "output"),
        }
    )
    with urllib.request.urlopen(f"{COMFYUI_URL}/view?{query}", timeout=60) as response:
        destination.write_bytes(response.read())
    return str(destination)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _workflow_format(data: object) -> str:
    if isinstance(data, dict) and isinstance(data.get("nodes"), list):
        return "ui"
    if isinstance(data, dict) and data and all(
        isinstance(node, dict)
        and isinstance(node.get("class_type"), str)
        and isinstance(node.get("inputs"), dict)
        for node in data.values()
    ):
        return "api"
    return "invalid"


def _metadata_path(workflow_path: Path) -> Path:
    return workflow_path.with_name(f"{workflow_path.stem}.meta.json")


def _normalize_parameter_map(metadata: dict | None) -> tuple[dict[str, list[dict]], list[str]]:
    if metadata is None:
        return {}, []
    errors: list[str] = []
    if metadata.get("schema_version") != 1:
        errors.append("metadata.schema_version must be 1")
    raw_map = metadata.get("parameter_map", {})
    if not isinstance(raw_map, dict):
        return {}, errors + ["metadata.parameter_map must be an object"]

    normalized: dict[str, list[dict]] = {}
    for name, raw_bindings in raw_map.items():
        if name not in PUBLIC_PARAMETERS:
            errors.append(f"unsupported public parameter: {name}")
            continue
        bindings = raw_bindings if isinstance(raw_bindings, list) else [raw_bindings]
        if not bindings:
            errors.append(f"parameter {name} has no bindings")
            continue
        normalized[name] = []
        for binding in bindings:
            if not isinstance(binding, dict):
                errors.append(f"parameter {name} contains a non-object binding")
                continue
            node_id = binding.get("node_id")
            input_name = binding.get("input")
            if node_id is None or not isinstance(input_name, str) or not input_name:
                errors.append(f"parameter {name} binding requires node_id and input")
                continue
            normalized[name].append({"node_id": str(node_id), "input": input_name})
    return normalized, errors


def _validate_bindings(graph: dict, parameter_map: dict[str, list[dict]]) -> list[str]:
    errors: list[str] = []
    for name, bindings in parameter_map.items():
        for binding in bindings:
            node_id = binding["node_id"]
            input_name = binding["input"]
            if node_id not in graph:
                errors.append(f"parameter {name} references missing node {node_id}")
            elif input_name not in graph[node_id]["inputs"]:
                errors.append(f"parameter {name} references missing input {node_id}.{input_name}")
    return errors


def _normalize_requirements(
    graph: dict,
    metadata: dict | None,
    parameter_map: dict[str, list[dict]],
) -> tuple[list[dict], list[str]]:
    """Normalize user-facing input requirements and infer safe basics for API graphs."""
    raw_requirements = metadata.get("input_requirements") if metadata else None
    if raw_requirements is None:
        raw_requirements = []
        for node_id, node in graph.items():
            if node.get("class_type") == "LoadImage" and "image" in node.get("inputs", {}):
                raw_requirements.append(
                    {
                        "id": f"image_{node_id}",
                        "label": f"Input image (node {node_id})",
                        "type": "image",
                        "node_id": str(node_id),
                        "input": "image",
                        "required": True,
                        "confirm_default": True,
                    }
                )
        if "prompt" in parameter_map and parameter_map["prompt"]:
            binding = parameter_map["prompt"][0]
            raw_requirements.append(
                {
                    "id": "prompt",
                    "label": "Prompt",
                    "type": "text",
                    "public_parameter": "prompt",
                    "node_id": binding["node_id"],
                    "input": binding["input"],
                    "required": True,
                    "confirm_default": True,
                }
            )

    if not isinstance(raw_requirements, list):
        return [], ["metadata.input_requirements must be an array"]

    requirements: list[dict] = []
    errors: list[str] = []
    requirement_ids: set[str] = set()
    for index, raw in enumerate(raw_requirements):
        if not isinstance(raw, dict):
            errors.append(f"input_requirements[{index}] must be an object")
            continue
        requirement_id = raw.get("id")
        node_id = raw.get("node_id")
        input_name = raw.get("input")
        public_parameter = raw.get("public_parameter")
        if not requirement_id or node_id is None or not isinstance(input_name, str) or not input_name:
            errors.append(f"input_requirements[{index}] requires id, node_id, and input")
            continue
        if public_parameter is not None and public_parameter not in PUBLIC_PARAMETERS:
            errors.append(f"input requirement {requirement_id} has unsupported public_parameter {public_parameter}")
            continue
        requirement_id = str(requirement_id)
        if requirement_id in requirement_ids:
            errors.append(f"duplicate input requirement id: {requirement_id}")
            continue
        requirement_ids.add(requirement_id)
        node_id = str(node_id)
        if node_id not in graph:
            errors.append(f"input requirement {requirement_id} references missing node {node_id}")
            continue
        if input_name not in graph[node_id].get("inputs", {}):
            errors.append(f"input requirement {requirement_id} references missing input {node_id}.{input_name}")
            continue
        requirements.append(
            {
                "id": requirement_id,
                "label": str(raw.get("label", requirement_id)),
                "type": str(raw.get("type", "text")),
                "description": str(raw.get("description", "")),
                "public_parameter": public_parameter,
                "node_id": node_id,
                "input": input_name,
                "required": bool(raw.get("required", True)),
                "confirm_default": bool(raw.get("confirm_default", True)),
            }
        )
    return requirements, errors


def _parse_confirmed_default_ids(raw_value: str, requirements: list[dict]) -> set[str]:
    try:
        values = json.loads(raw_value) if raw_value else []
    except json.JSONDecodeError as exc:
        raise ValueError("confirmed_default_ids must be a JSON array of requirement IDs") from exc
    if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
        raise ValueError("confirmed_default_ids must be a JSON array of non-empty requirement IDs")

    confirmed_ids = set(values)
    confirmable_ids = {item["id"] for item in requirements if item["confirm_default"]}
    unknown_ids = sorted(confirmed_ids - confirmable_ids)
    if unknown_ids:
        raise ValueError(
            "confirmed_default_ids contains unknown or non-confirmable IDs: " + ", ".join(unknown_ids)
        )
    return confirmed_ids


def _requirement_value(requirement: dict, graph: dict, params: dict, extra_inputs: dict) -> tuple[object, bool]:
    """Return effective value and whether the caller explicitly supplied it."""
    public_parameter = requirement.get("public_parameter")
    if public_parameter and params.get(public_parameter) is not None:
        return params[public_parameter], True
    node_id = requirement["node_id"]
    input_name = requirement["input"]
    if node_id in extra_inputs and input_name in extra_inputs[node_id]:
        return extra_inputs[node_id][input_name], True
    return graph[node_id]["inputs"].get(input_name), False


def _input_file_available(value: object) -> bool | None:
    if not isinstance(value, str) or not value:
        return False
    normalized = value.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(part == ".." for part in parts):
        return False
    filename = parts[-1]
    subfolder = "/".join(parts[:-1])

    if COMFYUI_ROOT:
        input_root = (Path(COMFYUI_ROOT).resolve() / "input").resolve()
        candidate = (input_root / Path(*parts)).resolve()
        try:
            candidate.relative_to(input_root)
        except ValueError:
            return False
        if candidate.is_file():
            return True

    query = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": "input"})
    try:
        with urllib.request.urlopen(f"{COMFYUI_URL}/view?{query}", timeout=5) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return None


def _preflight_requirements(
    graph: dict,
    requirements: list[dict],
    params: dict,
    extra_inputs: dict,
    confirm_defaults: bool = False,
    confirmed_default_ids: set[str] | None = None,
) -> dict:
    confirmed_default_ids = confirmed_default_ids or set()
    missing: list[dict] = []
    unconfirmed: list[dict] = []
    visible: list[dict] = []
    for requirement in requirements:
        value, explicit = _requirement_value(requirement, graph, params, extra_inputs)
        is_empty = value is None or value == ""
        item = {
            **requirement,
            "current_value": value,
            "explicitly_supplied": explicit,
        }
        if requirement["type"] == "image":
            item["available_in_comfyui_input"] = _input_file_available(value)
        visible.append(item)
        unavailable_file = requirement["type"] == "image" and item.get("available_in_comfyui_input") is False
        if requirement["required"] and (is_empty or unavailable_file):
            if unavailable_file:
                item["problem"] = "The image is not available under ComfyUI/input."
            missing.append(item)
        elif (
            requirement["confirm_default"]
            and not explicit
            and not confirm_defaults
            and requirement["id"] not in confirmed_default_ids
        ):
            unconfirmed.append(item)

    if missing or unconfirmed:
        ask_items = missing + unconfirmed
        labels = "、".join(item["label"] for item in ask_items)
        return {
            "status": "needs_user_input",
            "requires_user_input": True,
            "requirements": visible,
            "missing_inputs": missing,
            "unconfirmed_defaults": unconfirmed,
            "question": f"运行此工作流前，请提供或确认：{labels}。",
        }
    return {
        "status": "ready",
        "requires_user_input": False,
        "requirements": visible,
        "missing_inputs": [],
        "unconfirmed_defaults": [],
    }


def _resolve_workflow_path(name: str) -> Path:
    supplied = Path(name)
    if supplied.name != name or name in {"", ".", ".."}:
        raise ValueError("workflow_name must be a filename from the configured workflows directory")
    filename = name if name.lower().endswith(".json") else f"{name}.json"
    if filename.lower().endswith(".meta.json"):
        raise ValueError("metadata sidecars cannot be executed as workflows")
    path = WORKFLOWS_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"Workflow does not exist: {filename} (directory: {WORKFLOWS_DIR})")
    return path


def _inspect_workflow_path(path: Path) -> dict:
    result = {"name": path.stem, "file": path.name}
    try:
        graph = _read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return {**result, "status": "invalid", "errors": [str(exc)]}
    kind = _workflow_format(graph)
    if kind == "ui":
        return {
            **result,
            "status": "needs_api_export",
            "format": "ui",
            "reason": "Export this canvas workflow from ComfyUI using Save/Export (API Format).",
        }
    if kind != "api":
        return {
            **result,
            "status": "invalid",
            "format": "unknown",
            "errors": ["Expected a non-empty {node_id: {class_type, inputs}} API graph."],
        }

    meta_path = _metadata_path(path)
    metadata = None
    if meta_path.is_file():
        try:
            metadata = _read_json(meta_path)
        except (OSError, json.JSONDecodeError) as exc:
            return {**result, "status": "invalid_metadata", "format": "api", "errors": [str(exc)]}
    parameter_map, errors = _normalize_parameter_map(metadata)
    errors.extend(_validate_bindings(graph, parameter_map))
    requirements, requirement_errors = _normalize_requirements(graph, metadata, parameter_map)
    errors.extend(requirement_errors)
    if errors:
        return {**result, "status": "invalid_metadata", "format": "api", "errors": errors}
    return {
        **result,
        "status": "ready",
        "format": "api",
        "parameter_mode": "explicit" if metadata is not None else "inferred",
        "supported_parameters": sorted(parameter_map) if metadata is not None else sorted(PUBLIC_PARAMETERS),
        "node_count": len(graph),
        "metadata": meta_path.name if metadata is not None else None,
        "input_requirements": [
            {
                "id": item["id"],
                "label": item["label"],
                "type": item["type"],
                "required": item["required"],
            }
            for item in requirements
        ],
    }


def _load_workflow(name: str) -> tuple[dict, dict[str, list[dict]], list[dict]]:
    path = _resolve_workflow_path(name)
    inspection = _inspect_workflow_path(path)
    if inspection["status"] != "ready":
        raise ValueError(json.dumps(inspection, ensure_ascii=False))
    graph = _read_json(path)
    meta_path = _metadata_path(path)
    metadata = _read_json(meta_path) if meta_path.is_file() else None
    parameter_map, _ = _normalize_parameter_map(metadata)
    requirements, requirement_errors = _normalize_requirements(graph, metadata, parameter_map)
    if requirement_errors:
        raise ValueError("; ".join(requirement_errors))
    return graph, parameter_map, requirements


def _load_inline_workflow(workflow_json: str) -> dict:
    graph = json.loads(workflow_json)
    kind = _workflow_format(graph)
    if kind == "ui":
        raise ValueError("UI workflow JSON is not executable; export it from ComfyUI in API format")
    if kind != "api":
        raise ValueError("workflow_json must be a non-empty ComfyUI API-format graph")
    return graph


def _direct_node_id(value: object) -> str | None:
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], (str, int)):
        return str(value[0])
    return None


def _inferred_parameter_map(graph: dict) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    nodes_by_class: dict[str, list[str]] = {}
    for node_id, node in graph.items():
        nodes_by_class.setdefault(node["class_type"], []).append(str(node_id))

    samplers = nodes_by_class.get("KSampler", []) + nodes_by_class.get("KSamplerAdvanced", [])
    if samplers:
        sampler_id = samplers[0]
        inputs = graph[sampler_id]["inputs"]
        for public_name, input_name in (
            ("seed", "seed" if "seed" in inputs else "noise_seed"),
            ("steps", "steps"),
            ("cfg", "cfg"),
            ("denoise", "denoise"),
        ):
            if input_name in inputs:
                result[public_name] = [{"node_id": sampler_id, "input": input_name}]
        for public_name, sampler_field in (("prompt", "positive"), ("negative_prompt", "negative")):
            node_id = _direct_node_id(inputs.get(sampler_field))
            if (
                node_id in graph
                and graph[node_id]["class_type"] == "CLIPTextEncode"
                and "text" in graph[node_id]["inputs"]
            ):
                result[public_name] = [{"node_id": node_id, "input": "text"}]

    clip_nodes = nodes_by_class.get("CLIPTextEncode", [])
    if "prompt" not in result and clip_nodes:
        result["prompt"] = [{"node_id": clip_nodes[0], "input": "text"}]
    if "negative_prompt" not in result and len(clip_nodes) > 1:
        result["negative_prompt"] = [{"node_id": clip_nodes[1], "input": "text"}]

    for node_id in nodes_by_class.get("EmptyLatentImage", []):
        inputs = graph[node_id]["inputs"]
        for field in ("width", "height", "batch_size"):
            if field in inputs:
                result.setdefault(field, []).append({"node_id": node_id, "input": field})
    for node_id in nodes_by_class.get("CheckpointLoaderSimple", []):
        if "ckpt_name" in graph[node_id]["inputs"]:
            result.setdefault("model", []).append({"node_id": node_id, "input": "ckpt_name"})
    return result


def _apply_params(
    graph: dict,
    params: dict,
    parameter_map: dict[str, list[dict]],
    extra_inputs: dict,
) -> None:
    inferred_map = _inferred_parameter_map(graph)
    for name, value in params.items():
        if value is None:
            continue
        bindings = parameter_map.get(name, inferred_map.get(name, []))
        for binding in bindings:
            graph[binding["node_id"]]["inputs"][binding["input"]] = value

    if not isinstance(extra_inputs, dict):
        raise ValueError("extra_inputs must decode to an object")
    for raw_node_id, fields in extra_inputs.items():
        node_id = str(raw_node_id)
        if node_id not in graph:
            raise ValueError(f"extra_inputs references missing node {node_id}")
        if not isinstance(fields, dict):
            raise ValueError(f"extra_inputs value for node {node_id} must be an object")
        graph[node_id]["inputs"].update(fields)


def _live_validation(graph: dict, parameter_map: dict[str, list[dict]]) -> dict:
    metadata_errors = _validate_bindings(graph, parameter_map)
    object_info = _get_json("/object_info")
    class_types = sorted({node["class_type"] for node in graph.values()})
    missing_node_types = [class_type for class_type in class_types if class_type not in object_info]
    missing_assets: list[dict] = []
    output_nodes: list[dict] = []

    for node_id, node in graph.items():
        class_type = node["class_type"]
        definition = object_info.get(class_type, {})
        if definition.get("output_node"):
            output_nodes.append({"node_id": str(node_id), "class_type": class_type})
        input_spec = definition.get("input", {})
        available_inputs = {**input_spec.get("required", {}), **input_spec.get("optional", {})}
        for input_name, value in node["inputs"].items():
            spec = available_inputs.get(input_name)
            if not spec or not isinstance(value, str):
                continue
            options = spec[0] if isinstance(spec, list) and spec else None
            if isinstance(options, list) and value not in options:
                missing_assets.append(
                    {
                        "node_id": str(node_id),
                        "class_type": class_type,
                        "input": input_name,
                        "value": value,
                    }
                )

    issues = []
    if metadata_errors:
        issues.extend(metadata_errors)
    if missing_node_types:
        issues.append("The target ComfyUI instance is missing required node types.")
    if missing_assets:
        issues.append("The target ComfyUI instance is missing referenced selectable assets.")
    if not output_nodes:
        issues.append("No output node recognized by the target ComfyUI instance is present.")
    return {
        "ready": not issues,
        "issues": issues,
        "missing_node_types": missing_node_types,
        "missing_assets": missing_assets,
        "output_nodes": output_nodes,
        "node_count": len(graph),
    }


def _file_url(file_record: dict) -> str:
    query = urllib.parse.urlencode(
        {
            "filename": file_record["filename"],
            "subfolder": file_record.get("subfolder", ""),
            "type": file_record.get("type", "output"),
        }
    )
    return f"{COMFYUI_URL}/view?{query}"


def _collect_artifacts(prompt_id: str, outputs: dict) -> list[dict]:
    artifacts: list[dict] = []
    for node_id, node_output in outputs.items():
        if not isinstance(node_output, dict):
            continue
        for output_name, records in node_output.items():
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict) or "filename" not in record:
                    continue
                filename = Path(str(record["filename"])).name
                destination = OUTPUT_DIR / f"{prompt_id}_{filename}"
                artifact = {
                    "node_id": str(node_id),
                    "kind": output_name,
                    "filename": record["filename"],
                    "url": _file_url(record),
                }
                try:
                    artifact["local_path"] = _download(record, destination)
                except Exception as exc:
                    artifact["download_error"] = str(exc)
                artifacts.append(artifact)
    return artifacts


@mcp.tool(annotations=_READONLY)
def get_environment() -> str:
    """Return effective paths, interpreter, URL, and ComfyUI reachability."""
    reachable = False
    detail = None
    try:
        _get_json("/system_stats")
        reachable = True
    except Exception as exc:
        detail = str(exc)
    root = Path(COMFYUI_ROOT).resolve() if COMFYUI_ROOT else None
    result = {
        "python_executable": sys.executable,
        "comfyui_root": str(root) if root else None,
        "comfyui_root_valid": bool(root and (root / "main.py").is_file()),
        "comfyui_url": COMFYUI_URL,
        "comfyui_reachable": reachable,
        "connection_error": detail,
        "workflows_dir": str(WORKFLOWS_DIR),
        "workflows_dir_exists": WORKFLOWS_DIR.is_dir(),
        "output_dir": str(OUTPUT_DIR),
        "timeout_seconds": TIMEOUT,
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool(annotations=_READONLY)
def list_models(kind: str = "", query: str = "", limit: int = 50, offset: int = 0) -> str:
    """Search one model kind with pagination; omit filters only for the legacy full inventory."""
    try:
        normalized_kind = kind.strip().casefold().replace("-", "_")
        if normalized_kind == "control_net":
            normalized_kind = "controlnet"
        if not normalized_kind:
            if query or limit != 50 or offset != 0:
                return json.dumps(
                    {"error": "kind is required when query, limit, or offset is supplied"},
                    ensure_ascii=False,
                )
        elif normalized_kind not in MODEL_KINDS:
            return json.dumps(
                {
                    "error": f"Unsupported model kind: {kind}",
                    "allowed_kinds": sorted(MODEL_KINDS),
                },
                ensure_ascii=False,
            )
        if not 1 <= limit <= 200:
            return json.dumps({"error": "limit must be between 1 and 200"}, ensure_ascii=False)
        if offset < 0:
            return json.dumps({"error": "offset must be zero or greater"}, ensure_ascii=False)

        object_info = _get_json("/object_info")
        if not normalized_kind:
            result = {}
            for class_type, input_name in MODEL_KINDS.values():
                spec = object_info.get(class_type, {}).get("input", {}).get("required", {}).get(input_name)
                options = spec[0] if isinstance(spec, list) and spec else []
                result[class_type] = options if isinstance(options, list) else []
            return json.dumps(result, ensure_ascii=False, indent=2)

        class_type, input_name = MODEL_KINDS[normalized_kind]
        spec = object_info.get(class_type, {}).get("input", {}).get("required", {}).get(input_name)
        options = spec[0] if isinstance(spec, list) and spec else []
        items = options if isinstance(options, list) else []
        normalized_query = query.strip().casefold()
        if normalized_query:
            items = [item for item in items if normalized_query in str(item).casefold()]
        total = len(items)
        page = items[offset : offset + limit]
        result = {
            "kind": normalized_kind,
            "query": query.strip(),
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": page,
            "truncated": offset + len(page) < total,
        }
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    except Exception as exc:
        return json.dumps({"error": f"Cannot query ComfyUI at {COMFYUI_URL}: {exc}"}, ensure_ascii=False)


@mcp.tool(annotations=_READONLY)
def list_workflows() -> str:
    """Classify workflow files as ready, needing API export, or invalid."""
    if not WORKFLOWS_DIR.is_dir():
        return json.dumps({"workflows": [], "dir": str(WORKFLOWS_DIR)}, ensure_ascii=False, indent=2)
    paths = sorted(
        (path for path in WORKFLOWS_DIR.glob("*.json") if not path.name.lower().endswith(".meta.json")),
        key=lambda path: path.name.lower(),
    )
    workflows = [_inspect_workflow_path(path) for path in paths]
    return json.dumps(
        {
            "workflows": workflows,
            "dir": str(WORKFLOWS_DIR),
            "ready_count": sum(item["status"] == "ready" for item in workflows),
            "next_step": "Call validate_workflow for a ready entry before its first run on this ComfyUI instance.",
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(annotations=_READONLY)
def get_workflow_requirements(workflow_name: str) -> str:
    """Show required files, prompts, defaults, and parameters before queuing a workflow."""
    try:
        graph, parameter_map, requirements = _load_workflow(workflow_name)
        params = {name: None for name in PUBLIC_PARAMETERS}
        preflight = _preflight_requirements(graph, requirements, params, {}, confirm_defaults=False)
        current_parameters = {}
        for name, bindings in parameter_map.items():
            if bindings:
                binding = bindings[0]
                current_parameters[name] = graph[binding["node_id"]]["inputs"].get(binding["input"])
        return json.dumps(
            {
                "workflow_name": workflow_name,
                "status": preflight["status"],
                "question": preflight.get("question"),
                "requirements": preflight["requirements"],
                "current_parameters": current_parameters,
                "parameter_map": parameter_map,
                "hint": "Ask the user to provide or confirm every required/default item, then call run_workflow.",
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        return json.dumps(
            {"status": "error", "workflow_name": workflow_name, "error": str(exc)},
            ensure_ascii=False,
            indent=2,
        )


@mcp.tool(annotations=_READONLY)
def validate_workflow(workflow_name: str) -> str:
    """Validate one installed workflow against live ComfyUI nodes and model assets."""
    try:
        graph, parameter_map, requirements = _load_workflow(workflow_name)
        result = _live_validation(graph, parameter_map)
        result["workflow_name"] = workflow_name
        result["input_requirements"] = requirements
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps(
            {"ready": False, "workflow_name": workflow_name, "issues": [str(exc)]},
            ensure_ascii=False,
            indent=2,
        )


@mcp.tool()
def run_workflow(
    workflow_name: str = "",
    workflow_json: str = "",
    prompt: str = "",
    negative_prompt: str = "",
    width: int = 0,
    height: int = 0,
    steps: int = 0,
    cfg: float = 0.0,
    seed: int = -1,
    denoise: float = 0.0,
    batch_size: int = 0,
    model: str = "",
    extra_inputs: str = "{}",
    confirm_defaults: bool = False,
    confirmed_default_ids: str = "[]",
) -> str:
    """Run a validated API-format workflow after explicit or per-requirement confirmation."""
    try:
        if workflow_json:
            graph = _load_inline_workflow(workflow_json)
            parameter_map = _inferred_parameter_map(graph)
            requirements, requirement_errors = _normalize_requirements(graph, None, parameter_map)
            if requirement_errors:
                raise ValueError("; ".join(requirement_errors))
        elif workflow_name:
            graph, parameter_map, requirements = _load_workflow(workflow_name)
        else:
            raise ValueError("Provide workflow_name or workflow_json")

        params = {
            "prompt": prompt if prompt else None,
            "negative_prompt": negative_prompt if negative_prompt else None,
            "width": width if width > 0 else None,
            "height": height if height > 0 else None,
            "steps": steps if steps > 0 else None,
            "cfg": cfg if cfg > 0 else None,
            "seed": seed if seed >= 0 else None,
            "denoise": denoise if denoise > 0 else None,
            "batch_size": batch_size if batch_size > 0 else None,
            "model": model if model else None,
        }
        parsed_extra_inputs = json.loads(extra_inputs) if extra_inputs else {}
        if not isinstance(parsed_extra_inputs, dict):
            raise ValueError("extra_inputs must decode to an object")
        parsed_confirmed_default_ids = _parse_confirmed_default_ids(confirmed_default_ids, requirements)
        preflight = _preflight_requirements(
            graph,
            requirements,
            params,
            parsed_extra_inputs,
            confirm_defaults=confirm_defaults,
            confirmed_default_ids=parsed_confirmed_default_ids,
        )
        if preflight["status"] != "ready":
            return json.dumps(
                {
                    "status": "needs_user_input",
                    "workflow_name": workflow_name or "inline",
                    **preflight,
                    "hint": "Set values explicitly or pass their requirement IDs through confirmed_default_ids. Use legacy confirm_defaults=true only after every listed default is accepted.",
                },
                ensure_ascii=False,
                indent=2,
            )
        _apply_params(graph, params, parameter_map, parsed_extra_inputs)

        response = _post_json("/prompt", {"prompt": graph, "client_id": str(uuid.uuid4())})
        prompt_id = response.get("prompt_id")
        if not prompt_id:
            raise RuntimeError(f"ComfyUI did not return prompt_id: {response}")

        started = time.time()
        while time.time() - started < TIMEOUT:
            try:
                history = _get_json(f"/history/{prompt_id}")
            except urllib.error.HTTPError:
                history = {}
            if prompt_id in history:
                record = history[prompt_id]
                status_record = record.get("status", {})
                if status_record.get("status_str") == "error":
                    return json.dumps(
                        {
                            "prompt_id": prompt_id,
                            "status": "error",
                            "messages": status_record.get("messages", []),
                            "elapsed_sec": round(time.time() - started, 1),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                artifacts = _collect_artifacts(prompt_id, record.get("outputs", {}))
                images = [
                    artifact.get("local_path", artifact["url"])
                    for artifact in artifacts
                    if artifact["kind"] == "images"
                ]
                return json.dumps(
                    {
                        "prompt_id": prompt_id,
                        "status": "success",
                        "artifacts": artifacts,
                        "images": images,
                        "elapsed_sec": round(time.time() - started, 1),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            time.sleep(1.5)

        return json.dumps(
            {"prompt_id": prompt_id, "status": "timeout", "elapsed_sec": TIMEOUT},
            ensure_ascii=False,
        )
    except Exception as exc:
        return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2)


@mcp.tool(annotations=_READONLY)
def get_image(prompt_id: str) -> str:
    """Return image URLs from ComfyUI history for a known prompt_id."""
    try:
        history = _get_json(f"/history/{prompt_id}")
        if prompt_id not in history:
            return json.dumps({"status": "not_ready", "prompt_id": prompt_id}, ensure_ascii=False)
        artifacts = _collect_artifacts(prompt_id, history[prompt_id].get("outputs", {}))
        images = [artifact.get("local_path", artifact["url"]) for artifact in artifacts if artifact["kind"] == "images"]
        return json.dumps({"prompt_id": prompt_id, "images": images}, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
