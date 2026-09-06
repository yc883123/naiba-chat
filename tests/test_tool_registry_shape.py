"""护栏：工具声明表形态守门（工具系统重构 Phase 0 引入）。

在现有行为不变的前提下冻结以下不变量，防止后续「单一定义」重构期间漂移：

1. 每个工具 schema 行结构合法：名字唯一/非空、description 非空、parameters 为
   {"type": "object", "properties": {...}} 且 required ⊆ properties；
2. 每个声明工具必须恰好有一个可执行的通道：
   - mcp__ 前缀 → MCP 动态工具；
   - ToolExecutor._tool_<name> 方法 → 执行器实现；
   - 静态 AST 收集到的系统处理器注册点（app/jobs/subagent/capability/vision/search）；
3. 别名（ToolExecutor.TOOL_ALIASES）目标必须存在于声明表；
4. permission 取值必须属于 ToolExecutor.VALID_PERMISSION_MODES。

通道清单为静态推断（AST + 运行时声明表），不启动 App，避免装配/IO 依赖。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path
from typing import Any

from naiba.tools import registry as registry_mod
from naiba.tools.executor import ToolExecutor

ROOT = Path(__file__).resolve().parent.parent
SPEC_FILES = [
    "naiba/app.py",
    "naiba/jobs.py",
    "naiba/subagent.py",
    "naiba/capability.py",
    "naiba/vision/runtime.py",
    "naiba/search.py",
]


def _ast_literal(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def _executor_method_names() -> set[str]:
    """静态收集 ToolExecutor 的所有 _tool_<name> 方法名。"""
    src = (ROOT / "naiba/tools/executor.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ToolExecutor":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name.startswith("_tool_"):
                    names.add(item.name[len("_tool_"):])
    return names


def _alias_table() -> dict[str, str]:
    """静态收集 ToolExecutor.TOOL_ALIASES 字面量表。"""
    src = (ROOT / "naiba/tools/executor.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ToolExecutor":
            for item in node.body:
                if isinstance(item, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "TOOL_ALIASES" for t in item.targets
                ):
                    raw = _ast_literal(item.value)
                    if isinstance(raw, dict):
                        return {str(k): str(v) for k, v in raw.items()}
    return {}


def _system_handler_names() -> set[str]:
    """静态收集系统处理器注册名（仅字面量注册点，避免误收配置字典）：

    - register_system_handler("字面量")：Name 或 Attribute（self.tool_registry.register_system_handler）调用；
    - job_tool_handler_factory / tool_handlers：仅收集 Return 直接值字典的字符串键
      （函数体内其他字典如设值默认值不属于处理器表）。
    """
    names: set[str] = set()
    for rel in SPEC_FILES:
        path = ROOT / rel
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Name) and node.func.id == "register_system_handler")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "register_system_handler")
            ):
                if node.args:
                    val = _ast_literal(node.args[0])
                    if isinstance(val, str):
                        names.add(val)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name == "job_tool_handler_factory" or node.name == "tool_handlers"
            ):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Return) and sub.value is not None:
                        dict_node = sub.value
                        if isinstance(dict_node, ast.Dict):
                            for key in dict_node.keys:
                                val = _ast_literal(key) if key else None
                                if isinstance(val, str):
                                    names.add(val)
                        # 兼容 rebuild 中转形式：dict(name=fn, ...)
                        elif isinstance(dict_node, ast.Call):
                            for kw in dict_node.keywords:
                                if kw.arg:
                                    names.add(kw.arg)
    return names


class ToolRegistryShapeTests(unittest.TestCase):
    """声明表形态与通道完整性守门。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = registry_mod.build_tool_registry()
        cls.rows = cls.registry.schemas()
        cls.methods = _executor_method_names()
        cls.handlers = _system_handler_names()
        cls.aliases = _alias_table()

    def test_names_unique_and_nonempty(self) -> None:
        names = [str(row["name"]) for row in self.rows]
        self.assertEqual(len(names), len(set(names)), "存在重复工具名（须先查注册序覆盖）")
        self.assertTrue(all(name for name in names), "存在空工具名")

    def test_schema_rows_wellformed(self) -> None:
        for row in self.rows:
            name = row["name"]
            with self.subTest(tool=name):
                self.assertIsInstance(row["description"], str)
                self.assertTrue(row["description"].strip(), f"{name}: description 为空")
                params = row["parameters"]
                self.assertIsInstance(params, dict, f"{name}: parameters 非对象")
                self.assertEqual(params.get("type"), "object", f"{name}: parameters.type 应为 object")
                properties = params.get("properties")
                self.assertIsInstance(properties, dict, f"{name}: parameters.properties 缺失")
                required = params.get("required") or []
                self.assertIsInstance(required, list, f"{name}: required 应为数组")
                for key in required:
                    self.assertIn(key, properties, f"{name}: required 键 {key} 不在 properties 中")
                self.assertGreater(row["timeout"], 0, f"{name}: timeout 必须为正")
                self.assertIn(
                    row["permission"],
                    ToolExecutor.VALID_PERMISSION_MODES,
                    f"{name}: permission 非法 {row['permission']}",
                )
                self.assertIsInstance(row["side_effect"], bool, f"{name}: side_effect 非布尔")
                self.assertIsInstance(row["retryable"], bool, f"{name}: retryable 非布尔")

    def test_every_tool_has_one_execution_channel(self) -> None:
        """声明的每个工具必须能解析到执行通道（防"仅声明/仅实现"漂移）。

        别名（TOOL_ALIASES）经其目标工具解析通道。
        """
        for row in self.rows:
            name = str(row["name"])
            with self.subTest(tool=name):
                if name.startswith("mcp__"):
                    continue  # MCP 动态工具，由寄存器回调维护
                lookup = self.aliases.get(name, name)
                if lookup in self.methods:
                    continue  # 执行器实现（或别名指向的实现）
                self.assertIn(
                    lookup,
                    self.handlers,
                    f"{name}（别名→{lookup}）：既无 _tool_* 方法也不在系统处理器注册表（仅声明，无法执行）",
                )

    def test_declared_tools_have_implementations_or_handlers_consistent(self) -> None:
        """反向防漂移：执行器方法/系统处理器注册名不应指向不存在的工具。"""
        declared = {str(row["name"]) for row in self.rows}
        orphans = sorted(self.methods - declared)
        self.assertEqual(orphans, [], f"执行器存在未声明的孤儿方法：{orphans}")
        unknown_handlers = sorted(
            name for name in self.handlers if not name.startswith("mcp__") and name not in declared
        )
        self.assertEqual(unknown_handlers, [], f"系统处理器注册了未声明的工具：{unknown_handlers}")

    def test_aliases_target_exists(self) -> None:
        declared = {str(row["name"]) for row in self.rows}
        for alias, target in self.aliases.items():
            with self.subTest(alias=alias):
                self.assertIn(target, declared, f"别名 {alias} → {target} 的目标不存在")

    def test_schema_rows_match_names(self) -> None:
        self.assertEqual(len(self.rows), len(self.registry.names()))


if __name__ == "__main__":
    unittest.main()
