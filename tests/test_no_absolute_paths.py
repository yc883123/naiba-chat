# -*- coding: utf-8 -*-
"""守门：入库的代码/配置里不得出现本机绝对路径（维护说明 §8.8）。

规则：凡提交进仓库的脚本、测试、配置一律从文件位置派生项目根，不写盘符/用户目录字面量。
本测试扫已入库的代码/配置类文件；`docs/` 下的手册生成物与文档里的语义/示例写法按 §一.5 处理，
不在扫描范围内。测试桩里的假路径（不含用户目录）不会误报。
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CODE_EXTS = {
    ".py", ".js", ".mjs", ".cjs", ".json", ".yml", ".yaml", ".spec",
    ".ps1", ".bat", ".html", ".css", ".toml",
}
# 盘符 + 用户目录（占位名放行）
USER_PATH = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+([^\\/\s\"'`)]+)", re.IGNORECASE)
# 盘符 + 仓库目录名（本机检出路径的指纹）
REPO_PATH = re.compile(r"[A-Za-z]:[\\/]+naiba-chat\b", re.IGNORECASE)
PLACEHOLDERS = {"<用户名>", "user", "username", "%username%"}


class NoAbsolutePathTests(unittest.TestCase):
    def _tracked_code_files(self) -> list[str]:
        try:
            result = subprocess.run(
                ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace", check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            self.skipTest(f"不是 git 仓库或 git 不可用：{exc}")
        return [
            line for line in result.stdout.splitlines()
            if Path(line).suffix.lower() in CODE_EXTS and not line.startswith("docs/")
        ]

    def test_no_local_absolute_paths_in_code(self) -> None:
        hits: list[str] = []
        for rel in self._tracked_code_files():
            path = ROOT / rel
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line_no, line in enumerate(text.split("\n"), 1):
                for match in USER_PATH.finditer(line):
                    if match.group(1).lower() in PLACEHOLDERS:
                        continue
                    hits.append(f"{rel}:{line_no} 用户目录路径 {match.group(0)}")
                repo_match = REPO_PATH.search(line)
                if repo_match:
                    hits.append(f"{rel}:{line_no} 仓库盘符路径 {repo_match.group(0)}")
        self.assertEqual(
            hits, [],
            "入库代码不得硬编码本机绝对路径（见维护说明 §8.8），应派生项目根：\n"
            + "\n".join(hits),
        )


if __name__ == "__main__":
    unittest.main()
