"""流水线 API 契约模型（backend-architecture.md §3.4；前后端共同遵守）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# pipeline_runs.status 全集 + 尚未执行的 'pending'
RunStatus = Literal["pending", "running", "waiting_user", "done", "failed", "skipped", "cancelled"]


class PipelineStartOut(BaseModel):
    """POST /pipeline/start|resume|retry → 202 响应体。"""

    run_group: str


class StepStateOut(BaseModel):
    """state.steps[] 条目。"""

    key: str
    name_zh: str
    status: RunStatus = "pending"
    attempt: int = 0
    error: str | None = None


class PendingInteractionOut(BaseModel):
    """state.pending_interaction（无挂起时为 null）。"""

    model_config = ConfigDict(populate_by_name=True)

    step_key: str
    kind: str
    # 'schema' 与 BaseModel 保留名冲突，用别名序列化
    schema_: Any = Field(default=None, alias="schema")
    prompt: str = ""
    default: Any = None


class PipelineStateOut(BaseModel):
    """GET /pipeline/state 响应体。"""

    run_group: str
    steps: list[StepStateOut]
    pending_interaction: PendingInteractionOut | None = None
    # 当前运行步骤的进度快照（无正在跑的步骤时为 None）。
    # 刷新页面 / 断线重连后，心跳要等下一拍才来；这里先给一份，
    # 否则用户会在最长的那一步上看到几秒钟的「什么都没有」。
    progress: dict[str, Any] | None = None


class PipelineInputBody(BaseModel):
    """POST /pipeline/input 请求体。"""

    step_key: str
    payload: Any = None


class PipelineRetryBody(BaseModel):
    """POST /pipeline/retry 请求体（step_key 可省略 = 重试当前失败步）。"""

    step_key: str | None = None
