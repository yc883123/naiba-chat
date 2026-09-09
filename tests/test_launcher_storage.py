# -*- coding: utf-8 -*-
"""护栏：桌面端 WebView2 profile 持久化。

背景：pywebview 的 ``private_mode`` 默认为 True —— profile 落在临时目录、进程退出即删除，
前端写在 localStorage 的偏好（侧栏宽度 / 文件面板宽度 / 顶栏 Skill 勾选 / 交互模式）
每次启动都被重置；用户保存的「我的工具集」也曾因此重启即消失（现已改存后端 config.json）。

覆盖：launcher 必须显式 private_mode=False + storage_path；目录取 app_dir 而非 data_dir；
目录不可写时回退私有模式而不是崩；源码模式的 webview/ 必须被 .gitignore 忽略。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class LauncherWebviewStorageTests(unittest.TestCase):
    def _launcher(self) -> str:
        return (ROOT / "launcher.py").read_text(encoding="utf-8")

    def test_private_mode_disabled_with_persistent_storage_path(self) -> None:
        source = self._launcher()
        self.assertIn('start_kwargs["private_mode"] = False', source)
        self.assertIn('start_kwargs["storage_path"] = str(storage_dir)', source)
        self.assertIn('webview.start(**start_kwargs)', source)

    def test_storage_dir_is_app_dir_not_data_dir(self) -> None:
        source = self._launcher()
        self.assertIn('srv.APP.paths.app_dir / "webview"', source)
        self.assertNotIn('data_dir / "webview"', source, "别放进数据目录（会被迁移/备份带走）")
        self.assertIn("mkdir(parents=True, exist_ok=True)", source)

    def test_unwritable_storage_falls_back_to_private_mode(self) -> None:
        source = self._launcher()
        block = source[source.index('storage_dir = srv.APP.paths.app_dir / "webview"'):]
        block = block[: block.index("webview.start(")]
        self.assertIn("except OSError", block, "目录不可写要回退，不能让启动挂掉")
        self.assertIn("stderr", block, "回退原因要留痕")

    def test_source_mode_profile_is_gitignored(self) -> None:
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("webview/", ignore, "源码模式 profile 落在仓库根，必须忽略")


if __name__ == "__main__":
    unittest.main()
