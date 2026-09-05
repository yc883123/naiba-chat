# -*- coding: utf-8 -*-
"""依赖图机械校验（重构哲学指引 §0.6 第③条的落地载体）。

规则（按当前阶段范围）：
1. 全项目（根目录 *.py + naiba/**）的"项目内 import 图"必须无环；
2. naiba/* 任何模块不得 import server（运行时永不 import server 的红线）；
3. naiba/core/* 只允许 import：标准库、naiba 包内部、net_io（叶子网络层）——
   纯函数层不得依赖任何运行时/编排模块。

说明：本测试随阶段推进扩展扫描范围（阶段 2/3 后应覆盖整个 naiba/ 包分层）。
"""

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROJECT_MODULE_PREFIXES = ("naiba.",)
ALLOWED_ROOT_LEAVES = {"net_io"}  # core 层允许的根目录叶子模块


def _project_module_names() -> set[str]:
    names: set[str] = set()
    for path in ROOT.glob("*.py"):
        if path.name.startswith(("test_", "_")):
            continue
        names.add(path.stem)
    for path in (ROOT / "naiba").rglob("*.py"):
        parts = path.relative_to(ROOT).with_suffix("").parts
        names.add(".".join(parts))
    return names


def _imports_of(source: Path) -> list[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imports.append(node.module)
    return imports


class DependencyDagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = _project_module_names()
        cls.graph: dict[str, set[str]] = {}
        for path in list(ROOT.glob("*.py")) + list((ROOT / "naiba").rglob("*.py")):
            name = path.relative_to(ROOT).with_suffix("").as_posix().replace("/", ".")
            if name.startswith(("test_", "_")) or path.name.startswith(("test_", "_")):
                continue
            local: set[str] = set()
            for imported in _imports_of(path):
                top = imported.split(".")[0]
                if top in cls.modules:
                    local.add(imported.split(".")[0] if imported in cls.modules
                              or imported.split(".", 1)[0] in cls.modules else imported)
            cls.graph[name] = local

    def _resolve_to_module(self, imported: str) -> str | None:
        """把导入路径归约到项目模块名（naiba.core.x → naiba.core 形式的包/模块）。"""
        parts = imported.split(".")
        for length in range(len(parts), 0, -1):
            candidate = ".".join(parts[:length])
            if candidate in self.modules:
                return candidate
        return None

    def test_no_import_cycles(self):
        # 简单 DFS 三色法找环；项目规模小，直接暴力检测即可。
        graph: dict[str, set[str]] = {}
        for name, imports in self.graph.items():
            resolved: set[str] = set()
            for imported in imports:
                target = self._resolve_to_module(imported)
                if target and target != name:
                    resolved.add(target)
            graph[name] = resolved

        cycles: list[list[str]] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def dfs(node: str, stack: list[str]) -> None:
            if node in visiting:
                idx = stack.index(node)
                cycles.append(stack[idx:] + [node])
                return
            if node in visited:
                return
            visiting.add(node)
            stack.append(node)
            for nxt in sorted(graph.get(node, set())):
                dfs(nxt, stack)
            stack.pop()
            visiting.discard(node)
            visited.add(node)

        for node in sorted(graph):
            dfs(node, [])
        self.assertEqual(cycles, [], f"项目内 import 图存在环：{cycles}")

    def test_naiba_never_imports_server(self):
        offenders = [
            (name, sorted(imports))
            for name, imports in self.graph.items()
            if name.startswith("naiba.") and any(self._resolve_to_module(i) == "server" for i in imports)
        ]
        self.assertEqual(offenders, [], f"naiba 包模块 import 了 server（红线）：{offenders}")

    def test_core_pure_layers_only_depend_on_leaves(self):
        offenders = [
            (name, sorted(imports))
            for name, imports in self.graph.items()
            if name.startswith("naiba.core.")
            for imported in imports
            if (target := self._resolve_to_module(imported))
            and target not in ALLOWED_ROOT_LEAVES
            and not target.startswith("naiba.")
        ]
        self.assertEqual(offenders, [], f"core 纯函数层依赖了非叶子模块（红线）：{offenders}")


if __name__ == "__main__":
    unittest.main()
