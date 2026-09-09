"""Naiba Chat 后端包。

分层规则（见 docs/dev/模块化重构设计方案.md §2.1 / §0.6）：
- 低层不得 import 高层；本包内任何模块不得 import 根目录的 server；
- 跨模块共享一律走参数 / 注入 / Protocol，不依赖全局单例与字符串键契约。

导入期副作用（唯一一处，显式声明）：把 stdout/stderr 切成 UTF-8。
理由：Windows 英文控制台（cp1252）下，包内任何一处中文输出都会抛
``UnicodeEncodeError``——库代码不该假定宿主控制台编码（GitHub Actions
windows runner 实测：Job 产物写回消息的中文 print 直接让用例失败）。
实现复用 ``naiba.core.diagnostics.ensure_utf8_stdio``（唯一实现），
无控制台（pythonw / 冻结版）时静默跳过。
"""

from naiba.core.diagnostics import ensure_utf8_stdio as _ensure_utf8_stdio

_ensure_utf8_stdio()
