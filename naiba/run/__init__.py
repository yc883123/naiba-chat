"""run 层：一次 Run/Job 执行的编排（会话固化、事件流、运行骨架与主循环）。

阶段 2 拆分计划（顺序，见设计文档 §3.3）：
1. session.py（本阶段完成：快照固化/工具集决议）
2. stream.py（事件 sink + 取消重建）
3. manager.py（注册表/取消/看门狗/插话）
4. chat.py（主循环 _run_chat/_run_plan）
"""

from __future__ import annotations
