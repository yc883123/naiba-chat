"""执行信号异常（原 skill_runtime.TaskCancelled，供技能层/工具层/运行层共用）。"""

from __future__ import annotations


class TaskCancelled(RuntimeError):
    """用户取消/停止信号：模型 HTTP 取消与工具确认取消统一以该异常中断执行。"""
    pass
