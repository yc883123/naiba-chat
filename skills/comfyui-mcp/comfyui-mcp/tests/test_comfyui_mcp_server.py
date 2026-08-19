import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import unittest
import urllib.parse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = SKILL_ROOT / "scripts" / "comfyui_mcp_server.py"


class _FakeFastMCP:
    def __init__(self, name):
        self.name = name

    def tool(self, **_kwargs):
        return lambda function: function

    def run(self):
        raise AssertionError("The MCP transport must not start during tests")


def _load_server(environment):
    mcp_module = types.ModuleType("mcp")
    types_module = types.ModuleType("mcp.types")
    server_module = types.ModuleType("mcp.server")
    fastmcp_module = types.ModuleType("mcp.server.fastmcp")
    fastmcp_module.FastMCP = _FakeFastMCP
    types_module.ToolAnnotations = lambda **values: types.SimpleNamespace(**values)
    modules = {
        "mcp": mcp_module,
        "mcp.types": types_module,
        "mcp.server": server_module,
        "mcp.server.fastmcp": fastmcp_module,
    }
    module_name = f"comfyui_mcp_server_test_{id(environment)}"
    spec = importlib.util.spec_from_file_location(module_name, SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, environment, clear=False), patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


def _object_info(checkpoints=None, loras=None):
    checkpoints = ["model.safetensors"] if checkpoints is None else checkpoints
    loras = ["detail.safetensors"] if loras is None else loras
    return {
        "CheckpointLoaderSimple": {
            "input": {
                "required": {
                    "ckpt_name": [checkpoints, {}],
                }
            }
        },
        "LoraLoader": {
            "input": {"required": {"lora_name": [loras, {}]}},
        },
        "VAELoader": {
            "input": {"required": {"vae_name": [["sdxl.vae.safetensors"], {}]}},
        },
        "ControlNetLoader": {
            "input": {"required": {"control_net_name": [["canny.safetensors"], {}]}},
        },
        "CLIPTextEncode": {
            "input": {"required": {"text": ["STRING", {}], "clip": ["CLIP", {}]}},
        },
        "EmptyLatentImage": {
            "input": {
                "required": {
                    "width": ["INT", {}],
                    "height": ["INT", {}],
                    "batch_size": ["INT", {}],
                }
            }
        },
        "KSampler": {
            "input": {
                "required": {
                    "model": ["MODEL", {}],
                    "positive": ["CONDITIONING", {}],
                    "latent_image": ["LATENT", {}],
                    "seed": ["INT", {}],
                    "steps": ["INT", {}],
                    "cfg": ["FLOAT", {}],
                    "denoise": ["FLOAT", {}],
                }
            }
        },
        "SaveImage": {
            "output_node": True,
            "input": {
                "required": {
                    "images": ["IMAGE", {}],
                    "filename_prefix": ["STRING", {}],
                }
            },
        },
    }


class _ComfyUIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _send_json(self, payload, status=200):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        self.server.state.requests[parsed.path] += 1
        if parsed.path == "/system_stats":
            self._send_json({"system": {"comfyui_version": "0.3-test"}, "devices": []})
            return
        if parsed.path == "/object_info":
            self._send_json(self.server.state.object_info)
            return
        if parsed.path.startswith("/history/"):
            prompt_id = parsed.path.rsplit("/", 1)[-1]
            record = self.server.state.history.get(prompt_id)
            self._send_json({prompt_id: record} if record is not None else {})
            return
        if parsed.path == "/view":
            data = self.server.state.artifact_bytes
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        self.server.state.requests[parsed.path] += 1
        if parsed.path != "/prompt":
            self._send_json({"error": "not found"}, status=404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.state.submissions.append(payload)
        prompt_id = f"prompt-{len(self.server.state.submissions)}"
        self.server.state.history[prompt_id] = {
            "status": {"status_str": "success"},
            "outputs": {
                "5": {
                    "images": [
                        {
                            "filename": "result.png",
                            "subfolder": "",
                            "type": "output",
                        }
                    ]
                }
            },
        }
        self._send_json({"prompt_id": prompt_id})


class _FakeComfyUI:
    def __init__(self):
        self.state = types.SimpleNamespace(
            artifact_bytes=b"fake-png-data",
            history={},
            object_info=_object_info(),
            requests=Counter(),
            submissions=[],
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ComfyUIHandler)
        self.httpd.state = self.state
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self):
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def start(self):
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


class ComfyUIMCPServerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workflows_dir = self.root / "workflows"
        self.workflows_dir.mkdir()
        self.comfyui_root = self.root / "ComfyUI"
        (self.comfyui_root / "input").mkdir(parents=True)
        (self.comfyui_root / "main.py").write_text("", encoding="utf-8")
        self.output_dir = self.root / "output"
        self._write_workflows()

        self.comfyui = _FakeComfyUI()
        self.comfyui.start()
        self.server = _load_server(
            {
                "COMFYUI_URL": self.comfyui.url,
                "COMFYUI_ROOT": str(self.comfyui_root),
                "COMFYUI_WORKFLOWS_DIR": str(self.workflows_dir),
                "COMFYUI_MCP_OUTPUT_DIR": str(self.output_dir),
                "COMFYUI_TIMEOUT": "2",
            }
        )

    def tearDown(self):
        self.comfyui.close()
        self.temp_dir.cleanup()

    def _write_workflows(self):
        graph = {
            "1": {
                "class_type": "CheckpointLoaderSimple",
                "inputs": {"ckpt_name": "model.safetensors"},
            },
            "2": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "saved prompt", "clip": ["1", 1]},
            },
            "3": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": 1024, "height": 1024, "batch_size": 1},
            },
            "4": {
                "class_type": "KSampler",
                "inputs": {
                    "model": ["1", 0],
                    "positive": ["2", 0],
                    "latent_image": ["3", 0],
                    "seed": 1,
                    "steps": 20,
                    "cfg": 7.0,
                    "denoise": 1.0,
                },
            },
            "5": {
                "class_type": "SaveImage",
                "inputs": {"images": ["4", 0], "filename_prefix": "test"},
            },
        }
        metadata = {
            "schema_version": 1,
            "parameter_map": {
                "prompt": {"node_id": "2", "input": "text"},
                "model": {"node_id": "1", "input": "ckpt_name"},
            },
            "input_requirements": [
                {
                    "id": "prompt",
                    "label": "Prompt",
                    "type": "text",
                    "public_parameter": "prompt",
                    "node_id": "2",
                    "input": "text",
                    "required": True,
                    "confirm_default": True,
                },
                {
                    "id": "model_default",
                    "label": "Model",
                    "type": "model",
                    "public_parameter": "model",
                    "node_id": "1",
                    "input": "ckpt_name",
                    "required": True,
                    "confirm_default": True,
                }
            ],
        }
        (self.workflows_dir / "ready.json").write_text(json.dumps(graph), encoding="utf-8")
        (self.workflows_dir / "ready.meta.json").write_text(json.dumps(metadata), encoding="utf-8")
        (self.workflows_dir / "canvas.json").write_text(
            json.dumps({"nodes": [], "links": []}), encoding="utf-8"
        )

    def test_legacy_tools_return_json_strings_with_expected_fields(self):
        environment_raw = self.server.get_environment()
        models_raw = self.server.list_models()
        workflows_raw = self.server.list_workflows()
        requirements_raw = self.server.get_workflow_requirements("ready")
        validation_raw = self.server.validate_workflow("ready")

        for raw in (environment_raw, models_raw, workflows_raw, requirements_raw, validation_raw):
            self.assertIsInstance(raw, str)

        environment = json.loads(environment_raw)
        self.assertTrue(environment["comfyui_reachable"])
        self.assertIn("python_executable", environment)
        self.assertIn("workflows_dir", environment)

        models = json.loads(models_raw)
        self.assertEqual(
            set(models),
            {"CheckpointLoaderSimple", "LoraLoader", "VAELoader", "ControlNetLoader"},
        )

        workflows = json.loads(workflows_raw)
        statuses = {item["name"]: item["status"] for item in workflows["workflows"]}
        self.assertEqual(statuses, {"canvas": "needs_api_export", "ready": "ready"})
        self.assertIn("next_step", workflows)

        requirements = json.loads(requirements_raw)
        self.assertEqual(requirements["status"], "needs_user_input")
        self.assertIn("parameter_map", requirements)
        self.assertIn("current_parameters", requirements)

        validation = json.loads(validation_raw)
        self.assertTrue(validation["ready"])
        self.assertIn("input_requirements", validation)

    def test_filtered_model_search_is_compact_and_paginated(self):
        loras = [f"regular-{index:03}.safetensors" for index in range(200)]
        loras.extend(f"special-{index:03}.safetensors" for index in range(25))
        self.comfyui.state.object_info = _object_info(loras=loras)

        legacy_raw = self.server.list_models()
        compact_raw = self.server.list_models(kind="lora", query="SPECIAL", limit=2, offset=1)
        compact = json.loads(compact_raw)

        self.assertNotIn("\n", compact_raw)
        self.assertEqual(compact["kind"], "lora")
        self.assertEqual(compact["total"], 25)
        self.assertEqual(compact["items"], ["special-001.safetensors", "special-002.safetensors"])
        self.assertTrue(compact["truncated"])
        self.assertLess(len(compact_raw), len(legacy_raw) * 0.2)

    def test_invalid_model_search_does_not_fetch_object_info(self):
        invalid_kind = json.loads(self.server.list_models(kind="embedding"))
        missing_kind = json.loads(self.server.list_models(query="portrait"))
        invalid_limit = json.loads(self.server.list_models(kind="lora", limit=201))

        self.assertIn("allowed_kinds", invalid_kind)
        self.assertIn("kind is required", missing_kind["error"])
        self.assertIn("limit must be", invalid_limit["error"])
        self.assertEqual(self.comfyui.state.requests["/object_info"], 0)

    def test_run_does_not_submit_unconfirmed_saved_prompt(self):
        result = json.loads(self.server.run_workflow(workflow_name="ready"))

        self.assertEqual(result["status"], "needs_user_input")
        self.assertEqual(result["unconfirmed_defaults"][0]["id"], "prompt")
        self.assertEqual(self.comfyui.state.requests["/prompt"], 0)
        self.assertEqual(self.comfyui.state.submissions, [])

    def test_run_submits_effective_graph_and_downloads_artifacts(self):
        result = json.loads(
            self.server.run_workflow(
                workflow_name="ready",
                prompt="new prompt",
                model="model.safetensors",
            )
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["prompt_id"], "prompt-1")
        self.assertIn("artifacts", result)
        self.assertIn("images", result)
        self.assertIn("videos", result)
        self.assertEqual([], result["videos"])
        self.assertEqual(len(self.comfyui.state.submissions), 1)
        submitted_graph = self.comfyui.state.submissions[0]["prompt"]
        self.assertEqual(submitted_graph["2"]["inputs"]["text"], "new prompt")
        self.assertEqual(submitted_graph["1"]["inputs"]["ckpt_name"], "model.safetensors")
        local_path = Path(result["artifacts"][0]["local_path"])
        self.assertEqual(local_path.read_bytes(), self.comfyui.state.artifact_bytes)

    def test_get_image_returns_video_artifacts_for_completed_video_prompt(self):
        self.comfyui.state.history["video-prompt"] = {
            "status": {"status_str": "success"},
            "outputs": {
                "5": {
                    "videos": [{"filename": "result.mp4", "subfolder": "", "type": "output"}],
                }
            },
        }

        result = json.loads(self.server.get_image("video-prompt"))

        self.assertEqual("success", result["status"])
        self.assertEqual(1, len(result["artifacts"]))
        self.assertEqual(1, len(result["videos"]))
        self.assertTrue(result["videos"][0].endswith("result.mp4"))

    def test_get_image_classifies_savevideo_mp4_even_when_comfyui_uses_images_key(self):
        self.comfyui.state.history["savevideo-prompt"] = {
            "status": {"status_str": "success"},
            "outputs": {
                "285": {
                    "images": [{"filename": "segment.mp4", "subfolder": "BOSS", "type": "output"}],
                }
            },
        }

        result = json.loads(self.server.get_image("savevideo-prompt"))

        self.assertEqual("images", result["artifacts"][0]["kind"])
        self.assertEqual(1, len(result["videos"]))
        self.assertTrue(result["videos"][0].endswith("segment.mp4"))

    def test_confirming_one_default_does_not_confirm_other_defaults(self):
        result = json.loads(
            self.server.run_workflow(
                workflow_name="ready",
                confirmed_default_ids='["prompt"]',
            )
        )

        self.assertEqual(result["status"], "needs_user_input")
        self.assertEqual(
            [item["id"] for item in result["unconfirmed_defaults"]],
            ["model_default"],
        )
        self.assertEqual(self.comfyui.state.requests["/prompt"], 0)

    def test_confirming_all_defaults_by_id_allows_submission(self):
        result = json.loads(
            self.server.run_workflow(
                workflow_name="ready",
                confirmed_default_ids='["prompt", "model_default"]',
            )
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(self.comfyui.state.submissions), 1)

    def test_legacy_confirm_defaults_still_accepts_all_defaults(self):
        result = json.loads(
            self.server.run_workflow(workflow_name="ready", confirm_defaults=True)
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(self.comfyui.state.submissions), 1)

    def test_unknown_confirmed_default_id_is_rejected_without_submission(self):
        result = json.loads(
            self.server.run_workflow(
                workflow_name="ready",
                confirmed_default_ids='["not-a-requirement"]',
            )
        )

        self.assertEqual(result["status"], "error")
        self.assertIn("unknown or non-confirmable IDs", result["error"])
        self.assertEqual(self.comfyui.state.requests["/prompt"], 0)

    def test_malformed_confirmed_default_ids_are_rejected_without_submission(self):
        for value in ("not-json", "{}", '["", "prompt"]'):
            with self.subTest(value=value):
                result = json.loads(
                    self.server.run_workflow(
                        workflow_name="ready",
                        confirmed_default_ids=value,
                    )
                )
                self.assertEqual(result["status"], "error")
                self.assertIn("confirmed_default_ids must be", result["error"])
        self.assertEqual(self.comfyui.state.requests["/prompt"], 0)

    def test_duplicate_requirement_ids_make_metadata_invalid(self):
        metadata_path = self.workflows_dir / "ready.meta.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["input_requirements"].append(dict(metadata["input_requirements"][0]))
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        workflows = json.loads(self.server.list_workflows())
        ready = next(item for item in workflows["workflows"] if item["name"] == "ready")

        self.assertEqual(ready["status"], "invalid_metadata")
        self.assertIn("duplicate input requirement id: prompt", ready["errors"])

    def test_validation_reports_a_missing_model(self):
        self.comfyui.state.object_info = _object_info(checkpoints=["other.safetensors"])

        result = json.loads(self.server.validate_workflow("ready"))

        self.assertFalse(result["ready"])
        self.assertEqual(result["missing_assets"][0]["value"], "model.safetensors")
        self.assertEqual(self.comfyui.state.requests["/object_info"], 1)

    def test_inline_ui_workflow_is_rejected_without_submission(self):
        result = json.loads(
            self.server.run_workflow(workflow_json=json.dumps({"nodes": [], "links": []}))
        )

        self.assertEqual(result["status"], "error")
        self.assertIn("API format", result["error"])
        self.assertEqual(self.comfyui.state.requests["/prompt"], 0)

    def test_inline_workflow_requires_saved_prompt_confirmation(self):
        graph = json.loads((self.workflows_dir / "ready.json").read_text(encoding="utf-8"))

        result = json.loads(self.server.run_workflow(workflow_json=json.dumps(graph)))

        self.assertEqual("needs_user_input", result["status"])
        self.assertEqual("prompt", result["unconfirmed_defaults"][0]["id"])
        self.assertEqual(self.comfyui.state.requests["/prompt"], 0)
        self.assertEqual(self.comfyui.state.submissions, [])

    def test_non_object_extra_inputs_are_rejected_without_submission(self):
        result = json.loads(
            self.server.run_workflow(
                workflow_name="ready",
                prompt="new prompt",
                model="model.safetensors",
                extra_inputs="[]",
            )
        )

        self.assertEqual("error", result["status"])
        self.assertIn("extra_inputs must decode to an object", result["error"])
        self.assertEqual(self.comfyui.state.requests["/prompt"], 0)


if __name__ == "__main__":
    unittest.main()
