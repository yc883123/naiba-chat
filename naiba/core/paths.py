"""路径判定原语（层级 0/1）。阶段 3 的 PathContext 将以此为基础扩展。"""

from __future__ import annotations

from pathlib import Path


def path_within(path: Path, root: Path) -> bool:
    """判断 ``path`` 是否位于 ``root`` 内（resolve 后的语义，符号/相对路径由调用方处理）。"""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
