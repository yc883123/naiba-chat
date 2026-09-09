# -*- coding: utf-8 -*-
"""naiba/ 全包未使用导入扫描（④ 导入审计）。

规则：
  - 遍历 naiba/**/*.py；对每个 import/from-import 顶层绑定，若该文件内从未 Load 则候选；
  - 带 ``# noqa: F401`` 的行被视为有意 re-export，不报（人工复核清单里单独列出）；
  - 输出按文件分组。候选需人工核实（跨模块引用、__init__ 星号导出、动态名等）。
"""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "naiba"


def scan(path: Path) -> tuple[list[str], list[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    loads = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    hits: list[str] = []
    noqa_reexports: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                if name not in loads:
                    line = ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
                    noqa = "*" if "noqa: F401" in (path.read_text(encoding="utf-8").splitlines()[node.lineno - 1] or "") else ""
                    (noqa_reexports if noqa else hits).append(f"{name}（{alias.name}）")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            for alias in node.names:
                name = alias.asname or alias.name
                if name not in loads:
                    text = path.read_text(encoding="utf-8").splitlines()[node.lineno - 1] if node.lineno - 1 < len(path.read_text(encoding="utf-8").splitlines()) else ""
                    noqa = "*" if ("noqa" in text and "F401" in text) else ""
                    (noqa_reexports if noqa else hits).append(f"{name}（{node.module}.{name}）")
    return hits, noqa_reexports


for path in sorted(PKG.rglob("*.py")):
    hits, reexports = scan(path)
    if hits or reexports:
        print(f"== {path.relative_to(PKG)} ==")
        for item in hits:
            print(f"   未使用: {item}")
        for item in reexports:
            print(f"   [noqa re-export]: {item}")
print("done")
