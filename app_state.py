"""应用级共享状态：路径常量与运行时可变全局。

原属于 server.py 顶部的路径引导逻辑，拆出为独立模块，供 server.py 与各子模块
（image_utils / config_helpers / model_media / config_store）共享，避免循环导入。

- EXE_DIR / RESOURCE_DIR / APP_DIR / PUBLIC_DIR / CONFIG_PATH：启动后不再改变。
- DATA_DIR / STATUS_PATH / LOCK_PATH：随用户配置的数据目录在 NaibaChatApp.__init__ 中重绑定，
  并通过本模块同步给各子模块。
- APP：由 server.main() / launcher 启动时创建 NaibaChatApp 后赋值（在 __init__ 末尾同步）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 打包成 exe（PyInstaller）后，__file__ 指向临时解压目录，不能用于读写运行数据。
# 目录分三类：
#   - EXE_DIR：exe 所在目录（仅冻结时与仓库根不同），用于默认工作区与定位相邻旧数据。
#   - RESOURCE_DIR：静态资源（public 等），随 exe 打包，运行时从 sys._MEIPASS 读取。
#   - APP_DIR：可写运行数据目录（config.json / data / skills），冻结版固定到
#     %LOCALAPPDATA%\NaibaChat；源码模式继续使用仓库目录。
if getattr(sys, "frozen", False):
    EXE_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", EXE_DIR)).resolve()
    _localappdata = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    APP_DIR = Path(_localappdata).resolve() / "NaibaChat"
else:
    EXE_DIR = Path(__file__).resolve().parent
    RESOURCE_DIR = EXE_DIR
    APP_DIR = EXE_DIR
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if str(EXE_DIR) not in sys.path and str(EXE_DIR) != str(APP_DIR):
    sys.path.insert(0, str(EXE_DIR))


PUBLIC_DIR = RESOURCE_DIR / "public"
CONFIG_PATH = APP_DIR / "config.json"


def _configured_data_dir() -> Path:
    """Resolve the persistent data directory before ConfigStore is initialized."""
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        raw = loaded.get("data_dir") if isinstance(loaded, dict) else ""
    except (OSError, json.JSONDecodeError):
        raw = ""
    path = Path(str(raw or "data")).expanduser()
    if not path.is_absolute():
        path = APP_DIR / path
    return path.resolve()


DATA_DIR = _configured_data_dir()
STATUS_PATH = DATA_DIR / "server.json"
LOCK_PATH = DATA_DIR / "server.lock"

# 运行时由 server.main() / launcher 启动时赋值为 NaibaChatApp 实例（见 NaibaChatApp.__init__）。
APP = None
