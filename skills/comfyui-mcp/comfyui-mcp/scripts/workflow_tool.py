#!/usr/bin/env python3
"""Inspect and install ComfyUI API-format workflows for the MCP server."""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_WORKFLOWS_DIR = SKILL_DIR / "workflows"
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
OUTPUT_CLASS_HINTS = ("SaveImage", "PreviewImage", "VideoCombine", "SaveAudio", "SaveAnimated")


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _format_kind(data: object) -> str:
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


def _sort_key(node_id: str) -> tuple[int, int | str]:
    return (0, int(node_id)) if node_id.isdigit() else (1, node_id)


def _binding(node_id: str, input_name: str) -> dict[str, str]:
    return {"node_id": str(node_id), "input": input_name}


def _direct_node_id(value: object) -> str | None:
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], (str, int)):
        return str(value[0])
    return None


def _detect_parameter_map(graph: dict) -> tuple[dict[str, list[dict[str, str]]], list[str]]:
    parameter_map: dict[str, list[dict[str, str]]] = {}
    warnings: list[str] = []
    by_class: dict[str, list[str]] = {}
    for node_id, node in graph.items():
        by_class.setdefault(node["class_type"], []).append(str(node_id))
    for node_ids in by_class.values():
        node_ids.sort(key=_sort_key)

    samplers = by_class.get("KSampler", []) + by_class.get("KSamplerAdvanced", [])
    samplers.sort(key=_sort_key)
    if len(samplers) > 1:
        warnings.append("Multiple sampler nodes found; generated bindings use only the first sampler.")
    if samplers:
        sampler_id = samplers[0]
        sampler_inputs = graph[sampler_id]["inputs"]
        field_map = {
            "seed": "seed" if "seed" in sampler_inputs else "noise_seed",
            "steps": "steps",
            "cfg": "cfg",
            "denoise": "denoise",
        }
        for public_name, input_name in field_map.items():
            if input_name in sampler_inputs:
                parameter_map[public_name] = [_binding(sampler_id, input_name)]

        for public_name in ("prompt", "negative_prompt"):
            sampler_field = "positive" if public_name == "prompt" else "negative"
            candidate_id = _direct_node_id(sampler_inputs.get(sampler_field))
            if candidate_id and candidate_id in graph:
                candidate = graph[candidate_id]
                if candidate["class_type"] == "CLIPTextEncode" and "text" in candidate["inputs"]:
                    parameter_map[public_name] = [_binding(candidate_id, "text")]

    clip_nodes = by_class.get("CLIPTextEncode", [])
    if "prompt" not in parameter_map and clip_nodes:
        parameter_map["prompt"] = [_binding(clip_nodes[0], "text")]
        warnings.append("Positive prompt was inferred from CLIPTextEncode order; review the binding.")
    if "negative_prompt" not in parameter_map and len(clip_nodes) > 1:
        parameter_map["negative_prompt"] = [_binding(clip_nodes[1], "text")]
        warnings.append("Negative prompt was inferred from CLIPTextEncode order; review the binding.")

    latents = by_class.get("EmptyLatentImage", [])
    if len(latents) > 1:
        warnings.append("Multiple EmptyLatentImage nodes found; generated bindings use only the first.")
    if latents:
        latent_id = latents[0]
        for field in ("width", "height", "batch_size"):
            if field in graph[latent_id]["inputs"]:
                parameter_map[field] = [_binding(latent_id, field)]

    loaders = by_class.get("CheckpointLoaderSimple", [])
    if len(loaders) > 1:
        warnings.append("Multiple checkpoint loaders found; generated binding uses only the first.")
    if loaders and "ckpt_name" in graph[loaders[0]]["inputs"]:
        parameter_map["model"] = [_binding(loaders[0], "ckpt_name")]

    if not samplers:
        warnings.append("No KSampler or KSamplerAdvanced node was found; sampler parameters were not mapped.")
    else:
        if "prompt" not in parameter_map:
            warnings.append("No prompt binding was inferred; add input_requirements and parameter_map for the workflow's custom prompt node.")
        if "negative_prompt" not in parameter_map:
            warnings.append("No negative prompt binding was inferred; add one only if this workflow exposes a negative prompt.")
    return parameter_map, warnings


def _detect_input_requirements(graph: dict, parameter_map: dict[str, list[dict[str, str]]]) -> list[dict]:
    requirements: list[dict] = []
    for node_id, node in graph.items():
        if node["class_type"] == "LoadImage" and "image" in node["inputs"]:
            requirements.append(
                {
                    "id": f"image_{node_id}",
                    "label": f"Input image (node {node_id})",
                    "type": "image",
                    "node_id": str(node_id),
                    "input": "image",
                    "required": True,
                    "confirm_default": True,
                    "description": "Copy the file into ComfyUI/input and provide its filename.",
                }
            )
    if parameter_map.get("prompt"):
        binding = parameter_map["prompt"][0]
        requirements.append(
            {
                "id": "prompt",
                "label": "Prompt",
                "type": "text",
                "public_parameter": "prompt",
                "node_id": binding["node_id"],
                "input": binding["input"],
                "required": True,
                "confirm_default": True,
                "description": "Ask the user for the prompt instead of silently using the saved text.",
            }
        )
    return requirements


def inspect(path: Path) -> tuple[dict, dict | None]:
    try:
        data = _load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "invalid", "path": str(path), "errors": [str(exc)]}, None

    kind = _format_kind(data)
    if kind == "ui":
        return {
            "status": "needs_api_export",
            "path": str(path),
            "format": "ui",
            "errors": ["UI workflow JSON is not executable. Export it from ComfyUI in API format."],
        }, None
    if kind != "api":
        return {
            "status": "invalid",
            "path": str(path),
            "format": "unknown",
            "errors": ["Expected a non-empty {node_id: {class_type, inputs}} API graph."],
        }, None

    graph = data
    parameter_map, warnings = _detect_parameter_map(graph)
    input_requirements = _detect_input_requirements(graph, parameter_map)
    class_types = sorted({node["class_type"] for node in graph.values()})
    output_nodes = [
        str(node_id)
        for node_id, node in graph.items()
        if any(hint.lower() in node["class_type"].lower() for hint in OUTPUT_CLASS_HINTS)
    ]
    if not output_nodes:
        warnings.append("No recognized output node was found; the run may complete without downloadable files.")
    metadata = {
        "schema_version": 1,
        "display_name": path.stem,
        "description": "",
        "parameter_map": parameter_map,
        "input_requirements": input_requirements,
        "inspection": {
            "class_types": class_types,
            "output_nodes": output_nodes,
            "warnings": warnings,
        },
    }
    report = {
        "status": "ready",
        "path": str(path),
        "format": "api",
        "node_count": len(graph),
        "class_types": class_types,
        "output_nodes": output_nodes,
        "detected_parameter_map": parameter_map,
        "input_requirements": input_requirements,
        "warnings": warnings,
    }
    return report, metadata


def _install(args: argparse.Namespace) -> int:
    source = Path(args.workflow).expanduser().resolve()
    report, metadata = inspect(source)
    if report["status"] != "ready" or metadata is None:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    name = args.name or source.stem
    if not NAME_PATTERN.fullmatch(name) or name.endswith(".meta"):
        print(json.dumps({"status": "invalid_name", "name": name}, indent=2))
        return 1

    workflows_dir = Path(args.workflows_dir).expanduser().resolve()
    workflows_dir.mkdir(parents=True, exist_ok=True)
    target = workflows_dir / f"{name}.json"
    meta_target = workflows_dir / f"{name}.meta.json"
    existing = [str(path) for path in (target, meta_target) if path.exists()]
    can_add_sidecar_to_source = source == target and target.exists() and not meta_target.exists()
    if existing and not args.force and not can_add_sidecar_to_source:
        print(json.dumps({"status": "exists", "files": existing, "hint": "Use --force to replace them."}, indent=2))
        return 1

    if source != target:
        shutil.copy2(source, target)
    metadata["display_name"] = args.display_name or name
    metadata["description"] = args.description or ""
    meta_target.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report.update(
        {
            "status": "installed",
            "workflow": str(target),
            "metadata": str(meta_target),
            "next_step": f'Call list_workflows, then validate_workflow with workflow_name="{name}".',
        }
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect", help="Inspect without changing files")
    inspect_parser.add_argument("workflow")
    install_parser = subparsers.add_parser("install", help="Install an API workflow and metadata")
    install_parser.add_argument("workflow")
    install_parser.add_argument("--name")
    install_parser.add_argument("--display-name")
    install_parser.add_argument("--description")
    install_parser.add_argument("--workflows-dir", default=str(DEFAULT_WORKFLOWS_DIR))
    install_parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.command == "inspect":
        report, _ = inspect(Path(args.workflow).expanduser().resolve())
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "ready" else 1
    return _install(args)


if __name__ == "__main__":
    sys.exit(main())
