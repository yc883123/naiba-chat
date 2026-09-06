# -*- coding: utf-8 -*-
"""护栏：naiba/vision/images 图像原语行为规格（收官线 ① 第一件）。

保护对象：图像编码/尺寸/像素读取原语自 vision/runtime.py 迁出至 images.py 时的
行为等价性——统一 RGB 化、缩略上限、质量阶梯、base64 输出、路径解析与
「缺失文件/越界尺寸返回 None」契约，以及编码字节级确定性（同输入必须同输出）。
"""

import base64
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.vision.images import (  # noqa: E402
    _encode_image_bytes,
    _image_size,
    _make_probe_jpeg_b64,
    _read_rgb,
    encode_image_file,
)


def _png_bytes(size=(64, 64), color=(200, 90, 90), mode="RGB") -> bytes:
    from PIL import Image

    image = Image.new(mode, size, color if mode == "RGB" else (*color, 128))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class VisionImagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_probe_jpeg_is_valid_rgb_jpeg(self):
        from PIL import Image

        data = base64.b64decode(_make_probe_jpeg_b64())
        with Image.open(io.BytesIO(data)) as probe:
            self.assertEqual(probe.size, (32, 32))
            self.assertEqual(probe.mode, "RGB")
            self.assertEqual(probe.format, "JPEG")

    def test_encode_image_file_png_to_jpeg_part(self):
        target = self.root / "pic.png"
        target.write_bytes(_png_bytes())
        part = encode_image_file(str(target))
        self.assertIsNotNone(part)
        self.assertEqual(part["type"], "image")
        self.assertEqual(part["media_type"], "image/jpeg")
        self.assertEqual(part["name"], "pic.png")
        self.assertEqual(part["path"], str(target.resolve()))

    def test_encode_image_file_missing_returns_none(self):
        self.assertIsNone(encode_image_file(str(self.root / "不存在.png")))

    def test_encode_image_bytes_deterministic(self):
        raw = _png_bytes(mode="RGBA")
        first = _encode_image_bytes(raw, "image/png", "a.png")
        second = _encode_image_bytes(raw, "image/png", "a.png")
        self.assertEqual(first["data"], second["data"])
        self.assertEqual(first["media_type"], "image/jpeg")

    def test_encode_image_bytes_non_image_returns_none(self):
        self.assertIsNone(_encode_image_bytes(b"not an image", "image/png"))

    def test_image_size(self):
        target = self.root / "size.png"
        target.write_bytes(_png_bytes())
        self.assertEqual(_image_size(str(target)), (64, 64))
        self.assertIsNone(_image_size(str(self.root / "missing.png")))

    def test_read_rgb_flattens_alpha(self):
        target = self.root / "rgba.png"
        target.write_bytes(_png_bytes(mode="RGBA"))
        image = _read_rgb(str(target))
        self.assertEqual(image.mode, "RGB")
        self.assertEqual(image.size, (64, 64))

    def test_read_rgb_passthrough_rgb(self):
        target = self.root / "rgb.png"
        target.write_bytes(_png_bytes())
        image = _read_rgb(str(target))
        self.assertEqual(image.mode, "RGB")


if __name__ == "__main__":
    unittest.main()
