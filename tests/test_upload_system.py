# -*- coding: utf-8 -*-
"""上传系统守门：分日落盘、内容去重、图片压缩+缩略图、删除保护、缓存组键。"""
import io
import tempfile
import unittest
from pathlib import Path

from naiba.storage.media import (
    IMAGE_CACHE_CLEAN_LIMIT,
    UPLOAD_MAX_BYTES,
    _clean_uploads_cache,
    _fit_image_pixels,
    _process_uploaded_image,
    auto_clean_uploads,
    is_uploads_path,
    remove_uploaded_file,
    store_uploaded_file,
    upload_target_dir,
)
from naiba.storage.store import ChatStorage


def _small_png(width: int = 8, height: int = 8, color=(255, 0, 0)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _small_pdf(text: bytes = b"PDF-BODY") -> bytes:
    import zlib

    stream = b"BT /F1 12 Tf 20 50 Td (" + text + b") Tj ET"
    compressed = zlib.compress(stream)
    return (
        b"\x25PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 100]/Contents 4 0 R>>endobj\n"
        b"4 0 obj<</Length " + str(len(compressed)).encode() + b">>stream\n" + compressed
        + b"\nendstream endobj\ntrailer<</Root 1 0 R>>\n\x25\x25EOF\n"
    )


class StoreUploadedFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba_upload_test_"))
        self.data_dir = self.tmp / "data"

    def test_non_image_lands_in_dated_dir(self) -> None:
        pdf = _small_pdf()
        result = store_uploaded_file(pdf, "测试文档.pdf", self.data_dir)
        path = Path(result["path"])
        self.assertTrue(path.is_file())
        # 分日目录：uploads/YYYY-MM-DD/
        self.assertTrue(path.parent.parent.name == "uploads")
        self.assertRegex(path.parent.name, r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(path.read_bytes(), pdf)
        self.assertEqual(result["thumb_path"], "")
        self.assertFalse(result["deduped"])

    def test_duplicate_content_deduped(self) -> None:
        pdf = _small_pdf()
        first = store_uploaded_file(pdf, "a.pdf", self.data_dir)
        second = store_uploaded_file(pdf, "b.pdf", self.data_dir)
        self.assertTrue(second["deduped"])
        self.assertEqual(Path(second["path"]).resolve(), Path(first["path"]).resolve())
        # 仅一份文件 + 空索引目录无残留
        self.assertEqual(sum(1 for _ in (self.data_dir / "uploads").rglob("*") if _.is_file()), 1)

    def test_image_gets_thumb_and_compress(self) -> None:
        png = _small_png(4000, 4000)  # 16M 像素 > 默认 2M
        result = store_uploaded_file(
            png, "big.png", self.data_dir,
            {"image_upload_original": False, "image_max_pixels": 2000000, "thumbnail_max_pixels": 500000},
        )
        main = Path(result["path"])
        self.assertTrue(main.is_file())
        # 压缩后小于原图（像素缩到 2M 以内）
        from PIL import Image

        with Image.open(main) as img:
            self.assertLessEqual(img.width * img.height, 2000000)
        self.assertTrue(result["thumb_path"])
        self.assertTrue(Path(result["thumb_path"]).is_file())
        # 缩略图必须与主图同 stem：前端兜底、去重复用与删除成组都按此约定推导。
        self.assertEqual(Path(result["thumb_path"]).name, f"{main.stem}_thumb.webp")
        # 同内容再传一次：命中去重并复用同一份缩略图（不能返回空 thumb_path）。
        again = store_uploaded_file(
            png, "big-again.png", self.data_dir,
            {"image_upload_original": False, "image_max_pixels": 2000000, "thumbnail_max_pixels": 500000},
        )
        self.assertTrue(again["deduped"])
        self.assertEqual(Path(again["thumb_path"]).resolve(), Path(result["thumb_path"]).resolve())

    def test_auto_clean_with_reference_guard(self) -> None:
        """B1：自动清理带引用保护——被引用的最旧组永久保留，只删未引用组。"""
        from datetime import datetime, timedelta

        # 旧组（被引用）与新组（未引用）
        old_pdf = _small_pdf(b"OLD-REFERENCED")
        new_pdf = _small_pdf(b"NEW-UNREFERENCED")
        old_dir = upload_target_dir(self.data_dir, datetime.now() - timedelta(days=2))
        old_dir.mkdir(parents=True, exist_ok=True)
        old_file = old_dir / "naiba_chat_old_ref.pdf"
        old_file.write_bytes(old_pdf)
        new_dir = upload_target_dir(self.data_dir)
        new_dir.mkdir(parents=True, exist_ok=True)
        new_file = new_dir / "naiba_chat_new_unref.pdf"
        new_file.write_bytes(new_pdf)

        def checker(path: Path) -> bool:
            return path.name == old_file.name  # 旧组被"消息引用"

        result = _clean_uploads_cache(limit=1, data_dir=self.data_dir, referenced_checker=checker)
        self.assertTrue(old_file.is_file(), "被引用的旧组不得被自动清理")
        self.assertFalse(new_file.exists(), "未引用组超限时应被删除")
        self.assertEqual(result["removed"], 1)

    def test_manual_clean_without_checker_keeps_referenced_unaware(self) -> None:
        """手动清理（无 checker）保持按时间保留语义，不感知引用。"""
        import os
        from datetime import datetime, timedelta

        pdf = _small_pdf()
        old_dir = upload_target_dir(self.data_dir, datetime.now() - timedelta(days=2))
        old_dir.mkdir(parents=True, exist_ok=True)
        old_file = old_dir / "naiba_chat_old.pdf"
        old_file.write_bytes(pdf)
        old_mtime = (datetime.now() - timedelta(days=2)).timestamp()
        os.utime(old_file, (old_mtime, old_mtime))
        new_dir = upload_target_dir(self.data_dir)
        new_dir.mkdir(parents=True, exist_ok=True)
        new_file = new_dir / "naiba_chat_new.pdf"
        new_file.write_bytes(pdf)
        # limit 单位是字节：设为新文件大小+1（可容纳最新组，最旧组必超）
        result = _clean_uploads_cache(limit=new_file.stat().st_size + 1, data_dir=self.data_dir)
        self.assertEqual(result["removed"], 1)  # 删最旧
        self.assertFalse(old_file.exists())
        self.assertTrue(new_file.is_file())

    def test_upload_limit_constant_aligned(self) -> None:
        self.assertEqual(UPLOAD_MAX_BYTES, 80 * 1024 * 1024)

    def test_clean_upload_cache_group_key_keeps_date_buckets(self) -> None:
        """分日目录下同名主图不得合并为一个清理组。"""
        from datetime import datetime, timedelta

        pdf = _small_pdf()
        store_uploaded_file(pdf, "同名.pdf", self.data_dir)
        # 模拟昨天上传的同类文件（同 stem）
        yesterday = datetime.now() - timedelta(days=1)
        old_dir = upload_target_dir(self.data_dir, yesterday)
        old_dir.mkdir(parents=True, exist_ok=True)
        (old_dir / "naiba_chat_old_same.pdf").write_bytes(pdf)
        # 超出 128MB 才会触发清理；这里直接调极限：limit=1 则全部清除
        result = _clean_uploads_cache(limit=1, data_dir=self.data_dir)
        self.assertEqual(result["removed"], 2)  # 两组各 1 文件
        self.assertEqual(result["size"], 0)

    def test_remove_uploaded_file_is_tree_bound(self) -> None:
        pdf = _small_pdf()
        result = store_uploaded_file(pdf, "del.pdf", self.data_dir)
        with self.assertRaises(ValueError):
            remove_uploaded_file(self.data_dir, self.tmp / "outside.pdf")
        self.assertTrue(remove_uploaded_file(self.data_dir, result["path"]))
        self.assertFalse(Path(result["path"]).exists())

    def test_is_uploads_path(self) -> None:
        pdf = _small_pdf()
        result = store_uploaded_file(pdf, "x.pdf", self.data_dir)
        self.assertTrue(is_uploads_path(self.data_dir, result["path"]))
        self.assertFalse(is_uploads_path(self.data_dir, self.tmp / "nope.pdf"))

    def test_auto_clean_trigger_only_over_limit(self) -> None:
        # 空目录不触发
        self.assertIsNone(auto_clean_uploads(self.data_dir, limit=1))
        store_uploaded_file(_small_pdf(), "keep.pdf", self.data_dir)
        # limit=0 必然超限：返回带 trigger 的清理结果
        result = auto_clean_uploads(self.data_dir, limit=0)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("trigger"), "auto")


class UploadReferenceGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="naiba_ref_test_"))
        self.storage = ChatStorage(self.tmp / "chat.db")
        conv = self.storage.create_conversation("ref", "引用测试")
        self.conv_id = str(conv.get("id") or "")

    def test_unreferenced_then_referenced(self) -> None:
        target = self.tmp / "uploads" / "naiba_chat_x_file.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_small_pdf())
        self.assertFalse(self.storage.upload_path_referenced(target))
        # 创建消息引用该文件
        self.storage.create_chat_run(
            self.conv_id,
            "总结",
            [{"name": target.name, "path": str(target), "size": 10, "thumb_path": ""}],
            {"id": "general", "name": "通用 Agent", "system_prompt": "", "skill_ids": []},
            {"attachments": [{"name": target.name, "path": str(target), "size": 10, "thumb_path": ""}]},
            "craft",
        )
        self.assertTrue(self.storage.upload_path_referenced(target))


class UploadedImageProcessingTests(unittest.TestCase):
    def test_process_non_image_passthrough(self) -> None:
        pdf = _small_pdf()
        main, thumb_name, thumb_bytes = _process_uploaded_image(pdf, "a.pdf", {})
        self.assertEqual(main, pdf)
        self.assertIsNone(thumb_name)

    def test_fit_pixels(self) -> None:
        from PIL import Image

        img = Image.new("RGB", (200, 100), "blue")
        fitted = _fit_image_pixels(img, 5000)  # 200*100=20000 > 5000
        self.assertLessEqual(fitted.width * fitted.height, 5000)


if __name__ == "__main__":
    unittest.main()
