import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SKILL_ROOT / "scripts" / "configure_mcp.py"


def _load_configure_module():
    spec = importlib.util.spec_from_file_location("configure_mcp_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfigureMCPTests(unittest.TestCase):
    def setUp(self):
        self.configure = _load_configure_module()

    def test_probe_checks_the_fastmcp_api_used_by_the_server(self):
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(self.configure.subprocess, "run", return_value=completed) as run:
            available, detail = self.configure._probe_mcp(Path("python.exe"))

        self.assertTrue(available)
        self.assertEqual(detail, "")
        self.assertEqual(
            run.call_args.args[0],
            ["python.exe", "-c", "from mcp.server.fastmcp import FastMCP"],
        )

    def test_install_command_caps_mcp_below_version_two(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            comfyui_root = root / "ComfyUI"
            workflows_dir = root / "workflows"
            python_exe = root / "python.exe"
            comfyui_root.mkdir()
            workflows_dir.mkdir()
            (comfyui_root / "main.py").write_text("", encoding="utf-8")
            python_exe.write_text("", encoding="utf-8")
            argv = [
                "configure_mcp.py",
                "--comfyui-root",
                str(comfyui_root),
                "--python",
                str(python_exe),
                "--workflows-dir",
                str(workflows_dir),
                "--skip-url-check",
                "--strict",
            ]
            output = io.StringIO()
            with (
                patch.object(sys, "argv", argv),
                patch.object(self.configure, "_probe_mcp", return_value=(False, "missing")),
                redirect_stdout(output),
            ):
                exit_code = self.configure.main()

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertIn("mcp>=1.2.0,<2", result["install_command"])
        self.assertIn("required FastMCP API", result["issues"][0])


if __name__ == "__main__":
    unittest.main()
