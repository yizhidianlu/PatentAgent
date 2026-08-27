"""账号系统的 API 契约模型（M8）。

对应 docs/design/auth-system.md §5。命名与既有 models/*.py 保持一致：
入参以 In 结尾、出参以 Out 结尾，敏感字段一律不出现在 Out 里。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Role = Literal["admin", "user"]
UserStatus = Literal["active", "disabled"]

# 用户名规则：3-32 位，字母开头，仅允许字母/数字/下划线/连字符/点
USERNAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_.-]{2,31}$"
# 密码最短长度（管理员生成的随机密码更长）
MIN_PASSWORD_LEN = 8


class Quota(BaseModel):
    """按用户的用量上限；0 或缺省表示不限。"""

    model_config = ConfigDict(extra="ignore")

    daily_llm_calls: int = Field(default=0, ge=0, description="每日 LLM 调用次数上限，0=不限")
    monthly_tokens: int = Field(default=0, ge=0, description="每月 token 上限，0=不限")
    storage_mb: int = Field(default=0, ge=0, description="上传+交付物磁盘占用上限(MB)，0=不限")
    max_cases: int = Field(default=0, ge=0, description="案件数上限，0=不限")


class UsageSnapshot(BaseModel):
    """当前周期的用量快照（配合 Quota 在前端画进度条）。"""

    llm_calls_today: int = 0
    tokens_this_month: int = 0
    storage_mb: float = 0.0
    case_count: int = 0


class UserOut(BaseModel):
    """用户信息（绝不含 password_hash）。"""

    id: str
    username: str
    display_name: str
    role: Role
    status: UserStatus
    must_change_password: bool
    last_login_at: str | None = None
    locked_until: str | None = None
    quota: Quota = Field(default_factory=Quota)
    usage: UsageSnapshot | None = None
    created_at: str
    updated_at: str


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class LoginOut(BaseModel):
    """登录成功响应；session 走 httpOnly Cookie，此处只回 CSRF 与用户信息。"""

    user: UserOut
    csrf_token: str


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=MIN_PASSWORD_LEN, max_length=256)

    @field_validator("new_password")
    @classmethod
    def _strength(cls, v: str) -> str:
        """最低强度：不少于 8 位，且不能是纯数字或纯字母。"""
        if v.isdigit() or v.isalpha():
            raise ValueError("密码需同时包含字母与数字（或符号），且不少于 8 位")
        return v


class UserCreateIn(BaseModel):
    """管理员建号；password 省略时由服务端生成随机强密码并一次性返回。"""

    username: str = Field(pattern=USERNAME_PATTERN)
    display_name: str = Field(default="", max_length=64)
    role: Role = "user"
    password: str | None = Field(default=None, min_length=MIN_PASSWORD_LEN, max_length=256)
    quota: Quota = Field(default_factory=Quota)

    @field_validator("username")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()


class UserUpdateIn(BaseModel):
    """管理员改号；字段省略表示不修改（不允许改用户名，避免审计断链）。"""

    display_name: str | None = Field(default=None, max_length=64)
    role: Role | None = None
    status: UserStatus | None = None
    quota: Quota | None = None


class PasswordIssuedOut(BaseModel):
    """建号/重置密码后**仅此一次**返回明文密码，之后无法再取。"""

    user: UserOut
    password: str
    note: str = "该密码仅显示这一次，请立即复制并交给用户；用户首次登录后必须修改。"


class AuditEntryOut(BaseModel):
    id: int
    actor_id: str | None = None
    actor_name: str | None = None
    action: str
    target_type: str | None = None
    target_id: str | None = None
    target_owner: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    ip: str | None = None
    created_at: str


class AdminStatsOut(BaseModel):
    """平台总览。"""

    user_count: int = 0
    active_user_count: int = 0
    case_count: int = 0
    case_count_by_module: dict[str, int] = Field(default_factory=dict)
    llm_calls_today: int = 0
    tokens_this_month: int = 0
    storage_mb: float = 0.0
    artifact_count: int = 0
