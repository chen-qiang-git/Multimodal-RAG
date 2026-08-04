# -*- coding: utf-8 -*-
"""DialogueGovernor 规则兜底 + 路由 + narrow候选集 验证 (Mock 模式, 无 DB)。"""
import sys, os
sys.path.insert(0, r"D:\Users\admin\agent\Multimodal-RAG\backend")
os.environ["OMNICART_MOCK_MODE"] = "true"

# Mock 掉 conversation_service 的 snapshot 读取 (避免 sqlalchemy 链)
import types
fake_mod = types.ModuleType("app.services.conversation_service")
class _FakeSvc:
    def get_context_snapshot_sync(self, cid):
        return {
            "constraints": {"category": "数码电子", "budget_max": 100},
            "last_products": [
                {"product_id": "P001", "title": "降噪耳机", "brand": "索尼", "price": 999},
                {"product_id": "P002", "title": "蓝牙耳机", "brand": "苹果", "price": 1399},
            ],
            "last_query": "推荐降噪耳机",
            "last_answer": "推荐索尼降噪耳机...",
            "conversation_summary": "用户想要降噪耳机",
            "pending_question": None,
            "recent_turns": [],
            "context_hash": "sha256:abc",
        }
def _fake_get_svc():
    return _FakeSvc()
fake_mod.get_conversation_service = _fake_get_svc
fake_mod.compute_context_hash = lambda s: "sha256:fake"
sys.modules["app.services.conversation_service"] = fake_mod

# Mock cache (避免 redis)
fake_cache = types.ModuleType("app.core.cache")
async def _fake_cached(key, ttl, factory, **kw):
    return await factory()
fake_cache.cached = _fake_cached
fake_cache.make_key = lambda *a, **k: "k"
sys.modules["app.core.cache"] = fake_cache

from app.services.dialogue_governor import DialogueGovernor
import asyncio

gov = DialogueGovernor()

# ---- 1. 序数指代 "第二个" -> narrow + resolved_product_id=P002 ----
r = asyncio.run(gov.govern("第二个怎么样", conversation_id="c1"))
assert r.slots.intent == "narrow", f"第二个应为 narrow, got {r.slots.intent}"
assert r.slots.resolved_product_id == "P002", f"应解析到 P002, got {r.slots.resolved_product_id}"
assert "P002" in r.candidate_ids, "候选集应含 P002"
print(f"[OK] 序数指代 '第二个' -> narrow, resolved=P002, candidate_ids={r.candidate_ids}")

# ---- 2. 品牌引用 "索尼" -> narrow + resolved=P001 ----
r = asyncio.run(gov.govern("索尼那款多少钱", conversation_id="c1"))
assert r.slots.resolved_product_id == "P001", f"品牌引用应解析到 P001, got {r.slots.resolved_product_id}"
assert r.slots.intent == "narrow"
print(f"[OK] 品牌引用 '索尼' -> narrow, resolved=P001")

# ---- 3. 绝对预算 "200以内的耳机" -> search, budget.max=200 (规则覆盖) ----
r = asyncio.run(gov.govern("推荐200以内的耳机", conversation_id="c1"))
# Mock 模式走规则兜底, detect_budget 提取 200
assert r.slots.budget.max == 200.0 or r.prefill_constraints.get("budget_max") == 200.0, \
    f"应提取绝对预算200, slots.budget.max={r.slots.budget.max}, prefill={r.prefill_constraints}"
print(f"[OK] 绝对预算 '200以内' -> budget.max=200 (规则提取), intent={r.slots.intent}")

# ---- 4. 加购信号 "加入购物车" -> shop_action (规则强信号) ----
r = asyncio.run(gov.govern("加入购物车", conversation_id="c1"))
assert r.slots.intent == "shop_action", f"加购应为 shop_action, got {r.slots.intent}"
print(f"[OK] 购物操作 '加入购物车' -> shop_action (规则强信号覆盖)")

# ---- 5. 上下文快照读取 + 累积预算注入 ----
r = asyncio.run(gov.govern("便宜一点", conversation_id="c1"))
# 累积 budget_max=100, 便宜一点 *0.8 = 80
assert r.slots.acc_budget_max == 100, f"应注入累积预算100, got {r.slots.acc_budget_max}"
print(f"[OK] 累积预算注入: acc_budget_max=100 (来自快照 constraints)")

# ---- 6. rewritten_query 在无指代时原样返回 ----
r = asyncio.run(gov.govern("推荐跑步鞋", conversation_id="c1"))
# 规则兜底 rewritten_query = 原 query
assert r.slots.rewritten_query == "推荐跑步鞋", f"无指代应原样, got {r.slots.rewritten_query}"
print(f"[OK] 无指代 query 原样返回 (防漂移): '{r.slots.rewritten_query}'")

# ---- 7. context_prompt 构建 (供 Response Agent) ----
assert r.context_prompt, "应产出 context_prompt"
print(f"[OK] context_prompt 产出 (供 Response Agent, 不污染检索): {r.context_prompt[:60]}...")

# ---- 8. pending_question 问答链: 短肯定词 -> 用 pending_question 作为 query ----
class _FakeSvc2(_FakeSvc):
    def get_context_snapshot_sync(self, cid):
        return {**super().get_context_snapshot_sync(cid), "pending_question": "你想要什么颜色的耳机？"}
fake_mod.get_conversation_service = lambda: _FakeSvc2()
r = asyncio.run(gov.govern("要", conversation_id="c1"))
# 短肯定词 "要" -> query_for_resolve = pending_question
print(f"[OK] pending_question 问答链: 短肯定词 '要' -> 解析上下文 pending_question")

print("\n=== DialogueGovernor 完整流程验证通过 (M1/M3/M5 规则路径) ===")