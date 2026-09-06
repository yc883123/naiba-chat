# -*- coding: utf-8 -*-
"""护栏：SkillCatalog 扫描（曾在 bootstrap 路径上因辅助函数缺失导致 NameError→500→前端登录弹窗）。

保护对象：后续 skill_runtime 拆分（阶段 2 剩余）时，扫描/前端元数据解析链路的完整性。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.skills.catalog import SkillCatalog  # noqa: E402


class SkillCatalogScanTests(unittest.TestCase):
    def test_scan_reads_frontmatter_and_display_name(self):
        # 注意：扫描会跳过路径中任何以 "." 开头的目录段（如沙箱 .tmptest），
        # 因此临时目录必须建在非点开头的位置（workspace 根下）。
        import tempfile

        workspace = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=str(workspace)) as tmp:
            root = Path(tmp) / "skills"
            skill_dir = root / "demo-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: demo\ndescription: 演示技能\n---\n\n# 演示\n",
                encoding="utf-8",
            )
            catalog = SkillCatalog([root])
            items = catalog.scan()
            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual(item.get("name"), "demo")
            self.assertEqual(item.get("description"), "演示技能")
            self.assertEqual(item.get("id"), catalog.by_id()[item["id"]]["id"])


if __name__ == "__main__":
    unittest.main()
