"""上下文窗口常量（原 skill_runtime 模块级；窗口预算方法暂随 SkillAgent 保留于 agent.py）。"""

from __future__ import annotations


# Conservative context ceiling (tokens) used when a provider exposes no window
# (e.g. DeepSeek's /v1/models returns no context-length field, so auto-detection
# yields 0). Rather than silently truncating history — which both drops context
# and re-breaks DeepSeek's token-prefix cache every turn — a conversation is
# blocked with a user-visible notice once it reaches this bound.
DEFAULT_CONTEXT_WINDOW = 256000



