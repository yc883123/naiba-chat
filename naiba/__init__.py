"""Naiba Chat 后端包。

分层规则（见 docs/dev/模块化重构设计方案.md §2.1 / §0.6）：
- 低层不得 import 高层；本包内任何模块不得 import 根目录的 server；
- 跨模块共享一律走参数 / 注入 / Protocol，不依赖全局单例与字符串键契约。
"""
