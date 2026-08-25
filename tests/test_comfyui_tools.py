import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from job_registry import JobRegistry
import server
from server import NaibaChatApp
from skill_runtime import SkillAgent
from tool_registry import build_tool_registry


class ComfyUIToolTests(unittest.TestCase):
    def test_registry_exposes_prepare_and_batch(self):
        registry = build_tool_registry()
        self.assertTrue(registry.has("comfyui_prepare_workflow"))
        self.assertTrue(registry.has("comfyui_batch"))

    def test_prepare_distinguishes_api_and_ui_json(self):
        api = {"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "x"}}}
        self.assertEqual(api, NaibaChatApp._normalize_comfyui_workflow(api))
        with self.assertRaisesRegex(ValueError, "UI JSON"):
            NaibaChatApp._normalize_comfyui_workflow({"nodes": [], "links": []})

    def test_prepare_reads_prompt_wrapper(self):
        api = {"1": {"class_type": "SaveImage", "inputs": {}}}
        self.assertEqual(api, NaibaChatApp._normalize_comfyui_workflow({"prompt": api}))

    def test_comfyui_intent_exposes_prepare_and_batch_without_skill(self):
        allowed = {
            "read_file", "write_file", "run_command", "http_request",
            "run_in_background", "job_output", "job_status", "job_wait",
            "comfyui_prepare_workflow", "comfyui_batch", "capability_inventory",
            "activate_skill",
        }
        schemas = [{"name": name, "description": name, "parameters": {}} for name in allowed]
        visible = SkillAgent._visible_tool_names(
            "ComfyUI 检查工作流并批量生成短剧", allowed, schemas, []
        )
        self.assertIn("comfyui_prepare_workflow", visible)
        self.assertIn("comfyui_batch", visible)
        self.assertIn("write_file", visible)
        self.assertNotIn("activate_skill", visible)

    def test_runtime_workflow_replaces_negative_seed(self):
        workflow = {
            "1": {"class_type": "KSampler", "inputs": {"seed": -1}},
            "2": {"class_type": "SaveImage", "inputs": {}},
        }
        normalized = NaibaChatApp._normalize_comfyui_runtime_workflow(workflow)
        self.assertGreaterEqual(normalized["1"]["inputs"]["seed"], 0)

    def test_comfyui_submit_wraps_workflow_as_prompt(self):
        captured = {}

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit=-1): return b'{"prompt_id":"prompt-1"}'

        def fake_urlopen(request, timeout):
            del timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return Response()

        registry = JobRegistry(SimpleNamespace())
        with patch("job_registry.urllib.request.urlopen", side_effect=fake_urlopen):
            prompt_id = registry._comfyui_submit(
                "http://127.0.0.1:8188",
                {"1": {"class_type": "SaveImage", "inputs": {}}},
                0,
            )
        self.assertEqual("prompt-1", prompt_id)
        self.assertIn("prompt", captured["payload"])

    def test_comfyui_output_urls_cover_images_and_videos(self):
        entry = {"outputs": {"7": {
            "images": [{"filename": "image.png", "subfolder": "", "type": "output"}],
            "videos": [{"filename": "clip.mp4", "subfolder": "video", "type": "output"}],
        }}}
        urls = JobRegistry._comfyui_output_urls("http://127.0.0.1:8188", entry)
        self.assertEqual(2, len(urls))
        self.assertTrue(any("filename=image.png" in url for url in urls))
        self.assertTrue(any("filename=clip.mp4" in url for url in urls))

    def test_extract_attachments_copies_media_into_managed_data_dir(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            outside = root_path / "outside" / "preview.mp4"
            outside.parent.mkdir()
            outside.write_bytes(b"video")
            data_dir = root_path / "data"
            with patch.object(server, "DATA_DIR", data_dir):
                attachments = server.extract_attachments([{"result": str(outside)}])
            cached = Path(attachments[0]["source"])
            self.assertTrue(cached.is_file())
            self.assertEqual((data_dir / "generated").resolve(), cached.parent)

    def test_extract_attachments_structured_image_record_is_single_attachment(self):
        # vision_read_folder 返回 {note, images:[{name,path,thumb_path,...}]}：
        # 应产出单个附件（source=path，thumb_path 作为元数据），而不是把 name /
        # thumb_path 当作独立附件造成缩略图破图。
        result = json.dumps({
            "note": "读取完成",
            "images": [{
                "name": "ComfyUI_00001.png",
                "path": "C:/data/uploads/naiba_chat_123_ComfyUI_00001.png",
                "thumb_path": "C:/data/uploads/naiba_chat_123_ComfyUI_00001_thumb.webp",
                "width": 1024,
                "height": 768,
            }],
        })
        attachments = server.extract_attachments([{"tool": "vision_read_folder", "result": result}])
        self.assertEqual(1, len(attachments))
        self.assertEqual("C:/data/uploads/naiba_chat_123_ComfyUI_00001.png", attachments[0]["source"])
        self.assertEqual("C:/data/uploads/naiba_chat_123_ComfyUI_00001_thumb.webp", attachments[0]["thumb_path"])
        self.assertEqual("ComfyUI_00001.png", attachments[0]["name"])


if __name__ == "__main__":
    unittest.main()
