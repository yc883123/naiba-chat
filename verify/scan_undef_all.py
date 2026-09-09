# -*- coding: utf-8 -*-
"""收官验收：naiba/ 全包 + server.py/launcher.py 未定义名扫描（含装饰器-绑定误报候选提示）。"""
import ast
import builtins
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = sorted(ROOT.joinpath("naiba").rglob("*.py")) + [
    ROOT / "server.py", ROOT / "launcher.py",
]

BUILTINS = set(dir(builtins)) | {"__name__", "__file__", "__doc__", "__package__", "self", "cls"}


def collect_all_bindings(tree: ast.AST) -> set[str]:
    bound: set[str] = set()

    class Binder(ast.NodeVisitor):
        def visit_Import(self, node):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])

        def visit_ImportFrom(self, node):
            for a in node.names:
                bound.add(a.asname or a.name)

        def _add_arg(self, arg):
            if isinstance(arg, ast.arg):
                bound.add(arg.arg)

        def _add_target(self, t):
            if isinstance(t, ast.Name):
                bound.add(t.id)
            elif isinstance(t, (ast.Tuple, ast.List)):
                for e in t.elts:
                    self._add_target(e)
            elif isinstance(t, ast.Starred):
                self._add_target(t.value)

        def visit_FunctionDef(self, node):
            bound.add(node.name)
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                self._add_arg(arg)
            for arg in (args.vararg, args.kwarg):
                if arg is not None:
                    self._add_arg(arg)
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node):
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                self._add_arg(arg)
            for arg in (args.vararg, args.kwarg):
                if arg is not None:
                    self._add_arg(arg)
            self.generic_visit(node)

        def visit_ExceptHandler(self, node):
            if node.name:
                bound.add(node.name)
            self.generic_visit(node)

        def visit_comprehension(self, node):
            for t in getattr(node, "targets", None) or [node.target]:
                self._add_target(t)
            self.generic_visit(node)

        def visit_Assign(self, node):
            for t in node.targets:
                self._add_target(t)
            self.generic_visit(node)

        def visit_AnnAssign(self, node):
            if isinstance(node.target, ast.Name):
                bound.add(node.target.id)
            self.generic_visit(node)

        def visit_For(self, node):
            self._add_target(node.target)
            self.generic_visit(node)

        visit_AsyncFor = visit_For

        def visit_With(self, node):
            for item in node.items:
                if item.optional_vars:
                    self._add_target(item.optional_vars)
            self.generic_visit(node)

        def visit_ClassDef(self, node):
            bound.add(node.name)
            self.generic_visit(node)

    Binder().visit(tree)
    return bound


total_bad = 0
for path in TARGETS:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound = collect_all_bindings(tree)
    loads = {n.id for n in ast.walk(tree)
             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    undef = sorted(loads - bound - BUILTINS)
    if undef:
        total_bad += 1
        print(f"未定义名 {path.relative_to(ROOT)}: {undef}")
print(f"完成：{len(TARGETS)} 个文件，含候选 {total_bad} 个")
