"""案件与消息的 API 契约模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .artifact import ArtifactOut

Module = Literal["disclosure", "paper2patent", "reader", "oa"]
PatentType = Literal["invention", "utility_model", "design"]
CaseStatus = Literal["draft", "running", "waiting_user", "completed", "failed", "archived"]


class Contact(BaseModel):
    """文头联系人（可占位）。"""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    phone: str = ""
    email: str = ""


class CaseCreate(BaseModel):
    """POST /cases 请求体。"""

    module: Module
    title: str | None = Field(default=None, description="案件标题，缺省为「未命名案件」")
    patent_type: PatentType | None = None


class CaseUpdate(BaseModel):
    """PATCH /cases/{id} 请求体：改名 / 归档 / 联系人 / 专利类型。"""

    title: str | None = None
    status: Literal["draft", "archived"] | None = None
    patent_type: PatentType | None = None
    contact: Contact | None = None


class CaseOut(BaseModel):
    """案件列表项。"""

    id: str
    module: Module
    title: str
    patent_type: PatentType | None = None
    status: CaseStatus
    current_step: str | None = None
    contact: Contact | None = None
    created_at: str
    updated_at: str
    # 属主信息（M8）：管理员查看他人案件时，前端据此显示"正在以管理员身份查看"提示条。
    # 普通用户只看得到自己的案件，这两个字段对他们恒等于自身。
    user_id: str | None = None
    owner_username: str | None = None
    owner_display_name: str | None = None


class CaseDetail(CaseOut):
    """GET /cases/{id}：案件 + 流水线工作内存 + 各 kind 最新版交付物。"""

    state: dict[str, Any] = Field(default_factory=dict)
    artifacts_latest: list[ArtifactOut] = Field(default_factory=list)


class MessageOut(BaseModel):
    """会话消息（SSE 重放源）。"""

    id: str
    case_id: str
    seq: int
    role: Literal["user", "assistant", "system", "event"]
    step_key: str | None = None
    content: str
    meta: dict[str, Any] | None = None
    created_at: str
