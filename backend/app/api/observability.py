"""可观测性 API — 查看 LLM 调用追踪和聚合统计"""

from fastapi import APIRouter, Query

from app.observability.collector import get_collector

router = APIRouter(prefix="/api/observability", tags=["observability"])


@router.get("/traces")
async def list_traces(
    limit: int = Query(50, ge=1, le=500),
    name: str = Query("", description="按调用类型筛选: qwen.chat / qwen.vision / qwen.embed / qwen.rerank"),
    status: str = Query("", description="按状态筛选: success / error / mock / fallback"),
):
    """获取最近 LLM 调用追踪列表"""
    collector = get_collector()
    traces = await collector.query(limit=limit, name=name, status=status)
    return {"total": len(traces), "traces": traces}


@router.get("/traces/{span_id}")
async def get_trace(span_id: str):
    """获取单条 LLM 调用完整追踪"""
    collector = get_collector()
    span = await collector.get_span(span_id)
    if span is None:
        return {"error": "span not found"}
    return span


@router.get("/stats")
async def observability_stats(
    hours: int = Query(24, ge=1, le=720, description="统计窗口（小时），默认 24h"),
):
    """LLM 调用聚合统计：次数、token、延迟分布、错误率、成本"""
    collector = get_collector()
    return await collector.stats(hours=hours)


@router.get("/overview")
async def observability_overview(
    hours: int = Query(24, ge=1, le=720, description="LLM 统计窗口（小时），默认 24h"),
):
    """三维度聚合视图：成本 / 性能 / 召回（RAG 质量）。

    合并 LLM 调用统计（成本 + 性能）与 RAG 检索质量指标（召回），
    支撑持续优化的统一视图。
    """
    collector = get_collector()
    llm = await collector.stats(hours=hours)
    from app.observability.rag_logger import compute_rag_stats
    rag = compute_rag_stats() or {}

    recall_note = None
    if "error" in rag or not rag.get("queries_with_eval"):
        recall_note = "RAG 指标基于 golden set 离线评测；当前无评测数据，需运行 /api/eval/run 或接入在线采纳信号"

    return {
        "window_hours": hours,
        "cost": {
            "estimated_cost_cny": llm.get("estimated_cost_cny", 0),
            "tokens_total": llm.get("tokens_total", 0),
            "tokens_input": llm.get("tokens_input", 0),
            "tokens_output": llm.get("tokens_output", 0),
            "by_model": llm.get("by_model", {}),
            "cost_by_model": llm.get("cost_by_model", {}),
        },
        "perf": {
            "total_calls": llm.get("total_calls", 0),
            "errors": llm.get("errors", 0),
            "error_rate": llm.get("error_rate", 0),
            "latency_avg_ms": llm.get("latency_avg_ms", 0),
            "latency_p50_ms": llm.get("latency_p50_ms", 0),
            "latency_p95_ms": llm.get("latency_p95_ms", 0),
            "by_capability": llm.get("by_capability", {}),
        },
        "recall": {
            "total_queries": rag.get("total_queries", 0),
            "queries_with_eval": rag.get("queries_with_eval", 0),
            "avg_recall@5": rag.get("avg_recall@5", 0),
            "avg_recall@10": rag.get("avg_recall@10", 0),
            "avg_mrr": rag.get("avg_mrr", 0),
            "avg_hit@3": rag.get("avg_hit@3", 0),
            "avg_precision@3": rag.get("avg_precision@3", 0),
            "note": recall_note,
        },
    }


@router.delete("/traces")
async def clear_traces(
    before: str = Query("", description="清除此时间之前的 trace（ISO 格式），不传则全清"),
):
    """清除追踪数据"""
    collector = get_collector()
    deleted = await collector.clear(before=before)
    return {"deleted": deleted, "message": f"Cleared {deleted} trace records"}
