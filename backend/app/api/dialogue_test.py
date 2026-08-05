# -*- coding: utf-8 -*-
"""多轮对话测试台 — 单页 HTML, 展示 DialogueGovernor 槽位。

访问: GET /api/dialogue_test
"""
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["dialogue_test"])

_HTML_PATH = Path(__file__).resolve().parent.parent / "static" / "dialogue_test.html"


@router.get("/api/dialogue_test", response_class=HTMLResponse)
async def dialogue_test_page():
    """多轮对话测试页 — 左侧对话, 右侧实时展示 query 改写后的槽位。"""
    try:
        return HTMLResponse(_HTML_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return HTMLResponse(f"<h3>页面加载失败: {e}</h3>", status_code=500)
