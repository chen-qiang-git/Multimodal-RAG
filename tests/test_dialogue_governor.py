# -*- coding: utf-8 -*-
"""DialogueGovernor 核心算法验证 — 纯逻辑, 不依赖 DB/Redis/SQLAlchemy。"""
import sys, hashlib, json
sys.path.insert(0, r"D:\Users\admin\agent\Multimodal-RAG\backend")
import os
os.environ["OMNICART_MOCK_MODE"] = "true"

# ---- 1. context_hash 算法 (D1, 复制实现独立验证) ----
def compute_context_hash(snapshot):
    recent = snapshot.get("recent_turns", []) or []
    summary = snapshot.get("conversation_summary", "") or ""
    constraints = snapshot.get("constraints", {}) or {}
    payload = json.dumps({"recent_turns": recent, "conversation_summary": summary,
                          "constraints": constraints}, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

snap1 = {"recent_turns": [{"user_query": "q1"}], "conversation_summary": "s1", "constraints": {"category": "数码电子"}}
snap2 = {"recent_turns": [{"user_query": "q1"}], "conversation_summary": "s1", "constraints": {"category": "数码电子"}}
snap3 = {"recent_turns": [{"user_query": "q2"}], "conversation_summary": "s1", "constraints": {"category": "数码电子"}}
assert compute_context_hash(snap1) == compute_context_hash(snap2), "相同快照哈希应一致"
assert compute_context_hash(snap1) != compute_context_hash(snap3), "不同快照哈希应不同"
assert compute_context_hash(snap1).startswith("sha256:")
print("[OK] D1 context_hash 算法: 相同一致/不同不同/前缀正确")

# ---- 2. Schema + BudgetGovernor (只依赖 pydantic + rules) ----
from app.schemas.dialogue_governor import GovernorSlots, BudgetSlots
from app.services.budget_governor import apply_budget_modifier, apply_rule_override, BUDGET_MODIFIERS

# 算术确定性
cases = [("cheaper", 100, 80), ("much_cheaper", 100, 70), ("pricier", 100, 120),
         ("same", 100, 95), ("double", 100, 200), ("half", 100, 50)]
for mod, prev, expected in cases:
    s = GovernorSlots(rewritten_query=mod, budget=BudgetSlots(modifier=mod), acc_budget_max=prev)
    s = apply_budget_modifier(s)
    assert s.budget.max == expected, f"{mod}: 100 -> {s.budget.max}, 期望 {expected}"
print(f"[OK] BudgetGovernor 算术确定性 (6 修饰词): 100% 正确")

# 无上一轮预算 -> needs_clarification
s = GovernorSlots(rewritten_query="便宜一点", budget=BudgetSlots(modifier="cheaper"), acc_budget_max=None)
s = apply_budget_modifier(s)
assert s.needs_clarification is True
print("[OK] 无上一轮预算 + 修饰词 -> needs_clarification")

# min 钳制
s = GovernorSlots(rewritten_query="便宜一点", budget=BudgetSlots(min=150, modifier="cheaper"), acc_budget_max=100)
s = apply_budget_modifier(s)
assert s.budget.max == 80.0
assert s.budget.min <= s.budget.max, "min 应被钳制 <= max"
print("[OK] min 钳制: budget.min <= budget.max")

# ---- 3. D3 规则覆盖 ----
s = GovernorSlots(rewritten_query="200以内", budget=BudgetSlots(max=600.0), rule_budget_max=200.0)
s = apply_rule_override(s)
assert s.budget.max == 200.0, "绝对预算规则应覆盖 LLM"
print("[OK] D3 规则覆盖: 绝对预算 200 覆盖 LLM 600")

s = GovernorSlots(rewritten_query="第二个", resolved_product_id="P_LLM", rule_resolved_product_id="P_RULE")
s = apply_rule_override(s)
assert s.resolved_product_id == "P_RULE"
print("[OK] D3 规则覆盖: resolved_product_id 规则覆盖 LLM")

s = GovernorSlots(rewritten_query="加入购物车", intent="search", rule_shop_action=True)
s = apply_rule_override(s)
assert s.intent == "shop_action"
print("[OK] D3 规则覆盖: shop_action 强信号覆盖 LLM")

# ---- 4. 槽位归一化 ----
s = GovernorSlots(
    rewritten_query="推荐200以内不含酒精的精华", category="美妆护肤", sub_category="精华",
    budget=BudgetSlots(max=200.0), brand="兰蔻", exclusions=["酒精"], spec_keywords=["保湿","抗老"],
)
nc = s.normalized_constraints()
assert nc["category"] == "美妆护肤" and nc["budget_max"] == 200.0 and nc["sub_category"] == "精华"
assert nc.get("must_tags") and "兰蔻" in nc["must_tags"], "brand 未锁定 -> 并入 must_tags"
assert "酒精" in nc["exclude_tags"], "exclusions 别名展开"
print("[OK] 槽位归一化: 全字段正确映射到 Constraints")

# ---- 5. intent Literal 取值 ----
import typing
args = set(typing.get_args(GovernorSlots.model_fields["intent"].annotation))
expected = {"search", "narrow", "direct_answer", "scene_search", "shop_action", "chitchat"}
assert args == expected, f"intent Literal 不匹配: {args}"
print("[OK] GovernorSlots.intent Literal 6 个路由分支:", sorted(args))

# ---- 6. BudgetSlots 默认值 ----
b = BudgetSlots()
assert b.min is None and b.max is None and b.modifier is None and b.raw is None
print("[OK] BudgetSlots 默认值全 None")

print("\n=== 全部核心算法验证通过 (M1/M2/D1/D3) ===")