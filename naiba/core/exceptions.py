"""执行信号异常（原 skill_runtime.TaskCancelled 与 run/manager.ActiveRunError，移至叶子层共用）。"""

from __future__ import annotations


class TaskCancelled(RuntimeError):
    """用户取消/停止信号：模型 HTTP 取消与工具确认取消统一以该异常中断执行。"""
    pass


class ActiveRunError(RuntimeError):
    """当前对话已有运行中的任务（第二并发提交被互斥拒绝）。"""

    def __init__(self, run_id: str):
        super().__init__("当前对话已有运行中的任务")
        self.run_id = run_id
