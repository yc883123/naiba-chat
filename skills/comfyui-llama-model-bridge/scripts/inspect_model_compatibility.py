#!/usr/bin/env python3
"""Read-only GGUF inventory and local runtime capability inspection."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any


PROJECTOR_MARKERS = (
    "mmproj",
    "projector",
    "vision-proj",
    "vision_proj",
    "vision.projector",
)
IGNORED_PAIR_TOKENS = {
    "gguf",
    "mmproj",
    "model",
    "vision",
    "projector",
    "adapter",
    "f16",
    "f32",
    "bf16",
    "fp16",
    "fp32",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "只读盘点 GGUF 主模型与视觉投影，并可选探测 llama.cpp、"
            "Ollama、LM Studio 的本地接口。"
        )
    )
    parser.add_argument("--root", required=True, help="ComfyUI/共享模型根目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--llama-url", help="llama.cpp 地址，例如 http://127.0.0.1:8080")
    parser.add_argument("--ollama-url", help="Ollama 地址，例如 http://127.0.0.1:11434")
    parser.add_argument("--lmstudio-url", help="LM Studio 地址，例如 http://127.0.0.1:1234")
    parser.add_argument("--timeout", type=float, default=3.0, help="接口超时秒数，默认 3")
    return parser.parse_args()


def is_projector(path: Path) -> bool:
    name = path.name.lower()
    return any(marker in name for marker in PROJECTOR_MARKERS)


def model_tokens(path: Path) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", path.stem.lower()))
    return {
        token
        for token in tokens
        if token not in IGNORED_PAIR_TOKENS
        and not re.fullmatch(r"(?:i?q\d+(?:_[a-z0-9]+)?|q\d+k?|k_[a-z]+)", token)
    }


def file_record(path: Path, root: Path) -> dict[str, Any]:
    stat = path.stat()
    try:
        relative = str(path.relative_to(root))
    except ValueError:
        relative = str(path)
    return {
        "path": str(path),
        "relative_path": relative,
        "name": path.name,
        "size": stat.st_size,
        "link_count": getattr(stat, "st_nlink", 1),
        "device": getattr(stat, "st_dev", 0),
        "inode": getattr(stat, "st_ino", 0),
    }


def pair_projectors(
    models: list[dict[str, Any]], projectors: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for projector in projectors:
        projector_path = Path(projector["path"])
        projector_tokens = model_tokens(projector_path)
        ranked: list[tuple[float, dict[str, Any], list[str]]] = []
        for model in models:
            model_path = Path(model["path"])
            tokens = model_tokens(model_path)
            common = sorted(projector_tokens & tokens)
            union = projector_tokens | tokens
            score = len(common) / len(union) if union else 0.0
            if projector_path.parent == model_path.parent:
                score += 0.20
            if common or projector_path.parent == model_path.parent:
                ranked.append((score, model, common))
        ranked.sort(key=lambda item: (-item[0], item[1]["path"].lower()))
        pairs.append(
            {
                "projector": projector["path"],
                "suggestions": [
                    {
                        "model": model["path"],
                        "score": round(score, 3),
                        "shared_tokens": common,
                    }
                    for score, model, common in ranked[:3]
                ],
                "warning": (
                    "文件名只能用于候选配对，必须由模型元数据或真实图片请求验证。"
                ),
            }
        )
    return pairs


def grouped_paths(
    records: list[dict[str, Any]], key_names: tuple[str, ...]
) -> list[list[str]]:
    groups: dict[tuple[Any, ...], list[str]] = defaultdict(list)
    for record in records:
        key = tuple(record[name] for name in key_names)
        if all(key):
            groups[key].append(record["path"])
    return [sorted(paths) for paths in groups.values() if len(paths) > 1]


def duplicate_candidates(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[(record["name"].lower(), record["size"])].append(record)
    result = []
    for (name, size), items in groups.items():
        paths = sorted(item["path"] for item in items)
        if len(paths) > 1:
            result.append({"name": name, "size": size, "paths": paths})
    return sorted(result, key=lambda item: (item["name"], item["size"]))


def inventory(root: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item).lower()):
        if not path.is_file() or path.suffix.lower() != ".gguf":
            continue
        try:
            records.append(file_record(path.resolve(), root.resolve()))
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})

    models = [record for record in records if not is_projector(Path(record["path"]))]
    projectors = [record for record in records if is_projector(Path(record["path"]))]
    return {
        "root": str(root.resolve()),
        "models": models,
        "projectors": projectors,
        "projector_pair_suggestions": pair_projectors(models, projectors),
        "hardlink_groups": grouped_paths(records, ("device", "inode")),
        "duplicate_candidates": duplicate_candidates(records),
        "scan_errors": errors,
    }


def endpoint(base_url: str, path: str) -> str:
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def request_json(
    url: str, timeout: float, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            parsed = json.loads(body) if body else None
            return {"ok": True, "status": response.status, "data": parsed}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "status": exc.code, "error": body[:500]}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def probe_runtimes(args: argparse.Namespace) -> dict[str, Any]:
    runtimes: dict[str, Any] = {}
    if args.llama_url:
        runtimes["llama.cpp"] = request_json(
            endpoint(args.llama_url, "/props"), args.timeout
        )
    if args.lmstudio_url:
        runtimes["lm_studio"] = request_json(
            endpoint(args.lmstudio_url, "/api/v1/models"), args.timeout
        )
    if args.ollama_url:
        tags = request_json(endpoint(args.ollama_url, "/api/tags"), args.timeout)
        ollama: dict[str, Any] = {"tags": tags, "models": []}
        if tags.get("ok") and isinstance(tags.get("data"), dict):
            models = tags["data"].get("models", [])
            for model in models:
                name = model.get("name") or model.get("model")
                if not name:
                    continue
                shown = request_json(
                    endpoint(args.ollama_url, "/api/show"),
                    args.timeout,
                    {"model": name},
                )
                ollama["models"].append({"name": name, "show": shown})
        runtimes["ollama"] = ollama
    return runtimes


def format_size(size: int) -> str:
    value = float(size)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def print_human(report: dict[str, Any]) -> None:
    print(f"模型根目录: {report['root']}")
    print(f"主模型: {len(report['models'])}，视觉投影: {len(report['projectors'])}")
    for label, key in (("主模型", "models"), ("视觉投影", "projectors")):
        print(f"\n[{label}]")
        if not report[key]:
            print("  未发现")
        for record in report[key]:
            links = f"，硬链接数 {record['link_count']}" if record["link_count"] > 1 else ""
            print(f"  - {record['relative_path']} ({format_size(record['size'])}{links})")

    print("\n[视觉投影候选配对]")
    if not report["projector_pair_suggestions"]:
        print("  无视觉投影文件")
    for pair in report["projector_pair_suggestions"]:
        print(f"  - {pair['projector']}")
        if not pair["suggestions"]:
            print("    未找到候选主模型")
        for suggestion in pair["suggestions"]:
            print(f"    -> {suggestion['model']} (score={suggestion['score']})")

    print(f"\n硬链接组: {len(report['hardlink_groups'])}")
    print(f"疑似重复文件组: {len(report['duplicate_candidates'])}")
    if report["scan_errors"]:
        print(f"扫描错误: {len(report['scan_errors'])}")

    runtimes = report.get("runtimes", {})
    if runtimes:
        print("\n[运行端探测]")
        for name, result in runtimes.items():
            if name == "ollama":
                tags_ok = result.get("tags", {}).get("ok", False)
                print(f"  - {name}: {'可访问' if tags_ok else '失败'}")
                for model in result.get("models", []):
                    show = model.get("show", {})
                    capabilities = []
                    if show.get("ok") and isinstance(show.get("data"), dict):
                        capabilities = show["data"].get("capabilities", [])
                    print(f"    {model['name']}: capabilities={capabilities}")
            else:
                print(f"  - {name}: {'可访问' if result.get('ok') else '失败'}")

    print("\n提示: 候选配对不是视觉验证结果；请查询能力接口并发送真实小图片验证。")


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser()
    if not root.exists() or not root.is_dir():
        print(f"错误: 模型目录不存在或不是目录: {root}", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("错误: --timeout 必须大于 0", file=sys.stderr)
        return 2

    report = inventory(root)
    report["runtimes"] = probe_runtimes(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_human(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
