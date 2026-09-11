# -*- coding: utf-8 -*-
"""护栏：Skill 安装必须落在独立子目录，删除绝不能搬走托管目录本身。

背景 bug：单文件 ``.md`` 导入曾把 ``SKILL.md`` 直接写在托管 Skills 目录根下，该 Skill 的
``root`` 因此等于托管目录本身；删除时 ``delete_skill`` 的 ``shutil.move`` 会把整个托管
目录搬进回收目录，连带删掉目录下的所有 Skill。
"""

import base64
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from naiba.app import NaibaChatApp  # noqa: E402
from naiba.skills.catalog import SkillCatalog  # noqa: E402
from naiba.skills.install import validate_and_install_skill  # noqa: E402
from naiba.paths import PathContext  # noqa: E402

SINGLE_MD = "---\nname: flat-skill\ndescription: 单文件技能\n---\n\n正文\n"
OTHER_MD = "---\nname: other-skill\ndescription: 另一个技能\n---\n"
BUILTIN_MD = "---\nname: builtin-skill\ndescription: 内置技能\n---\n\n正文\n"
LEGACY_MD = "---\nname: legacy-skill\ndescription: 旧目录技能\n---\n"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class SkillInstallLayoutTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name).resolve()
        # 目录 0 = 内置（占位存在即可），目录 1 = 托管安装目录；catalog 的 source 按目录位置判定。
        (root / "skills").mkdir()
        (root / "data" / "skills").mkdir(parents=True)
        self.app = NaibaChatApp(paths=PathContext.local(root, root / "config.json"))
        self.assertGreaterEqual(len(self.app.catalog.directories), 2)
        self.managed = self.app.config.resolve_managed_skills_dir().resolve()
        self.assertEqual(self.app.catalog.directories[1].resolve(), self.managed)
        self.recycle = (self.app.paths.data_dir / "skills_recycle").resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def _install_folder(self, files):
        payload, status = self.app._install_folder({"files": files})
        self.assertEqual(int(status), 200, payload)
        return payload

    def test_single_md_lands_in_own_subdirectory(self):
        self._install_folder([{"path": "SKILL.md", "data": _b64(SINGLE_MD)}])
        self.assertFalse((self.managed / "SKILL.md").exists(), "托管目录根下不得裸放 SKILL.md")
        skill_file = self.managed / "flat-skill" / "SKILL.md"
        self.assertTrue(skill_file.is_file())
        item = next(s for s in SkillCatalog([self.managed]).scan() if s["name"] == "flat-skill")
        self.assertEqual(Path(item["root"]).resolve(), skill_file.parent.resolve())

    def test_direct_single_md_install_lands_in_own_subdirectory(self):
        source = Path(self._tmp.name) / "single.md"
        source.write_text(SINGLE_MD, encoding="utf-8")
        result = validate_and_install_skill(source, self.managed)
        self.assertTrue(result["success"], result)
        self.assertTrue((self.managed / "flat-skill" / "SKILL.md").is_file())
        self.assertFalse((self.managed / "SKILL.md").exists())

    def test_plain_single_md_gets_minimal_frontmatter(self):
        source = Path(self._tmp.name) / "plain.md"
        source.write_text("# 我的技能\n\n把这段内容交给 AI。\n", encoding="utf-8")
        result = validate_and_install_skill(source, self.managed)
        self.assertTrue(result["success"], result)
        installed = self.managed / "我的技能" / "SKILL.md"
        self.assertTrue(installed.is_file())
        text = installed.read_text(encoding="utf-8")
        self.assertIn("name: 我的技能", text)
        self.assertIn("description: 把这段内容交给 AI。", text)

    def test_folder_upload_keeps_its_top_level_directory(self):
        self._install_folder([
            {"path": "pack/SKILL.md", "data": _b64(OTHER_MD)},
            {"path": "pack/scripts/run.py", "data": _b64("print(1)\n")},
        ])
        self.assertTrue((self.managed / "pack" / "SKILL.md").is_file())
        self.assertTrue((self.managed / "pack" / "scripts" / "run.py").is_file())
        self.assertFalse((self.managed / "pack" / "pack").exists(), "带顶层目录的上传不得再包一层")

    def test_zip_with_root_skill_md_lands_in_own_subdirectory(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("SKILL.md", SINGLE_MD)
            archive.writestr("scripts/run.py", "print(1)\n")
        payload, status = self.app._install_skill({
            "name": "flat-skill.zip",
            "data": base64.b64encode(buffer.getvalue()).decode("ascii"),
        })
        self.assertEqual(int(status), 200, payload)
        self.assertFalse((self.managed / "SKILL.md").exists(), "压缩包顶层散放的定义也必须收进子目录")
        self.assertTrue((self.managed / "flat-skill" / "SKILL.md").is_file())

    def test_delete_bare_skill_keeps_other_skills(self):
        """历史遗留形态：托管目录根下裸放 SKILL.md（旧版单文件导入的产物）。"""
        (self.managed / "SKILL.md").write_text(SINGLE_MD, encoding="utf-8")
        other_dir = self.managed / "other-skill"
        other_dir.mkdir()
        (other_dir / "SKILL.md").write_text(OTHER_MD, encoding="utf-8")
        bare = next(s for s in self.app.catalog.by_id().values() if s["name"] == "flat-skill")
        self.assertEqual(Path(bare["root"]).resolve(), self.managed)

        payload, status = self.app._delete_skill_by_id(bare["id"])
        self.assertEqual(int(status), 200, payload)
        self.assertTrue(self.managed.is_dir(), "托管目录本身绝不能被搬走")
        self.assertTrue((other_dir / "SKILL.md").is_file(), "其它 Skill 不得被连带删除")
        self.assertFalse((self.managed / "SKILL.md").exists(), "裸定义文件应被回收")
        self.assertEqual(len(list(self.recycle.rglob("SKILL.md"))), 1)
        self.assertNotIn("flat-skill", [s["name"] for s in payload["skills"]])

    def test_startup_migrates_bare_skill_and_preserves_references(self):
        """升级后启动会迁移旧布局，并把 Agent 与隐藏状态切换到新的稳定 ID。"""
        (self.managed / "SKILL.md").write_text(SINGLE_MD, encoding="utf-8")
        old = next(s for s in SkillCatalog([self.managed]).scan() if s["name"] == "flat-skill")
        agent = self.app.config.upsert_agent({
            "name": "迁移引用", "system_prompt": "", "skill_ids": [old["id"]],
        })
        self.app.config.hide_skill(old["id"])

        upgraded = NaibaChatApp(paths=self.app.paths)
        skill_file = upgraded.config.resolve_managed_skills_dir() / "flat-skill" / "SKILL.md"
        self.assertTrue(skill_file.is_file())
        self.assertFalse((self.managed / "SKILL.md").exists())
        new = next(s for s in SkillCatalog([self.managed]).scan() if s["name"] == "flat-skill")
        self.assertNotEqual(old["id"], new["id"])
        saved_agent = next(a for a in upgraded.config.public_agents() if a["id"] == agent["id"])
        self.assertEqual(saved_agent["skill_ids"], [new["id"]])
        self.assertIn(new["id"], upgraded.config.get_hidden_skill_ids())
        self.assertNotIn(old["id"], upgraded.config.get_hidden_skill_ids())

    def test_delete_managed_skill_moves_only_its_directory(self):
        self._install_folder([
            {"path": "pack/SKILL.md", "data": _b64(OTHER_MD)},
            {"path": "keep/SKILL.md", "data": _b64("---\nname: keep-skill\ndescription: 保留\n---\n")},
        ])
        target = next(s for s in self.app.catalog.by_id().values() if s["name"] == "other-skill")
        payload, status = self.app._delete_skill_by_id(target["id"])
        self.assertEqual(int(status), 200, payload)
        self.assertFalse((self.managed / "pack").exists())
        self.assertTrue((self.managed / "keep" / "SKILL.md").is_file())
        self.assertTrue((self.recycle / "pack" / "SKILL.md").is_file())

    def test_permanent_delete_managed_skill_removes_its_directory(self):
        self._install_folder([{"path": "pack/SKILL.md", "data": _b64(OTHER_MD)}])
        target = next(s for s in self.app.catalog.by_id().values() if s["name"] == "other-skill")
        payload, status = self.app._delete_skill({"skill_id": target["id"], "mode": "permanent"})
        self.assertEqual(int(status), 200, payload)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["permanently_deleted"])
        self.assertIsNone(payload["recycled_to"])
        self.assertFalse(payload["recycled_skills"])
        self.assertNotIn(target["id"], [skill["id"] for skill in payload["skills"]])
        self.assertFalse((self.managed / "pack").exists())
        self.assertFalse(self.recycle.exists())

    def test_hiding_bundled_skill_recycles_its_managed_copy(self):
        bundled = self.app.paths.resource_dir / "skills" / "builtin-skill"
        bundled.mkdir(parents=True)
        (bundled / "SKILL.md").write_text(BUILTIN_MD, encoding="utf-8")
        app = NaibaChatApp(paths=self.app.paths)
        item = next(s for s in app.catalog.scan() if s["name"] == "builtin-skill")
        payload, status = app._delete_skill({"skill_id": item["id"], "mode": "recycle"})
        self.assertEqual(int(status), 200, payload)
        self.assertTrue(payload["hidden"])
        self.assertFalse((self.managed / "builtin-skill").exists())
        self.assertTrue((self.recycle / "builtin-skill" / "SKILL.md").is_file())

    def test_clear_skill_recycle_permanently_removes_its_contents(self):
        recycled = self.recycle / "old-skill"
        recycled.mkdir(parents=True)
        (recycled / "SKILL.md").write_text(SINGLE_MD, encoding="utf-8")
        payload, status = self.app._clear_skill_recycle({})
        self.assertEqual(int(status), 200, payload)
        self.assertEqual(payload["deleted"], 1)
        self.assertTrue(self.recycle.is_dir())
        self.assertFalse(any(self.recycle.iterdir()))

    def test_recycled_skill_can_be_restored(self):
        self._install_folder([{"path": "pack/SKILL.md", "data": _b64(OTHER_MD)}])
        target = next(s for s in self.app.catalog.by_id().values() if s["name"] == "other-skill")
        self.app._delete_skill({"skill_id": target["id"], "mode": "recycle"})
        entry = self.app._recycled_skill_entries()[0]
        payload, status = self.app._restore_skill_recycle({"entry": entry["entry"]})
        self.assertEqual(int(status), 200, payload)
        self.assertTrue((self.managed / "pack" / "SKILL.md").is_file())
        self.assertFalse(self.app._recycled_skill_entries())

    def test_bundled_skill_delete_hides_in_one_click_and_never_revives(self):
        """冻结版历史 bug：内置 Skill 被托管副本遮蔽时，删除只搬走副本 → 立刻"复活"。

        现在这类 Skill 的真实来源标为 builtin、并带重复目录信息；删除走隐藏，
        一次点击即从列表消失，且重启（内置同步 + 旧目录合并）不会把它带回来。
        """
        bundled = self.app.paths.resource_dir / "skills" / "builtin-skill"
        bundled.mkdir(parents=True)
        (bundled / "SKILL.md").write_text(BUILTIN_MD, encoding="utf-8")

        app = NaibaChatApp(paths=self.app.paths)
        item = next(s for s in app.catalog.scan() if s["name"] == "builtin-skill")
        self.assertEqual(item["source"], "builtin", "托管副本不得遮蔽内置来源")
        self.assertTrue(item["duplicate_dirs"], "应报告同一 Skill 的重复目录")
        self.assertTrue((self.managed / "builtin-skill" / "SKILL.md").is_file())

        payload, status = app._delete_skill_by_id(item["id"])
        self.assertEqual(int(status), 200, payload)
        self.assertTrue(payload.get("hidden"))
        self.assertTrue(payload.get("recycled_to"), "内置 Skill 的托管副本应移入回收目录")
        self.assertFalse((self.managed / "builtin-skill").exists())
        self.assertTrue((self.recycle / "builtin-skill" / "SKILL.md").is_file())
        self.assertNotIn("builtin-skill", [s["name"] for s in payload["skills"]])

        restarted = NaibaChatApp(paths=self.app.paths)
        self.assertTrue((bundled / "SKILL.md").is_file(), "内置原件不允许被删除")
        self.assertNotIn(item["id"], [s["id"] for s in restarted.catalog.scan()])

    def test_hidden_bundled_skill_is_not_resynced_into_managed(self):
        """已隐藏（删除）的内置 Skill 不得在启动时被重新写回托管目录。"""
        app = NaibaChatApp(paths=self.app.paths)
        (self.managed / "legacy-skill").mkdir(parents=True)
        (self.managed / "legacy-skill" / "SKILL.md").write_text(LEGACY_MD, encoding="utf-8")
        item = next(s for s in app.catalog.by_id().values() if s["name"] == "legacy-skill")
        payload, status = app._delete_skill_by_id(item["id"])
        self.assertEqual(int(status), 200, payload)
        self.assertTrue((self.recycle / "legacy-skill" / "SKILL.md").is_file())

        # 旧目录（resource_dir/skills 之外的 data 同级 skills）里放一份同名 Skill，
        # 模拟"删除后旧目录合并又把它复制回来"的复活路径。
        legacy_dir = self.app.paths.data_dir.parent / "skills" / "legacy-skill"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "SKILL.md").write_text(LEGACY_MD, encoding="utf-8")
        app.config.hide_skill(item["id"])
        NaibaChatApp(paths=self.app.paths)
        self.assertFalse((self.managed / "legacy-skill").exists(),
                         "已隐藏的 Skill 不得被旧目录合并重新复制回托管目录")

