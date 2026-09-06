# -*- coding: utf-8 -*-
"""路径上下文：统一管理源码/冻结双模式下的目录与文件路径（层级 0，零项目依赖）。

打包成 exe（PyInstaller）后，``__file__`` 指向临时解压目录，不能用于读写运行数据。
目录分三类（语义传承自原 server.py）：
  - EXE_DIR：exe 所在目录（仅冻结时与仓库根不同），用于默认工作区与定位相邻旧数据。
  - RESOURCE_DIR：静态资源（public 等），随 exe 打包，运行时从 sys._MEIPASS 读取。
  - APP_DIR：可写运行数据目录（config.json / data / skills），冻结版固定到
    ``%LOCALAPPDATA%\\NaibaChat``；源码模式继续使用仓库目录。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PathContext:
    """进程级路径上下文。

    ``data_dir`` 可在配置载入后被 ``rebind_data_dir`` 重置——对应原 server.py 中
    ``NaibaChatApp.__init__`` 的二次重绑定语义（用户配置自定义 data_dir 时切换），
    任何读取方必须经同一对象访问，避免 import 顺序改变取值。
    """

    app_dir: Path
    exe_dir: Path
    resource_dir: Path
    public_dir: Path
    config_path: Path
    data_dir: Path
    status_path: Path
    lock_path: Path

    def rebind_data_dir(self, value: Path) -> Path:
        """按配置切换数据目录：同步 data_dir/status_path/lock_path，返回新目录。"""
        self.data_dir = value.resolve()
        self.status_path = self.data_dir / "server.json"
        self.lock_path = self.data_dir / "server.lock"
        return self.data_dir

    @classmethod
    def local(cls, root: Path, config_path: Path) -> "PathContext":
        """以单目录为根的本地上下文（供测试等无生产路径场景构造）。"""
        root = Path(root).resolve()
        config_path = Path(config_path).resolve()
        data_dir = (root / "data").resolve()
        return cls(
            app_dir=root,
            exe_dir=root,
            resource_dir=root,
            public_dir=(root / "public").resolve(),
            config_path=config_path,
            data_dir=data_dir,
            status_path=data_dir / "server.json",
            lock_path=data_dir / "server.lock",
        )


def _configured_data_dir(config_path: Path, app_dir: Path) -> Path:
    """在 ConfigStore 初始化前解析持久化数据目录。"""
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
        raw = loaded.get("data_dir") if isinstance(loaded, dict) else ""
    except (OSError, json.JSONDecodeError):
        raw = ""
    path = Path(str(raw or "data")).expanduser()
    if not path.is_absolute():
        path = app_dir / path
    return path.resolve()


def default_path_context() -> PathContext:
    """构造进程默认路径上下文（保留原 server.py 导入时序的 sys.path 副作用）。"""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        resource_dir = Path(getattr(sys, "_MEIPASS", exe_dir)).resolve()
        localappdata = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        app_dir = Path(localappdata).resolve() / "NaibaChat"
    else:
        # 源码模式：以仓库根（本文件上一级）为 EXE/资源/应用目录。
        exe_dir = Path(__file__).resolve().parent.parent
        resource_dir = exe_dir
        app_dir = exe_dir
    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))
    if str(exe_dir) not in sys.path and str(exe_dir) != str(app_dir):
        sys.path.insert(0, str(exe_dir))
    public_dir = resource_dir / "public"
    config_path = app_dir / "config.json"
    data_dir = _configured_data_dir(config_path, app_dir)
    return PathContext(
        app_dir=app_dir,
        exe_dir=exe_dir,
        resource_dir=resource_dir,
        public_dir=public_dir,
        config_path=config_path,
        data_dir=data_dir,
        status_path=data_dir / "server.json",
        lock_path=data_dir / "server.lock",
    )


_STATIC_ASSET_VERSION: str | None = None


def static_asset_version(public_dir: Path) -> str:
    """静态资源版本哈希（lazy-once：首次计算后缓存，禁止每请求重算磁盘 I/O）。"""
    global _STATIC_ASSET_VERSION
    if _STATIC_ASSET_VERSION is None:
        digest = hashlib.sha256()
        for name in ("app.js", "styles.css"):
            digest.update((public_dir / name).read_bytes())
        _STATIC_ASSET_VERSION = digest.hexdigest()[:12]
    return _STATIC_ASSET_VERSION
