# -*- mode: python ; coding: utf-8 -*-

import json
import os
import subprocess
from pathlib import Path


root = Path(SPECPATH).resolve()
icon_path = root / "icon.ico"
commit = os.environ.get("NAIBA_BUILD_COMMIT", "").strip()
if not commit:
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
    except Exception:
        commit = ""
version = os.environ.get("NAIBA_BUILD_VERSION", "").strip() or (commit[:12] if commit else "dev")
build_info = root / "build_info.json"
build_info.write_text(
    json.dumps({"version": version, "commit": commit}, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

skill_root = root / "skills"
private_skill_dirs = {
    "H3擦边导演_动作库增强版_v5.4",
    "minnimax-h",
}
skill_datas = []
if skill_root.is_dir():
    for source in skill_root.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(skill_root)
        if any(part in private_skill_dirs for part in relative.parts):
            continue
        if "output" in relative.parts or source.name == "_err.txt":
            continue
        skill_datas.append((str(source), str(Path("skills") / relative.parent)))


def Tree(directory, prefix="", excludes=None):
    """Return PyInstaller data pairs without relying on Tree TOC entries."""
    source_root = Path(directory)
    excluded = set(excludes or ()) | private_skill_dirs
    pairs = []
    if not source_root.is_dir():
        return pairs
    for source in source_root.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(source_root)
        if any(part in excluded for part in relative.parts):
            continue
        if "output" in relative.parts or source.name == "_err.txt":
            continue
        pairs.append((str(source), str(Path(prefix) / relative.parent)))
    return pairs

a = Analysis(
    [str(root / "launcher.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / "public"), "public"),
        *Tree(
            str(root / "skills"),
            prefix="skills",
            excludes=[
                "H3擦边导演_动作库增强版_v5.4",
                "minnimax-h",
            ],
        ),
        (str(build_info), "."),
        (str(root / "release_notes.json"), "."),
        (str(icon_path), "."),
    ],
    hiddenimports=["mcp", "mcp.client.stdio"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="naiba-chat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    icon=str(icon_path) if icon_path.is_file() else None,
)
