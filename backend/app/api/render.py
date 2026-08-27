"""渲染 API（M2）：POST /render/mermaid → PNG bytes。

- 服务端 Playwright（本机 Chrome channel）渲染，与导出用图一致；
- 渲染失败返回 422，detail 携带脚本 stderr 提炼的错误详情。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..models.render import MermaidRenderIn
from ..services import mermaid as mermaid_service

router = APIRouter(prefix="/render", tags=["渲染"])


@router.post(
    "/mermaid",
    summary="mermaid 代码 → PNG（服务端 Playwright，导出与预览同源）",
    response_class=Response,
    responses={
        200: {"content": {"image/png": {}}, "description": "PNG 图片字节流"},
        422: {"description": "渲染失败（语法错误 / 浏览器不可用），detail 含原因"},
    },
)
async def render_mermaid(body: MermaidRenderIn) -> Response:
    try:
        png = await mermaid_service.render_mermaid_png(body.code, body.theme)
    except mermaid_service.MermaidRenderError as exc:
        raise HTTPException(status_code=422, detail=f"mermaid 渲染失败：{exc}") from exc
    return Response(content=png, media_type="image/png")
