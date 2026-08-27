"""渲染类 API 契约模型（M2）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MermaidRenderIn(BaseModel):
    """POST /render/mermaid 请求体。"""

    code: str = Field(description="mermaid 源码（不含 ```mermaid 围栏）")
    theme: str | None = Field(default=None, description="mermaid 主题（default/dark/forest/neutral 等），缺省用脚本默认")
