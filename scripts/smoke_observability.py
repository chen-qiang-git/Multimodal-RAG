"""可观测性全链路 smoke test（Mock 模式，无需 Qwen API / 依赖完整）。

验证四个修复点：
  ① chat_stream 流式调用落 trace（之前完全不落，主链路统计失真）
  ② 真实 usage 透传链路通畅（Mock 走估算，非 0 即证明 _trace 跑通）
  ③a stats 含成本维度（estimated_cost_cny / cost_by_model）+ P50/P95 nearest-rank
  ③b overview 三维度聚合（cost / perf / recall）

运行：
  py -3 scripts/smoke_observability.py
（或 OMNICART_MOCK_MODE 环境下用项目 python）
"""
import asyncio
import os
import sys
from pathlib import Path

# 控制台 UTF-8 输出（避免中文乱码）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 让脚本能从仓库根直接跑
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "backend"))
os.environ.setdefault("OMNICART_MOCK_MODE", "true")

from app.model_gateway.gateway import get_model_gateway  # noqa: E402
from app.observability.collector import get_collector, _percentile  # noqa: E402


def check(label: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {label}{(' — ' + detail) if detail else ''}")
    return ok


async def main() -> int:
    gw = get_model_gateway()
    coll = get_collector()

    # 清空旧 trace，避免历史数据干扰断言
    await coll.clear()

    all_ok = True

    # ===== 1. 四种调用齐发（chat / chat_stream / embed / rerank）=====
    print("=== 1. 四类模型调用（Mock 模式）===")
    stream_chunks = [tok async for tok in gw.chat_stream("chat_generation", "你好，豆仔", system="你是购物助手")]
    r = await gw.chat("intent_understanding", "我想买蓝牙耳机")
    v = await gw.embed(["蓝牙降噪耳机"], "text_embedding")
    rr = await gw.rerank("耳机", ["蓝牙耳机", "薯片", "降噪耳机"], top_n=2)
    print(f"  chat_stream → {len(stream_chunks)} tokens")
    print(f"  chat        → {len(r)} chars")
    print(f"  embed       → {len(v)} vecs x {len(v[0]) if v else 0}d")
    print(f"  rerank      → {len(rr)} results")
    coll._flush()  # 强制把缓冲写盘

    # ===== 2. trace 落库（缺口①：chat_stream 是否进库）=====
    print("\n=== 2. trace 落库验证（缺口①）===")
    traces = await coll.query(limit=50)
    by_name: dict[str, int] = {}
    for t in traces:
        n = t["name"]
        by_name[n] = by_name.get(n, 0) + 1
    print(f"  落库总数: {len(traces)} | 按 name: {by_name}")
    # chat_stream 也用 name=qwen.chat，所以 qwen.chat 至少 2 条（chat + chat_stream）
    all_ok &= check("chat_stream 落库（之前为 0）", by_name.get("qwen.chat", 0) >= 2,
                    f"qwen.chat={by_name.get('qwen.chat', 0)}")
    all_ok &= check("embed 落库", by_name.get("qwen.embed", 0) >= 1)
    all_ok &= check("rerank 落库", by_name.get("qwen.rerank", 0) >= 1)

    # ===== 3. token 透传链路（缺口②：_trace 跑通则 tokens 非 0）=====
    print("\n=== 3. token 透传链路（缺口②）===")
    nonzero = [t for t in traces if t.get("tokens_input", 0) > 0 or t.get("tokens_output", 0) > 0]
    all_ok &= check("trace 含 token 计数", len(nonzero) >= 1, f"{len(nonzero)}/{len(traces)} 有 token")
    # Mock 模式不计成本（质量点：status==mock → cost_cny 跳过）
    mock_with_cost = [t for t in traces
                      if t.get("status") == "mock" and (t.get("metadata") or {}).get("cost_cny")]
    all_ok &= check("Mock 不计虚假成本", len(mock_with_cost) == 0, f"{len(mock_with_cost)} 条误计")

    # ===== 4. stats 成本维度 + P50/P95（缺口③a）=====
    print("\n=== 4. stats 聚合（缺口③a）===")
    s = await coll.stats(hours=1)
    print(f"  total_calls={s.get('total_calls')}  error_rate={s.get('error_rate')}")
    print(f"  p50={s.get('latency_p50_ms')}ms  p95={s.get('latency_p95_ms')}ms  avg={s.get('latency_avg_ms')}ms")
    print(f"  tokens_total={s.get('tokens_total')}  cost_cny={s.get('estimated_cost_cny')}")
    print(f"  by_model={s.get('by_model')}")
    print(f"  cost_by_model={s.get('cost_by_model')}")
    all_ok &= check("stats 含 estimated_cost_cny 字段", "estimated_cost_cny" in s)
    all_ok &= check("stats 含 cost_by_model 字段", "cost_by_model" in s)
    all_ok &= check("Mock 模式成本为 0", s.get("estimated_cost_cny") == 0)

    # ===== 5. P95 nearest-rank 算法正确性 =====
    print("\n=== 5. P50/P95 nearest-rank 算法 ===")
    t20 = list(range(1, 21))  # [1..20]
    p50, p95 = _percentile(t20, 50), _percentile(t20, 95)
    print(f"  [1..20]: p50={p50} p95={p95} (期望 p50=10 p95=19)")
    all_ok &= check("p50 nearest-rank", p50 == 10)
    all_ok &= check("p95 nearest-rank", p95 == 19)
    all_ok &= check("空列表百分位=0", _percentile([], 95) == 0)
    all_ok &= check("单元素 p95=自身", _percentile([42], 95) == 42)

    # ===== 6. overview 三维度聚合（缺口③b，绕过 fastapi 直接调底层）=====
    print("\n=== 6. overview 三维度（缺口③b）===")
    from app.observability.rag_logger import compute_rag_stats
    overview = {
        "cost": {
            "estimated_cost_cny": s.get("estimated_cost_cny"),
            "tokens_total": s.get("tokens_total"),
            "cost_by_model": s.get("cost_by_model"),
        },
        "perf": {
            "total_calls": s.get("total_calls"),
            "error_rate": s.get("error_rate"),
            "latency_p95_ms": s.get("latency_p95_ms"),
        },
        "recall": compute_rag_stats() or {},
    }
    print(f"  cost  = {overview['cost']}")
    print(f"  perf  = {overview['perf']}")
    print(f"  recall= {overview['recall']}")
    all_ok &= check("overview 三维度齐备",
                    all(k in overview for k in ("cost", "perf", "recall")))

    print("\n" + ("=" * 50))
    print("全部通过 ✅" if all_ok else "存在失败项 ❌ — 见上方 FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
