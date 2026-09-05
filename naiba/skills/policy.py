"""Skill 策略规范化（原 skill_runtime 模块级 policy 区）。"""

from __future__ import annotations

from typing import Any


SKILL_POLICY_MODES = {"auto", "pinned", "exclusive"}


def normalize_skill_policy(
    raw_policy: Any = None,
    *,
    legacy_auto: Any = None,
    legacy_ids: Any = None,
    fixed_ids: Any = None,
    catalog: Any = None,
) -> dict[str, Any]:
    """Normalize and validate the frozen Skill policy for one run.

    Legacy ``auto_skills`` / ``skill_ids`` inputs remain accepted at the API
    boundary, but every running Agent receives this single policy structure.
    """

    def _ids(value: Any) -> list[str]:
        if not isinstance(value, (list, tuple, set)):
            return []
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))

    explicit_policy = isinstance(raw_policy, dict)
    selected = _ids(raw_policy.get("skill_ids")) if explicit_policy else _ids(legacy_ids)
    # referenced_ids：本轮消息里通过 /ref 显式引用的技能（可与冻结集重合）。它是
    # “本轮要启用”的技能，其中不在冻结集内的会走尾部追加；冻结集仍由 skill_ids 决定。
    referenced = _ids(raw_policy.get("referenced_ids")) if explicit_policy else []
    if explicit_policy:
        mode = str(raw_policy.get("mode") or "auto").strip().lower()
    elif selected:
        # A legacy selection was always mandatory; auto_skills only controlled
        # whether routing could add more Skills.
        mode = "pinned"
    else:
        mode = "auto"
    if mode not in SKILL_POLICY_MODES:
        raise ValueError("skill_policy.mode 必须是 auto、pinned 或 exclusive")

    available: set[str] | None = None
    if catalog is not None:
        rows = catalog.values() if isinstance(catalog, dict) else catalog
        available = {
            str(item.get("id") or "")
            for item in rows
            if isinstance(item, dict) and item.get("id")
        }
        unknown = [skill_id for skill_id in selected if skill_id not in available]
        if unknown:
            raise ValueError("未知 Skill：" + ", ".join(unknown))
        # 引用里未知/已删除的技能静默丢弃（前端在染色时已解析成具体 id，这里只兜底）。
        referenced = [skill_id for skill_id in referenced if skill_id in available]

    if mode == "exclusive":
        # 允许为空：exclusive 未选中任何 Skill 时表示该轮不加载任何 Skill（无自动匹配）。
        effective_ids = selected
    elif mode == "auto":
        fixed = _ids(fixed_ids)
        if available is not None:
            fixed = [skill_id for skill_id in fixed if skill_id in available]
        effective_ids = fixed
    else:
        fixed = _ids(fixed_ids)
        if available is not None:
            fixed = [skill_id for skill_id in fixed if skill_id in available]
        effective_ids = list(dict.fromkeys([*fixed, *selected]))

    return {"mode": mode, "skill_ids": effective_ids, "referenced_ids": referenced}


