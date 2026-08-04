# -*- coding: utf-8 -*-
"""BudgetGovernor — 确定性条件治理 (M2, 阶段 4).

职责: 把 LLM 的"语言条件"转成检索可用的"数字条件"。
**不允许 LLM 做算术** — 相对预算修饰词在此处做系数乘法。

依据 docs/dialogue-governor-design.md §4 阶段4:
  - BudgetGovernor 映射表 (可配置系数)
  - 规则覆盖校验 (D3): 高置信度字段冲突时规则覆盖 LLM, 记录日志
  - 槽位归一化
"""

import logging
from typing import Optional

from app.schemas.dialogue_governor import GovernorSlots

logger = logging.getLogger(__name__)

# ---- BudgetGovernor 映射表 (可配置) ----
# modifier -> (系数, 说明)
BUDGET_MODIFIERS: dict[str, tuple[float, str]] = {
    "cheaper": (0.8, "便宜一点"),
    "much_cheaper": (0.7, "再便宜点 / 更便宜"),
    "pricier": (1.2, "贵一点"),
    "same": (0.95, "差不多 / 别太贵"),
    "double": (2.0, "预算翻倍"),
    "half": (0.5, "减半"),
}

# 中文修饰词 -> modifier 标签 (LLM 可能直接给中文)
CN_MODIFIER_MAP: dict[str, str] = {
    "便宜一点": "cheaper", "稍微便宜": "cheaper", "便宜些": "cheaper",
    "再便宜点": "much_cheaper", "更便宜": "much_cheaper", "再便宜": "much_cheaper",
    "贵一点": "pricier", "稍贵": "pricier", "贵些": "pricier",
    "差不多": "same", "别太贵": "same", "差不多就行": "same",
    "翻倍": "double", "预算翻倍": "double",
    "减半": "half", "便宜一半": "half",
}


def resolve_modifier(text: str) -> Optional[str]:
    """从中文修饰词文本解析 modifier 标签。"""
    if not text:
        return None
    for cn, mod in CN_MODIFIER_MAP.items():
        if cn in text:
            return mod
    return None


def apply_budget_modifier(slots: GovernorSlots) -> GovernorSlots:
    """对 slots.budget 做确定性算术 (阶段4 BudgetGovernor)。

    输入 = 修饰词 + 上一轮 budget (slots.acc_budget_max)
    输出 = budget.max = round(prev * coef)
    无上一轮预算且只有修饰词 -> needs_clarification 或保持无预算
    """
    modifier = slots.budget.modifier or resolve_modifier(slots.budget.raw or "")
    if not modifier:
        return slots

    coef, _desc = BUDGET_MODIFIERS.get(modifier, (None, ""))
    if coef is None:
        return slots

    prev_max = slots.acc_budget_max
    if prev_max is None or prev_max <= 0:
        # 无上一轮预算, "便宜一点" 是语义缺失
        if modifier in ("cheaper", "much_cheaper", "pricier", "same", "half", "double"):
            slots.needs_clarification = True
        return slots

    new_max = round(prev_max * coef, 2)
    if new_max < 0:
        new_max = 0.0
    slots.budget.max = new_max
    # min 钳制: budget.min <= budget.max
    if slots.budget.min is not None and slots.budget.min > slots.budget.max:
        slots.budget.min = slots.budget.max
    return slots


def apply_rule_override(slots: GovernorSlots) -> GovernorSlots:
    """规则覆盖校验 (D3): 高置信度字段冲突时规则覆盖 LLM。

    高置信度字段:
      - 绝对预算 budget.max (正则提取)
      - resolved_product_id (序数/品牌匹配)
      - 显式购物意图
    冲突 -> 强制用规则值覆盖, 写结构化日志。
    """
    # 绝对预算: 规则 rule_budget_max 覆盖 LLM budget.max
    if slots.rule_budget_max is not None and slots.rule_budget_max > 0:
        llm_max = slots.budget.max
        if llm_max is None or abs(llm_max - slots.rule_budget_max) > 0.01:
            logger.info(
                "rule_override budget.max: rule=%s llm=%s query=%s",
                slots.rule_budget_max, llm_max, slots.rewritten_query[:60],
            )
            slots.budget.max = slots.rule_budget_max

    # resolved_product_id: 规则 rule_resolved_product_id 覆盖 LLM
    if slots.rule_resolved_product_id:
        if slots.resolved_product_id and slots.resolved_product_id != slots.rule_resolved_product_id:
            logger.info(
                "rule_override resolved_product_id: rule=%s llm=%s",
                slots.rule_resolved_product_id, slots.resolved_product_id,
            )
        slots.resolved_product_id = slots.rule_resolved_product_id

    # 显式购物意图: 规则强信号不被 LLM 覆盖
    if slots.rule_shop_action:
        if slots.intent != "shop_action":
            logger.info(
                "rule_override intent: rule=shop_action llm=%s", slots.intent,
            )
        slots.intent = "shop_action"

    return slots


def normalize_slots(slots: GovernorSlots) -> GovernorSlots:
    """阶段4 完整治理: 规则覆盖 -> 相对预算算术。"""
    slots = apply_rule_override(slots)
    slots = apply_budget_modifier(slots)
    return slots