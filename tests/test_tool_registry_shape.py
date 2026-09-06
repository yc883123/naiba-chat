"""护栏：工具声明表形态守门（工具系统重构，单轨形态 Phase 5）。

冻结以下不变量，防止「单一定义」结构漂移：

1. 每个工具 schema 行结构合法：名字唯一/非空、description 非空、parameters 为
   {"type": "object", "properties": {...}} 且 required ⊆ properties；
2. 每个声明工具在组装态注册表（Provider 绑定后）必须绑定 def.execute——
   系统工具（system=True）直调、常规工具经引擎；alias 经查询层归一；
3. 常规引擎直管工具（core）必须携带 def 级 policy（权限同源）；
4. 别名（HARNESS_ALIASES）目标必须存在于声明表；
5. schemas() 输出键集冻结（metadata/aliases/policy 不进模型可见行）；
6. permission 取值必须属于 ToolExecutor.VALID_PERMISSION_MODES。

组装态注册表以桩依赖构造（不启动 App），见 ``_assembled_test_registry``。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from naiba.tools import registry as registry_mod
from naiba.tools.executor import ToolExecutor

ROOT = Path(__file__).resolve().parent.parent


class _FakeHandlers:
    """模拟各域 runtime 的 tool_handlers()（桩）。"""

    def __init__(self, names: set[str]) -> None:
        self._names = names

    def tool_handlers(self):
        return {name: (lambda args, skills, ctx=None: (True, "")) for name in self._names}


def _assembled_test_registry() -> Any:
    """组装态注册表（桩依赖）：模拟 app 装配后的 Provider 绑定，供通道/一致性守门。

    仅用于测试：runtime 依赖以桩代替（不启动 App / 不触达存储与网络）。
    """
    from types import SimpleNamespace

    from naiba.tools.providers import capability as cap_provider
    from naiba.tools.providers import comfyui as comfyui_provider
    from naiba.tools.providers import core as core_provider
    from naiba.tools.providers import jobs as jobs_provider
    from naiba.tools.providers import search as search_provider
    from naiba.tools.providers import vision as vision_provider

    def _stub_app() -> SimpleNamespace:
        return SimpleNamespace(jobs=None, storage=None)

    reg = registry_mod.build_tool_registry()
    reg.register_provider(
        core_provider.CoreToolProvider(
            core_provider.ToolContext(
                workspace=Path(tempfile.mkdtemp(prefix="naiba-assembled-")),
                python_executable=sys.executable,
                command_timeout=60,
                mcp_registry=None,
                mcp_register=None,
            )
        )
    )
    reg.register_provider(jobs_provider.JobToolProvider(_stub_app()))
    reg.register_provider(comfyui_provider.ComfyUIProvider(_stub_app()))
    reg.register_provider(search_provider.SearchRecallProvider(None, None))
    reg.register_provider(
        cap_provider.CapabilityToolProvider(
            _FakeHandlers({spec.name for spec in registry_mod.build_capability_tool_specs()})
        )
    )
    reg.register_provider(
        vision_provider.VisionToolProvider(
            _FakeHandlers({spec.name for spec in registry_mod.build_vision_tool_specs()})
        )
    )
    return reg


class ToolRegistryShapeTests(unittest.TestCase):
    """声明表形态与通道完整性守门。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = registry_mod.build_tool_registry()
        cls.rows = cls.registry.schemas()
        cls.aliases = dict(registry_mod.HARNESS_ALIASES)

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

    def test_every_tool_has_execute_after_assembly(self) -> None:
        """组装态（Provider 绑定后）：每个声明工具（含 Harness 别名）必须绑有 def.execute（防仅声明漂移）。"""
        assembled = _assembled_test_registry()
        for name in self.registry.names():
            with self.subTest(tool=name):
                if name.startswith("mcp__"):
                    continue  # MCP 动态工具由 register_mcp_tools 绑定
                spec = assembled.get(name)
                self.assertIsNotNone(spec, f"{name}: 组装态注册表缺失该工具")
                self.assertIsNotNone(spec.execute, f"{name}: 无 def.execute（仅声明，无法执行）")

    def test_aliases_target_exists(self) -> None:
        declared = {str(row["name"]) for row in self.rows}
        for alias, target in self.aliases.items():
            with self.subTest(alias=alias):
                self.assertIn(target, declared, f"别名 {alias} → {target} 的目标不存在")

    def test_schema_row_key_set_stable(self) -> None:
        """schemas() 输出键集冻结（metadata/aliases/policy 不进模型可见行）。"""
        expected = {
            "name", "description", "parameters", "side_effect",
            "retryable", "timeout", "permission", "annotations",
        }
        for row in self.rows:
            with self.subTest(tool=row["name"]):
                self.assertEqual(set(row.keys()), expected, "schemas() 行键集漂移")

    def test_schema_rows_match_names(self) -> None:
        # schemas() 为模型/Web 可见集：Harness 别名被隐藏，其余注册 def 必须全部可见。
        visible = {str(row["name"]) for row in self.rows}
        everything = set(self.registry.names())
        self.assertFalse(
            visible & set(self.aliases), "Harness 别名不应出现在模型可见声明表（schemas）"
        )
        self.assertEqual(everything - visible, set(self.aliases), "除别名外 schemas() 不得静默缺漏")

    def test_harness_aliases_hidden_but_compatible(self) -> None:
        """Harness 别名隐藏守门：模型可见表不含别名；查询层 resolve/get/执行分发仍兼容。"""
        registry = registry_mod.build_tool_registry()
        visible = {str(row["name"]) for row in registry.schemas()}
        for alias, target in registry_mod.HARNESS_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertNotIn(alias, visible, f"别名 {alias} 泄露到模型可见声明表")
                self.assertIsNotNone(registry.get(alias), f"{alias} def 缺失（执行兼容破坏）")
                self.assertEqual(registry.resolve(alias), target)

    def test_plan_mode_system_tools_exclude_harness_aliases(self) -> None:
        """allowed_tools 决议（resolve_allowed_tools）不得再注入 Harness 别名（模型可见集来源）。"""
        from types import SimpleNamespace

        from naiba.run.session import resolve_allowed_tools

        app = SimpleNamespace(
            config=SimpleNamespace(data={"agent_tools": ["read_file"]}),
            tool_registry=registry_mod.build_tool_registry(),
            web_search=SimpleNamespace(is_available=lambda: False),
        )
        tools = resolve_allowed_tools(app, "craft", {"tool_scope": []}, False)
        for alias in registry_mod.HARNESS_ALIASES:
            with self.subTest(alias=alias):
                self.assertNotIn(alias, tools, f"别名 {alias} 进入 allowed_tools（模型可见集）")
        self.assertIn("read_file", tools)


class ToolRegistryUnifiedFieldsTests(unittest.TestCase):
    """单一定义字段（aliases/policy/system/metadata）与 Provider 骨架守门。"""

    def test_alias_resolution_in_query_layer(self) -> None:
        registry = registry_mod.build_tool_registry()
        for alias, target in registry_mod.HARNESS_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertEqual(registry.resolve(alias), target)
                self.assertEqual(registry.resolve(target), target)
        self.assertEqual(registry.resolve("no_such_tool"), "no_such_tool")

    def test_provider_registration_and_metadata(self) -> None:
        registry = registry_mod.ToolRegistry()

        class FakeProvider:
            def tools(self) -> list[Any]:
                return [
                    registry_mod.ToolSpec(
                        name="t_provider",
                        description="provider 注入的测试工具",
                        parameters={"type": "object", "properties": {}},
                        metadata={"icon": "wrench"},
                    )
                ]

        registry.register_provider(FakeProvider())
        spec = registry.get("t_provider")
        self.assertIsNotNone(spec, "Provider 工具未注册成功")
        self.assertEqual(spec.metadata, {"icon": "wrench"}, "metadata 未保留")
        self.assertIsNone(spec.policy, "policy 默认应为 None")
        self.assertFalse(spec.system, "system 默认应为 False")

    def test_execute_prefers_system_def_execute(self) -> None:
        """system=True 的 def.execute 直调，绕过引擎。"""
        registry = registry_mod.ToolRegistry()
        calls: list[int] = []

        def handler(arguments: dict[str, Any], skills: list[Any], ctx: Any) -> tuple[bool, str]:
            calls.append(1)
            return True, "system-ok"

        registry.register(
            registry_mod.ToolSpec(
                name="t_sys",
                description="系统级工具",
                parameters={"type": "object", "properties": {}},
                execute=handler,
                system=True,
            )
        )
        ok, result = registry.execute("t_sys", {}, [])
        self.assertTrue(ok)
        self.assertEqual(result, "system-ok")
        self.assertEqual(len(calls), 1)

    def test_execute_def_routes_through_engine_when_present(self) -> None:
        """非 system 的 def.execute：有引擎（run_context.executor）时经引擎，无引擎时直调。"""
        registry = registry_mod.ToolRegistry()

        def handler(arguments: dict[str, Any], skills: list[Any], ctx: Any) -> tuple[bool, str]:
            return True, "def-ok"

        registry.register(
            registry_mod.ToolSpec(
                name="t_reg",
                description="常规工具",
                parameters={"type": "object", "properties": {}},
                execute=handler,
            )
        )

        class FakeEngine:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def execute(self, name: str, arguments: dict[str, Any], active_skills: list[Any], run_context: Any = None) -> tuple[bool, str]:
                self.calls.append(name)
                return True, "engine-ok"

        engine = FakeEngine()
        ok, result = registry.execute("t_reg", {}, [], {"executor": engine})
        self.assertTrue(ok)
        self.assertEqual(result, "engine-ok", "有引擎时必须经引擎执行（策略/确认在引擎侧）")
        self.assertEqual(engine.calls, ["t_reg"])
        ok2, result2 = registry.execute("t_reg", {}, [])
        self.assertTrue(ok2)
        self.assertEqual(result2, "def-ok", "无引擎时直调 def.execute")

    def test_execute_unknown_tool_reports(self) -> None:
        registry = registry_mod.ToolRegistry()
        ok, result = registry.execute("no_such_tool", {}, [])
        self.assertFalse(ok)
        self.assertIn("未知工具", result)


class ToolPolicyUnificationTests(unittest.TestCase):
    """Phase 2/5：权限策略并入 ToolSpec 同源守门。"""

    def _make_executor(self, mode: str = "confirm", tmp: Path | None = None):
        from naiba.mcp import MCPRegistry
        from naiba.tools.executor import ToolExecutor

        root = tmp or Path(tempfile.mkdtemp(prefix="naiba-toolpolicy-"))
        registry = registry_mod.build_tool_registry()
        executor = ToolExecutor(root, sys.executable, 60, MCPRegistry([]), permission_mode=mode)
        executor.set_def_resolver(registry.get)
        return executor

    def test_engine_routed_core_defs_have_policies(self) -> None:
        """引擎直管（非 system）工具必须携带 def 级 policy——权限与声明同源。"""
        assembled = _assembled_test_registry()
        missing = []
        for spec in assembled.names():
            item = assembled.get(spec)
            if item.system or spec.startswith("mcp__"):
                continue
            if getattr(item, "policy", None) is None:
                missing.append(spec)
        self.assertEqual(missing, [], f"引擎直管工具缺 def 级 policy：{missing}")

    def test_http_request_policy_method_aware(self) -> None:
        """行为优化：GET/HEAD 免确认；POST 在 confirm 模式需确认、auto 放行。"""
        registry = registry_mod.build_tool_registry()
        from naiba.tools.providers.core import CoreToolProvider, ToolContext

        root = Path(tempfile.mkdtemp(prefix="naiba-toolpolicy-"))
        registry.register_provider(
            CoreToolProvider(
                ToolContext(
                    workspace=root,
                    python_executable=sys.executable,
                    command_timeout=60,
                    mcp_registry=None,
                    mcp_register=None,
                )
            )
        )
        executor = self._make_executor(tmp=root)
        executor.set_def_resolver(registry.get)
        self.assertEqual(
            executor._confirmation_reason("http_request", {"method": "GET", "url": "http://x"}, []),
            "",
        )
        self.assertEqual(
            executor._confirmation_reason("http_request", {"method": "HEAD", "url": "http://x"}, []),
            "",
        )
        self.assertIn(
            "HTTP",
            executor._confirmation_reason("http_request", {"method": "POST", "url": "http://x"}, []),
        )
        auto = self._make_executor(mode="auto", tmp=root)
        auto.set_def_resolver(registry.get)
        self.assertEqual(
            auto._confirmation_reason("http_request", {"method": "POST", "url": "http://x"}, []),
            "",
        )

    def test_def_policy_invoked_with_mode(self) -> None:
        """def 级 policy 优先于引擎默认，且收到 permission_mode。"""
        from naiba.mcp import MCPRegistry
        from naiba.tools.executor import ToolExecutor

        root = Path(tempfile.mkdtemp(prefix="naiba-toolpolicy-"))
        registry = registry_mod.ToolRegistry()
        seen: list[str] = []

        def policy(tool, arguments, active_skills, permission_mode, run_context, workspace=None):
            seen.append(permission_mode)
            return "自定义确认" if permission_mode == "confirm" else ""

        registry.register(
            registry_mod.ToolSpec(
                name="t_policy",
                description="策略测试工具",
                parameters={"type": "object", "properties": {}},
                execute=lambda args, skills, ctx: (True, "ok"),
                policy=policy,
                system=False,
            )
        )
        executor = ToolExecutor(root, sys.executable, 60, MCPRegistry([]), permission_mode="confirm")
        executor.set_def_resolver(registry.get)
        reason = executor._confirmation_reason("t_policy", {}, [])
        self.assertIn("自定义确认", reason)
        self.assertEqual(seen, ["confirm"])
        auto = ToolExecutor(root, sys.executable, 60, MCPRegistry([]), permission_mode="auto")
        auto.set_def_resolver(registry.get)
        self.assertEqual(auto._confirmation_reason("t_policy", {}, []), "")

    def test_def_policy_fail_closed(self) -> None:
        """策略抛异常 → 返回需确认理由（fail-closed，不静默放行）。"""
        from naiba.mcp import MCPRegistry
        from naiba.tools.executor import ToolExecutor

        root = Path(tempfile.mkdtemp(prefix="naiba-toolpolicy-"))
        registry = registry_mod.ToolRegistry()

        def boom(tool, arguments, active_skills, permission_mode, run_context, workspace=None):
            raise RuntimeError("策略炸了")

        registry.register(
            registry_mod.ToolSpec(
                name="t_boom",
                description="炸策略工具",
                parameters={"type": "object", "properties": {}},
                policy=boom,
            )
        )
        executor = ToolExecutor(root, sys.executable, 60, MCPRegistry([]), permission_mode="confirm")
        executor.set_def_resolver(registry.get)
        reason = executor._confirmation_reason("t_boom", {}, [])
        self.assertIn("权限策略评估失败", reason)
        self.assertIn("RuntimeError", reason)

    def test_full_mode_skips_policy(self) -> None:
        """full 模式语义 = 永不询问：策略不被评估。"""
        from naiba.mcp import MCPRegistry
        from naiba.tools.executor import ToolExecutor

        root = Path(tempfile.mkdtemp(prefix="naiba-toolpolicy-"))
        registry = registry_mod.ToolRegistry()

        def strict(tool, arguments, active_skills, permission_mode, run_context, workspace=None):
            return "永远确认"

        registry.register(
            registry_mod.ToolSpec(
                name="t_full",
                description="full 测试工具",
                parameters={"type": "object", "properties": {}},
                policy=strict,
            )
        )
        executor = ToolExecutor(root, sys.executable, 60, MCPRegistry([]), permission_mode="full")
        executor.set_def_resolver(registry.get)
        self.assertEqual(executor._confirmation_reason("t_full", {}, []), "")


class VisionEntryUnificationTests(unittest.TestCase):
    """视觉单入口重构守门：8 个旧视觉工具 + call_mcp 退役，2 个新入口同名会话化。"""

    LEGACY_NAMES = {
        "vision_describe", "vision_ground", "vision_detect", "vision_ocr",
        "vision_colors", "vision_crop", "vision_pixel_diff", "vision_read_folder",
        "call_mcp",
    }

    def test_legacy_names_absent_from_schemas(self) -> None:
        registry = registry_mod.build_tool_registry()
        declared = {str(row["name"]) for row in registry.schemas()}
        self.assertEqual(declared & self.LEGACY_NAMES, set(), "退役工具名仍出现在模型可见声明表")

    def test_new_vision_entries_declared(self) -> None:
        registry = registry_mod.build_tool_registry()
        for name in ("vision_analyze", "vision_image_ops"):
            with self.subTest(tool=name):
                self.assertIsNotNone(registry.get(name), f"{name} 未声明")

    def test_retired_map_targets_exist(self) -> None:
        for old_name, new_name in registry_mod.RETIRED_TOOL_MAP.items():
            with self.subTest(old=old_name):
                self.assertEqual(registry_mod.build_tool_registry().get(old_name), None)
                self.assertIsNotNone(
                    registry_mod.build_tool_registry().get(new_name),
                    f"退役映射目标 {new_name} 不存在（{old_name} 的映射）",
                )

    def test_retired_names_guide_instead_of_unknown(self) -> None:
        registry = registry_mod.build_tool_registry()
        for old_name in ("vision_describe", "vision_read_folder", "call_mcp", "vision_colors"):
            with self.subTest(old=old_name):
                ok, result = registry.execute(old_name, {}, [])
                self.assertFalse(ok)
                self.assertIn("已", result, f"{old_name} 失败提示不是退役引导：{result}")
                self.assertNotEqual(result, f"未知工具：{old_name}")

    def test_vision_analyze_load_variant(self) -> None:
        base = registry_mod.build_vision_tool_specs()[0]
        variant = registry_mod.vision_analyze_load_variant(base)
        self.assertEqual(variant.name, "vision_analyze", "会话化换形态不得改名（模型接口恒定）")
        self.assertEqual(
            set((variant.parameters or {}).get("properties", {}).keys()),
            {"paths", "folder", "max_images"},
            "装载形态参数集应替换为装载参数（不含 question）",
        )
        self.assertNotEqual(variant.description, base.description)
        self.assertEqual(base.name, variant.name)

    def test_vision_image_ops_op_enum(self) -> None:
        spec = registry_mod.build_vision_tool_specs()[1]
        op_schema = (spec.parameters or {}).get("properties", {}).get("op", {})
        self.assertEqual(
            op_schema.get("enum"), ["colors", "crop", "pixel_diff"], "op 枚举必须三选一"
        )
        self.assertIn("op", (spec.parameters or {}).get("required", []))

    def test_policy_uses_engine_workspace_not_assembly_config(self) -> None:
        """回归：策略必须使用「当前运行（引擎级）工作区」，而非 provider 装配期配置。

        会话级工作区切换后（executor.workspace 被 run 快照覆盖）：
        引擎工作区内子目录免确认；装配期配置区（现已非运行工作区）按越界确认。
        """
        import tempfile as _tf

        from naiba.mcp import MCPRegistry
        from naiba.tools.executor import ToolExecutor
        from naiba.tools.providers import core as core_provider

        base = Path(_tf.mkdtemp(prefix="naiba-ws-policy-"))
        assembly_ws = base / "assembly"
        run_ws = base / "run"
        assembly_ws.mkdir()
        run_ws.mkdir()
        (run_ws / "inside.txt").write_text("in", encoding="utf-8")
        (assembly_ws / "other.txt").write_text("out", encoding="utf-8")

        ctx = core_provider.ToolContext(
            workspace=assembly_ws,
            python_executable=sys.executable,
            command_timeout=60,
            mcp_registry=None,
        )
        registry = registry_mod.ToolRegistry()
        registry.register(
            registry_mod.ToolSpec(
                name="read_file",
                description="dummy",
                parameters={"type": "object", "properties": {}},
                side_effect=False,
                policy=core_provider._make_core_policy(ctx, "read_file"),
            )
        )
        executor = ToolExecutor(run_ws, sys.executable, 60, MCPRegistry([]), permission_mode="confirm")
        executor.set_def_resolver(registry.get)
        self.assertEqual(
            executor._confirmation_reason("read_file", {"path": str(run_ws / "inside.txt")}, []),
            "",
            "当前运行工作区内文件被误判越界（装配期配置泄露到策略）",
        )
        reason = executor._confirmation_reason("read_file", {"path": str(assembly_ws / "other.txt")}, [])
        self.assertIn("工作区外", reason)
        self.assertIn(str(assembly_ws / "other.txt"), reason, "确认理由应显示越界路径（装配区现不属于运行工作区）")

if __name__ == "__main__":
    unittest.main()
