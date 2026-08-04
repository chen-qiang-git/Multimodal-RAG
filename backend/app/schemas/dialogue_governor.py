# -*- coding: utf-8 -*-
"""DialogueGovernor 槽位 Schema (M1).

依据 docs/dialogue-governor-design.md §5 定义。
一次 LLM 调用输出的结构化槽位 + 确定性治理后的最终槽位。
"""

from typing import Literal, Optional
from pydantic import BaseModel, Field


class BudgetSlots(BaseModel):
    """预算槽位 — LLM 只产出 modifier, 算术由 BudgetGovernor 完成。"""

    min: Optional[float] = None
    max: Optional[float] = None
    raw: Optional[str] = None         # 原文片段, 如 "200以内" / "便宜一点"
    modifier: Optional[str] = None   # cheaper / pricier / same / double / half


class GovernorSlots(BaseModel):
    """DialogueGovernor 统一输出槽位。

    intent 路由分支:
      search / narrow / direct_answer / scene_search / shop_action / chitchat
    """

    intent: Literal[
        "search", "narrow", "direct_answer",
        "scene_search", "shop_action", "chitchat",
    ] = "search"
    confidence: float = 0.0
    rewritten_query: str = ""

    category: Optional[str] = None
    sub_category: Optional[str] = None
    skin_type: Optional[str] = None
    budget: BudgetSlots = Field(default_factory=BudgetSlots)
    scene: Optional[str] = None
    benefit: list[str] = Field(default_factory=list)
    brand: Optional[str] = None
    exclusions: list[str] = Field(default_factory=list)
    spec_keywords: list[str] = Field(default_factory=list)
    must_tags: list[str] = Field(default_factory=list)

    resolved_product_id: Optional[str] = None
    needs_clarification: bool = False

    # 确定性预消解阶段产出的高置信度字段 (供 D3 规则覆盖校验)
    rule_resolved_product_id: Optional[str] = None
    rule_budget_max: Optional[float] = None
    rule_shop_action: bool = False
    # 累积约束 (来自 context_snapshot), 供 BudgetGovernor 取上一轮 budget
    acc_budget_max: Optional[float] = None

    def normalized_constraints(self) -> dict:
        """槽位归一化为下游 Constraints/RetrievalPlan 可消费的 dict。"""

        from app.decision.rules import expand_brand_aliases

        constraints = {}
        if self.category:
            constraints["category"] = self.category
        if self.sub_category:
            constraints["sub_category"] = self.sub_category
        if self.budget.max is not None:
            constraints["budget_max"] = self.budget.max
        if self.budget.min is not None:
            constraints["budget_min"] = self.budget.min
        if self.scene:
            constraints["scenario"] = self.scene
        if self.spec_keywords:
            constraints["spec_keywords"] = list(self.spec_keywords)
        if self.must_tags:
            constraints["must_tags"] = list(self.must_tags)
        exclusions = list(self.exclusions)
        if self.brand and not self.resolved_product_id:
            # 未锁定具体商品时, brand 并入 must_tags 做语义加权
            constraints.setdefault("must_tags", [])
            constraints["must_tags"] = list(set(constraints["must_tags"] + [self.brand]))
        if exclusions:
            constraints["exclude_tags"] = expand_brand_aliases(exclusions)
        return constraints