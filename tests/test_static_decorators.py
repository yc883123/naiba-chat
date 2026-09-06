# -*- coding: utf-8 -*-
"""护栏：@staticmethod/@classmethod 与实例绑定一致性 + 供应商内联测试路径。

事故复盘（2026-09-06）：_provider_profile 被装饰为 @staticmethod 但方法体引用
self.app——AST 未定义名扫描器把 self 视为隐式绑定名，无法发现这类错配；
只有"无 model_key 的模型编辑页内联测试连接"路径会触发 NameError。
本文件把「装饰器-绑定一致性」升级为机械守门（哲学③），并补功能回归用例。
"""

import ast
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.app import NaibaChatApp  # noqa: E402
from naiba.http import RequestHandler  # noqa: E402


class StaticDecoratorGuardTests(unittest.TestCase):
    def test_no_staticmethod_body_references_self(self):
        issues = []
        for path in (Path(__file__).resolve().parents[1] / "naiba").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                is_static = any(
                    isinstance(d, ast.Name) and d.id == "staticmethod"
                    for d in node.decorator_list
                )
                if not is_static:
                    continue
                for child in ast.walk(node):
                    if (
                        isinstance(child, ast.Name)
                        and child.id in {"self", "cls"}
                        and isinstance(child.ctx, ast.Load)
                    ):
                        issues.append((str(path), node.name, child.id, child.lineno))
                        break
        self.assertEqual(issues, [], f"@staticmethod 方法体内引用了实例绑定名: {issues}")

    def test_provider_profile_merges_stored_api_key(self):
        handler = RequestHandler.__new__(RequestHandler)
        app = NaibaChatApp.__new__(NaibaChatApp)
        app.config = SimpleNamespace(
            data={"providers": [{"id": "demo", "name": "演示", "api_key": "sk-1"}]}
        )
        handler.server = SimpleNamespace(app=app)
        profile = handler._provider_profile({"id": "demo"})
        self.assertEqual(profile["api_key"], "sk-1")
        # 未指定 id 时按内联 provider 原样返回（kind 推断不报错）。
        inline = handler._provider_profile({"request_format": "gemini"})
        self.assertEqual(inline["kind"], "online")


if __name__ == "__main__":
    unittest.main()
