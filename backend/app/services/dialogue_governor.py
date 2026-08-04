# -*- coding: utf-8 -*-
"""DialogueGovernor — 前置子图, 统一多轮理解 (M1/M3/M5).

依据 docs/dialogue-governor-design.md:
  阶段1 上下文快照 -> 阶段2 确定性预消解 -> 阶段3 槽位编译(LLM一次) ->
  阶段4 条件治理(BudgetGovernor) -> 阶段5 意图路由

收敛 FollowUpEngine(指代检测) + RouterAgent(意图+约束) + RetrievalAgent._llm_extract_keywords
三处分散实现, 把"语言->结构"交给一次 LLM 调用, 确定性节点做算术/锁定/别名展开。

主链路调用: agent_stream 在 run_workflow 之前调用 DialogueGovernor.govern(),
用 governor.rewritten_query 替代"原query+[Follow-up]标注", 用 prefill_state 跳过 Router LLM。
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from app.schemas.dialogue_governor import GovernorSlots, BudgetSlots
from app.services.budget_governor import normalize_slots, resolve_modifier

logger = logging.getLogger(__name__)

# ---- 复用 followup_engine 的正则 (收敛三份重复实现) ----
# 序数指代
_ORDINAL_PATTERN = re.compile(
    r"第\s*([一二三四五六七八九十12345１２３４５])\s*[个款种]"
)
_CN_NUM = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4,
    "六": 5, "七": 6, "八": 7, "九": 8, "十": 9,
    "1": 0, "2": 1, "3": 2, "4": 3, "5": 4,
    "１": 0, "２": 1, "３": 2, "４": 3, "５": 4,
    "第一": 0, "第二": 1, "第三": 2, "第四": 3, "第五": 4,
}
_LAST_REF_PATTERN = re.compile(
    r"(刚才|上次|上一).{0,3}[个款种些]|"
    r"[这那]个[东西]|"
    r"前面.{0,3}[个款种]|"
    r"^.{0,2}(它|这个|那个)"
)
_CART_PATTERN = re.compile(
    r"(加入购物车|加购|加进购物车|买了|下单|结算)"
)

# ---- LLM 编译 prompt (基于 _ROUTER_PROMPT 扩展) ----
_GOVERNOR_PROMPT = """你是一个购物对话治理Agent。分析用户当前消息，结合历史上下文，输出结构化槽位。

## 对话上下文
{context}

## 用户当前消息
{query}

## 预消解结果(规则层, 高置信度)
{pre_resolve}

## 品类
仅限：数码电子 / 美妆护肤 / 服饰运动 / 食品饮料

## 规则
- 仅当存在代词/省略/隐性引用时才改写 query, 否则原样返回(防漂移)
- 槽位只填用户明确或可推断的信息, 未知一律 null, 禁止编造
- 相对价格只输出 modifier(cheaper/pricier/same/double/half), 禁止做乘法
- 追问(便宜点/好一点/有没有别的) → intent=search/narrow, 继承上轮品类
- 序数/品牌/上次引用命中 → intent=narrow
- "刚才那款能上飞机吗"等基于上轮商品的问题 → intent=direct_answer
- 购物操作(加购/下单) → intent=shop_action
- 购物无关闲聊 → intent=chitchat

## 任务
输出严格JSON(无其他内容):
{{
  "intent": "search|narrow|direct_answer|scene_search|shop_action|chitchat",
  "confidence": 0.0~1.0,
  "rewritten_query": "改写后的query(无指代则原样)",
  "category": "品类|null",
  "sub_category": "子品类|null",
  "budget": {{"min": null, "max": null, "raw": "原文片段|null", "modifier": "cheaper|pricier|same|double|half|null"}},
  "scene": "commute|flight|sport|outdoor|desk|travel|null",
  "benefit": ["功效词"],
  "brand": "品牌|null",
  "exclusions": ["排除词"],
  "spec_keywords": ["规格词"],
  "skin_type": "肤质|null",
  "needs_clarification": false
}}
"""


@dataclass
class GovernorResult:
    """DialogueGovernor 一次治理的完整输出。"""

    slots: GovernorSlots
    context_prompt: str = ""          # 供 Response Agent 使用(不污染检索)
    prefill_constraints: dict = field(default_factory=dict)  # 归一化后的 Constraints dict
    retrieval_channels: list[str] = field(default_factory=lambda: ["text", "review"])
    candidate_ids: list[str] = field(default_factory=list)   # narrow 分支候选集
    pending_question: Optional[str] = None                   # 本轮豆仔提取的问句(供下轮)
    needs_clarification_question: Optional[str] = None       # 触发反问时的内容


class DialogueGovernor:
    """前置子图 — 统一"理解历史"。"""

    async def govern(
        self,
        user_query: str,
        conversation_id: str = "",
        session_id: str = "",
    ) -> GovernorResult:
        """执行阶段1-5, 返回 GovernorResult。"""
        # ---- 阶段1: 上下文快照 ----
        snapshot = self._load_snapshot(conversation_id)

        # pending_question 问答链拦截 (D5)
        pending_q = snapshot.get("pending_question") or ""
        query_for_resolve = user_query
        if pending_q:
            # 短肯定词 -> 用 pending_question 作为 query
            _AFFIRMATIVE = {"要", "好", "行", "可以", "对", "是的", "嗯", "买", "要的",
                            "好的", "行的", "对啊", "是", "要买", "想看", "想买", "看看吧"}
            if user_query.strip() in _AFFIRMATIVE:
                query_for_resolve = pending_q

        # ---- 阶段2: 确定性预消解 (规则层) ----
        pre = self._pre_resolve(query_for_resolve, snapshot)

        # ---- 阶段3: 槽位编译 + 指代消解 (一次 LLM) ----
        slots = await self._compile_slots(
            query_for_resolve, user_query, snapshot, pre, conversation_id,
        )

        # 累积预算注入 (供 BudgetGovernor)
        acc_constraints = snapshot.get("constraints", {})
        slots.acc_budget_max = acc_constraints.get("budget_max")

        # ---- 阶段4: 条件治理 (BudgetGovernor) ----
        slots = normalize_slots(slots)

        # ---- 阶段5: 意图路由 + 路由守卫 ----
        slots = self._route_intent(slots, pre, snapshot)
        result = self._build_result(slots, snapshot, user_query, pending_q)
        return result

    # ================================================================
    # 阶段1: 上下文快照
    # ================================================================

    @staticmethod
    def _load_snapshot(conversation_id: str) -> dict:
        if not conversation_id:
            return {}
        try:
            from app.services.conversation_service import get_conversation_service
            return get_conversation_service().get_context_snapshot_sync(conversation_id)
        except Exception as e:
            logger.debug(f"Governor snapshot load failed: {e}")
            return {}

    # ================================================================
    # 阶段2: 确定性预消解 (规则层)
    # ================================================================

    def _pre_resolve(self, query: str, snapshot: dict) -> dict:
        """正则解决可确定的指代, 作为 LLM 提示 + 校验。"""
        pre = {
            "resolved_product_id": None,
            "budget_max": None,
            "shop_action": False,
            "route_hint": None,
        }

        last_products = snapshot.get("last_products", []) or []
        last_product_list = [p for p in last_products if isinstance(p, dict)]

        # 序数指代
        m = _ORDINAL_PATTERN.search(query)
        if m and last_product_list:
            num_token = m.group(1)
            idx = _CN_NUM.get(num_token)
            if idx is not None and idx < len(last_product_list):
                pre["resolved_product_id"] = last_product_list[idx].get("product_id")

        # 上次引用
        if not pre["resolved_product_id"] and _LAST_REF_PATTERN.search(query) and last_product_list:
            pre["resolved_product_id"] = last_product_list[0].get("product_id")

        # 品牌/标题子串匹配
        if not pre["resolved_product_id"] and last_product_list:
            for p in last_product_list:
                brand = (p.get("brand") or "").lower()
                title = (p.get("title") or "").lower()
                if brand and brand in query.lower():
                    pre["resolved_product_id"] = p.get("product_id")
                    break
                if title and len(title) >= 2 and title[:6] in query.lower():
                    pre["resolved_product_id"] = p.get("product_id")
                    break

        # 绝对预算
        try:
            from app.decision.rules import detect_budget
            pre["budget_max"] = detect_budget(query)
        except Exception:
            pass

        # 购物信号
        if _CART_PATTERN.search(query):
            pre["shop_action"] = True
            pre["route_hint"] = "shop_action"

        return pre

    # ================================================================
    # 阶段3: 槽位编译 + 指代消解 (一次 LLM)
    # ================================================================

    async def _compile_slots(
        self, query: str, original_query: str, snapshot: dict,
        pre: dict, conversation_id: str,
    ) -> GovernorSlots:
        # 构建上下文段落
        context = self._build_llm_context(snapshot, query)
        pre_str = json.dumps(pre, ensure_ascii=False)

        # Mock 模式 / LLM 不可用 -> 规则兜底
        try:
            from app.core.config import MOCK_MODE
            mock_mode = bool(MOCK_MODE)
        except Exception:
            mock_mode = False

        llm_result: dict = {}
        if not mock_mode:
            try:
                from app.core.cache import cached, make_key
                from app.core.config import REDIS_CACHE_TTL_REWRITE
                cache_key = make_key(
                    "dialogue_governor",
                    query,
                    snapshot.get("context_hash", ""),
                    conversation_id,
                )

                async def _do_llm():
                    from app.model_gateway.gateway import get_model_gateway
                    gateway = get_model_gateway()
                    prompt = _GOVERNOR_PROMPT.replace("{context}", context) \
                        .replace("{query}", query).replace("{pre_resolve}", pre_str)
                    raw = await gateway.chat("intent_understanding", prompt)
                    return self._parse_llm(raw)

                llm_result = await cached(cache_key, REDIS_CACHE_TTL_REWRITE, _do_llm)
            except Exception as e:
                logger.warning(f"Governor LLM failed, rule fallback: {e}")
                llm_result = {}

        slots = self._slots_from_llm(llm_result, query, original_query, pre)
        return slots

    @staticmethod
    def _parse_llm(raw: str) -> dict:
        if not raw:
            return {}
        raw = raw.strip()
        if "```" in raw:
            block = raw.split("```")[1] if len(raw.split("```")) > 1 else ""
            if block.startswith("json"):
                block = block[4:]
            raw = block.strip()
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, IndexError):
            m = re.search(r"\{[\s\S]*\}", raw)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    return {}
            return {}

    def _slots_from_llm(
        self, llm: dict, query: str, original_query: str, pre: dict,
    ) -> GovernorSlots:
        """LLM 结果 + 预消解规则 -> GovernorSlots。

        LLM 解析失败 -> 用预消解规则槽位 + 原 query (兜底链)。
        """
        if not llm:
            # 兜底: 规则槽位
            return self._rule_fallback_slots(original_query, pre)

        budget_raw = llm.get("budget") or {}
        slots = GovernorSlots(
            intent=llm.get("intent") or "search",
            confidence=float(llm.get("confidence") or 0.0),
            rewritten_query=llm.get("rewritten_query") or original_query,
            category=self._valid_category(llm.get("category")),
            sub_category=llm.get("sub_category"),
            budget=BudgetSlots(
                min=budget_raw.get("min"),
                max=budget_raw.get("max"),
                raw=budget_raw.get("raw"),
                modifier=budget_raw.get("modifier"),
            ),
            scene=llm.get("scene"),
            benefit=llm.get("benefit") or [],
            brand=llm.get("brand"),
            exclusions=llm.get("exclusions") or [],
            spec_keywords=llm.get("spec_keywords") or [],
            must_tags=llm.get("must_tags") or [],
            skin_type=llm.get("skin_type"),
            needs_clarification=bool(llm.get("needs_clarification")),
            # 高置信度规则字段
            rule_resolved_product_id=pre.get("resolved_product_id"),
            rule_budget_max=pre.get("budget_max"),
            rule_shop_action=pre.get("shop_action", False),
        )
        return slots

    @staticmethod
    def _valid_category(val) -> Optional[str]:
        if not val:
            return None
        s = str(val)
        if s.lower() in ("null", "none", ""):
            return None
        valid = {"数码电子", "美妆护肤", "服饰运动", "食品饮料"}
        return s if s in valid else None

    def _rule_fallback_slots(self, query: str, pre: dict) -> GovernorSlots:
        """LLM 不可用时的规则兜底 (复用 router_agent._rule_based_parse 逻辑)。"""
        try:
            from app.agents.router_agent import _rule_based_parse
            rule = _rule_based_parse(query)
        except Exception:
            rule = {}
        # 旧 Router intent (recommend/compare/...) 映射到 Governor 6 分支
        _OLD_INTENT_MAP = {
            "recommend": "search", "compare": "search", "alternative": "search",
            "risk_check": "search", "compatibility_check": "search",
        }
        _raw_intent = pre.get("route_hint") or rule.get("intent") or "search"
        _intent = _OLD_INTENT_MAP.get(_raw_intent, _raw_intent)
        return GovernorSlots(
            intent=_intent,
            confidence=0.5,
            rewritten_query=query,
            category=rule.get("category"),
            sub_category=rule.get("sub_category"),
            budget=BudgetSlots(
                max=pre.get("budget_max") or rule.get("budget_max"),
                min=rule.get("budget_min"),
            ),
            scene=rule.get("scenario"),
            spec_keywords=rule.get("spec_keywords") or [],
            must_tags=rule.get("must_have") or [],
            exclusions=rule.get("avoid") or [],
            rule_resolved_product_id=pre.get("resolved_product_id"),
            rule_budget_max=pre.get("budget_max"),
            rule_shop_action=pre.get("shop_action", False),
        )

    # ================================================================
    # 阶段5: 意图路由
    # ================================================================

    def _route_intent(self, slots: GovernorSlots, pre: dict, snapshot: dict) -> GovernorSlots:
        """路由守卫 + narrow 候选集锁定 (D4)。"""
        # confidence < 0.6 -> 回退 search
        if slots.confidence < 0.6 and slots.intent not in ("shop_action", "chitchat"):
            slots.intent = "search"

        # 有 resolved_product_id -> narrow (先过滤后检索)
        if slots.resolved_product_id:
            slots.intent = "narrow"

        # 购物操作强信号
        if pre.get("shop_action"):
            slots.intent = "shop_action"

        return slots

    # ================================================================
    # 构建 GovernorResult + context_prompt
    # ================================================================

    def _build_result(
        self, slots: GovernorSlots, snapshot: dict,
        original_query: str, pending_q: str,
    ) -> GovernorResult:
        prefill = slots.normalized_constraints()

        # narrow 候选集 (D4): 从 last_products 锁 ≤5 个
        candidate_ids: list[str] = []
        if slots.intent == "narrow" and slots.resolved_product_id:
            last_products = snapshot.get("last_products", []) or []
            for p in last_products:
                if isinstance(p, dict):
                    pid = p.get("product_id")
                    if pid:
                        candidate_ids.append(pid)
            # 锁定 resolved 优先, 保留其余作小范围二次检索
            if slots.resolved_product_id in candidate_ids:
                candidate_ids = [slots.resolved_product_id] + [
                    c for c in candidate_ids if c != slots.resolved_product_id
                ]
            candidate_ids = candidate_ids[:5]

        channels = ["text", "review"]
        if slots.intent == "chitchat":
            channels = []
        elif slots.intent in ("direct_answer",):
            channels = []
        elif prefill.get("exclude_tags"):
            channels = channels + ["policy"]

        # needs_clarification 反问内容
        clar_q = None
        if slots.needs_clarification and not slots.budget.max:
            clar_q = "你想找大概什么价位的产品呢？告诉我预算，我帮你精准推荐～"

        context_prompt = self._build_context_prompt(slots, snapshot, original_query, pending_q)

        return GovernorResult(
            slots=slots,
            context_prompt=context_prompt,
            prefill_constraints=prefill,
            retrieval_channels=channels,
            candidate_ids=candidate_ids,
            pending_question=None,  # 由 agent_stream 提取豆仔问句后写入
            needs_clarification_question=clar_q,
        )

    # ================================================================
    # context_prompt 构建 (供 Response Agent, 不污染检索)
    # ================================================================

    @staticmethod
    def _build_llm_context(snapshot: dict, query: str) -> str:
        """构建注入 LLM 的上下文段落。"""
        parts = []
        summary = snapshot.get("conversation_summary", "")
        if summary:
            parts.append(f"[对话历史] {summary}")
        last_q = snapshot.get("last_query", "")
        last_a = snapshot.get("last_answer", "")
        pending_q = snapshot.get("pending_question", "")
        if pending_q and pending_q != last_q:
            parts.append(f"⚠️ 上一轮豆仔问了: 「{pending_q}」 → 用户当前回复很可能在回答它")
        if last_q and last_q != query:
            parts.append(f"上一轮用户说: 「{last_q[:120]}」")
        if last_a:
            parts.append(f"上一轮豆仔回复: 「{last_a[-200:]}」")
        acc = snapshot.get("constraints", {})
        if acc.get("category"):
            parts.append(f"当前话题品类: {acc['category']}")
        if acc.get("budget_max") is not None:
            parts.append(f"当前预算上限: ¥{acc['budget_max']}")
        products = snapshot.get("last_products", []) or []
        if products:
            ps = "、".join(
                f"#{i+1} {p.get('brand','')} {p.get('title','')[:30]}"
                if isinstance(p, dict) else f"#{i+1} {p}"
                for i, p in enumerate(products[:3])
            )
            parts.append(f"上一轮推荐商品: {ps}")
        return "\n".join(parts) if parts else "(无上下文)"

    @staticmethod
    def _build_context_prompt(
        slots: GovernorSlots, snapshot: dict,
        original_query: str, pending_q: str,
    ) -> str:
        """构建 Response Agent 使用的 context_prompt (不污染检索/精排)。"""
        parts = []
        summary = snapshot.get("conversation_summary", "")
        if summary:
            parts.append(f"[对话历史] {summary}")
        pid = slots.resolved_product_id
        if pid and slots.intent == "narrow":
            parts.append(f"[Follow-up] 用户引用上轮商品 ({pid})，请围绕这个商品回答。")
        if slots.budget.max is not None:
            parts.append(f"[Follow-up] 当前预算 ≤ ¥{slots.budget.max}。")
        if pending_q:
            parts.append(f"[问答链] 上轮豆仔问: {pending_q}")
        last_a = snapshot.get("last_answer", "")
        if last_a:
            parts.append(f"[上次回答] {last_a[:200]}")
        return "\n".join(parts)


# ---- Singleton ----

_governor: DialogueGovernor | None = None


def get_dialogue_governor() -> DialogueGovernor:
    global _governor
    if _governor is None:
        _governor = DialogueGovernor()
    return _governor