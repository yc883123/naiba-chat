#!/usr/bin/env python3
"""Validate a ComfyUI installation and print an MCP client configuration."""

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = SKILL_DIR / "scripts" / "comfyui_mcp_server.py"
MCP_REQUIREMENT = "mcp>=1.2.0,<2"


def _resolve_python(value: str | None, comfyui_root: Path | None) -> Path | None:
    if value:
        candidate = Path(value).expanduser()
        if candidate.is_dir():
            windows = candidate / "python.exe"
            posix = candidate / "bin" / "python"
            return windows if windows.exists() else posix
        return candidate

    candidates: list[Path] = []
    if comfyui_root:
        portable_root = comfyui_root.parent
        candidates.extend(
            [
                portable_root / "python_embeded" / "python.exe",
                portable_root / "python_embedded" / "python.exe",
                comfyui_root / "python_embeded" / "python.exe",
                comfyui_root / ".venv" / "Scripts" / "python.exe",
                comfyui_root / ".venv" / "bin" / "python",
            ]
        )
    return next((path for path in candidates if path.is_file()), None)


def _probe_mcp(python_exe: Path) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [str(python_exe), "-c", "from mcp.server.fastmcp import FastMCP"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    detail = (result.stderr or result.stdout).strip()
    return result.returncode == 0, detail


def _probe_url(url: str) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/system_stats", timeout=3) as response:
            return response.status == 200, f"HTTP {response.status}"
    except (urllib.error.URLError, OSError) as exc:
        return False, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comfyui-root", help="Directory containing ComfyUI main.py")
    parser.add_argument("--python", help="Python executable or python_embeded directory")
    parser.add_argument("--url", default="http://127.0.0.1:8188", help="Running ComfyUI URL")
    parser.add_argument("--workflows-dir", default=str(SKILL_DIR / "workflows"))
    parser.add_argument("--skip-url-check", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when validation fails")
    args = parser.parse_args()

    issues: list[str] = []
    root = Path(args.comfyui_root).expanduser().resolve() if args.comfyui_root else None
    if root is None:
        issues.append("ComfyUI root was not provided.")
    elif not root.is_dir():
        issues.append(f"ComfyUI root does not exist: {root}")
    elif not (root / "main.py").is_file():
        issues.append(f"ComfyUI root does not contain main.py: {root}")

    python_exe = _resolve_python(args.python, root)
    mcp_available = False
    mcp_detail = "Python was not resolved."
    if python_exe is None:
        issues.append("Embedded Python was not provided and could not be discovered.")
    elif not python_exe.is_file():
        issues.append(f"Python executable does not exist: {python_exe}")
    else:
        python_exe = python_exe.resolve()
        mcp_available, mcp_detail = _probe_mcp(python_exe)
        if not mcp_available:
            issues.append(
                "The selected Python cannot import the required FastMCP API: "
                f"{mcp_detail or 'module missing or incompatible mcp version'}"
            )

    workflows_dir = Path(args.workflows_dir).expanduser().resolve()
    if not workflows_dir.is_dir():
        issues.append(f"Workflow directory does not exist: {workflows_dir}")

    url_reachable = None
    url_detail = "check skipped"
    if not args.skip_url_check:
        url_reachable, url_detail = _probe_url(args.url)
        if not url_reachable:
            issues.append(f"ComfyUI is not reachable at {args.url}: {url_detail}")

    config = None
    install_command = None
    if python_exe:
        env = {
            "COMFYUI_URL": args.url.rstrip("/"),
            "COMFYUI_WORKFLOWS_DIR": str(workflows_dir),
        }
        if root:
            env["COMFYUI_ROOT"] = str(root)
        config = {
            "mcpServers": {
                "comfyui": {
                    "command": str(python_exe),
                    "args": [str(SERVER_SCRIPT)],
                    "env": env,
                }
            }
        }
        install_command = subprocess.list2cmdline(
            [str(python_exe), "-m", "pip", "install", MCP_REQUIREMENT]
        )

    result = {
        "ready": not issues,
        "issues": issues,
        "checks": {
            "comfyui_root": str(root) if root else None,
            "python": str(python_exe) if python_exe else None,
            "mcp_available": mcp_available,
            "comfyui_url": args.url.rstrip("/"),
            "comfyui_reachable": url_reachable,
            "url_detail": url_detail,
            "workflows_dir": str(workflows_dir),
        },
        "config": config,
        "install_command": install_command if not mcp_available else None,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.strict and issues else 0


if __name__ == "__main__":
    sys.exit(main())
