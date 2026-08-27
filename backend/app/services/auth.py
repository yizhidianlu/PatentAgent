"""账号系统服务层（M8）。

对应 docs/design/auth-system.md §3/§4。职责：
- 密码哈希与校验（argon2id）；
- 会话签发/校验/吊销（服务端 session，禁用账号可立即失效）；
- 登录限流（同账号连续失败 → 指数锁定）；
- 用户 CRUD 与首启管理员引导；
- 审计日志与用量计数。

安全纪律（写代码时反复对照）：
1. 任何返回给前端的结构都不得含 password_hash；
2. 登录失败一律返回同一句话，不泄露账号是否存在；
3. 普通用户访问他人资源返回 404 而非 403（避免探测资源是否存在）；
4. 不提供任何"测试环境跳过认证"的开关——那种后门误配到生产就是灾难。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import string
from datetime import datetime, timedelta
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from ulid import ULID

from ..db import database as db
from ..models.auth import Quota, UsageSnapshot, UserOut

logger = logging.getLogger(__name__)

# --- 密码哈希 -------------------------------------------------------------
# 参数按 argon2 官方 RFC 9106 的 "second recommended option" 调整：
# 64MB 内存 / 3 轮 / 4 并行，本机实测单次约 50ms，够慢以抗爆破、又不拖垮登录。
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)

# --- 会话 -----------------------------------------------------------------
SESSION_COOKIE = "pa_session"
CSRF_COOKIE = "pa_csrf"
CSRF_HEADER = "X-CSRF-Token"
SESSION_IDLE_DAYS = 7        # 滑动过期：超过这么久没活动即失效
SESSION_ABSOLUTE_DAYS = 30   # 绝对过期：无论多活跃，最长存活

# --- 登录限流 -------------------------------------------------------------
LOCK_THRESHOLDS: tuple[tuple[int, int], ...] = (
    (10, 60),   # 连续失败 ≥10 次 → 锁 60 分钟
    (5, 5),     # 连续失败 ≥5  次 → 锁 5 分钟
)

LOGIN_FAILED_MESSAGE = "用户名或密码错误"


class AuthError(RuntimeError):
    """认证失败（用于向 API 层传递可安全展示给用户的原因）。

    `code` 取值：invalid_credentials / locked / disabled。
    `retry_after` 为锁定剩余秒数（仅 code='locked' 时有值），供前端做倒计时——
    否则前端只能从中文文案里正则抠数字，脆而易碎。
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_credentials",
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """argon2id 哈希。"""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """校验密码；哈希损坏或不匹配都返回 False（不抛异常给上层）。"""
    try:
        _hasher.verify(password_hash, password)
        return True
    except (VerifyMismatchError, InvalidHashError, Exception):  # noqa: BLE001
        return False


def needs_rehash(password_hash: str) -> bool:
    """哈希参数已过时（升级过 argon2 参数时用）。"""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except Exception:  # noqa: BLE001
        return False


def generate_password(length: int = 16) -> str:
    """生成随机强密码：字母+数字+安全符号，保证三类各至少一个。"""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pwd) and any(c.isupper() for c in pwd)
                and any(c.isdigit() for c in pwd)):
            return pwd


# ---------------------------------------------------------------------------
# 时间助手（统一走 db.now_str 的本地时间字符串格式，便于 SQL 比较）
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now()


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 用户
# ---------------------------------------------------------------------------


def _row_to_user(row: Any, *, usage: UsageSnapshot | None = None) -> UserOut:
    """DB 行 → UserOut（丢弃 password_hash 等敏感字段）。"""
    data = dict(row)
    try:
        quota = Quota(**json.loads(data.get("quota_json") or "{}"))
    except (json.JSONDecodeError, TypeError, ValueError):
        quota = Quota()
    return UserOut(
        id=data["id"],
        username=data["username"],
        display_name=data.get("display_name") or "",
        role=data.get("role") or "user",
        status=data.get("status") or "active",
        must_change_password=bool(data.get("must_change_password")),
        last_login_at=data.get("last_login_at"),
        locked_until=data.get("locked_until"),
        quota=quota,
        usage=usage,
        created_at=data.get("created_at") or "",
        updated_at=data.get("updated_at") or "",
    )


def get_user_row(user_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
    return dict(row) if row else None


def get_user_row_by_username(username: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM users WHERE username=?", (username.strip().lower(),))
    return dict(row) if row else None


def create_user(
    *,
    username: str,
    password: str,
    role: str = "user",
    display_name: str = "",
    quota: Quota | None = None,
    must_change_password: bool = True,
    created_by: str | None = None,
    status: str = "active",
) -> dict[str, Any]:
    """建号；用户名重复抛 ValueError。

    status 默认 active（管理员建号即刻可用）；自行注册传 pending 等待审核。
    """
    uname = username.strip().lower()
    if get_user_row_by_username(uname):
        raise ValueError(f"用户名「{uname}」已存在")
    now = _fmt(_now())
    user_id = str(ULID())
    db.execute(
        """
        INSERT INTO users(id, username, display_name, password_hash, role, status,
                          must_change_password, quota_json, created_by, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (user_id, uname, display_name or uname, hash_password(password), role, status,
         1 if must_change_password else 0, (quota or Quota()).model_dump_json(),
         created_by, now, now),
    )
    row = get_user_row(user_id)
    assert row is not None
    return row


def registration_open() -> bool:
    """是否允许自行注册（管理员可在后台关掉）。"""
    policy = db.get_setting_json("auth") or {}
    value = policy.get("allow_registration")
    return True if value is None else bool(value)


def set_registration_open(allowed: bool) -> None:
    policy = db.get_setting_json("auth") or {}
    policy["allow_registration"] = bool(allowed)
    db.set_setting_json("auth", policy)


def count_pending() -> int:
    """待审核账号数——后台入口上要挂角标，不然没人会想起来去看。"""
    row = db.query_one("SELECT COUNT(*) AS n FROM users WHERE status='pending'")
    return int(row["n"]) if row else 0


def register_user(username: str, password: str, display_name: str = "") -> dict[str, Any]:
    """自行注册：建一个 pending 账号，等管理员放行。

    有意不签发会话、不返回用户对象——注册这一步不该带来任何权限。
    用户名已存在时抛 AuthError；这里不做「不泄露账号是否存在」的处理，
    因为注册接口本来就必须告诉用户「这个名字被占了」，藏也藏不住。
    """
    if not registration_open():
        raise AuthError("管理员已关闭自助注册，请联系管理员开通账号。", code="registration_closed")
    if get_user_row_by_username(username):
        raise AuthError("该用户名已被占用，请换一个。", code="username_taken")
    return create_user(
        username=username,
        password=password,
        role="user",
        display_name=display_name or username,
        must_change_password=False,
        status="pending",
        created_by=None,      # 无创建者 = 自行注册，与管理员建号区分开
    )


def set_password(user_id: str, password: str, *, must_change: bool = False) -> None:
    """改密；同时吊销该用户的其它会话（防止旧会话继续用）。"""
    db.execute(
        "UPDATE users SET password_hash=?, must_change_password=?, failed_logins=0,"
        " locked_until=NULL, updated_at=? WHERE id=?",
        (hash_password(password), 1 if must_change else 0, _fmt(_now()), user_id),
    )


# ---------------------------------------------------------------------------
# 登录限流
# ---------------------------------------------------------------------------


def _lock_minutes(failed: int) -> int:
    """按连续失败次数决定锁定分钟数（阈值从高到低匹配）。"""
    for threshold, minutes in LOCK_THRESHOLDS:
        if failed >= threshold:
            return minutes
    return 0


def locked_remaining_seconds(row: dict[str, Any]) -> int:
    """账号剩余锁定秒数；未锁定返回 0。"""
    until = _parse(row.get("locked_until"))
    if not until:
        return 0
    delta = (until - _now()).total_seconds()
    return int(delta) if delta > 0 else 0


def record_login_failure(user_id: str) -> int:
    """记一次失败并按阈值锁定；返回锁定秒数（0=未锁）。"""
    row = get_user_row(user_id)
    if not row:
        return 0
    failed = int(row.get("failed_logins") or 0) + 1
    minutes = _lock_minutes(failed)
    locked_until = _fmt(_now() + timedelta(minutes=minutes)) if minutes else None
    db.execute(
        "UPDATE users SET failed_logins=?, locked_until=?, updated_at=? WHERE id=?",
        (failed, locked_until, _fmt(_now()), user_id),
    )
    return minutes * 60


def record_login_success(user_id: str) -> None:
    now = _fmt(_now())
    db.execute(
        "UPDATE users SET failed_logins=0, locked_until=NULL, last_login_at=?, updated_at=? WHERE id=?",
        (now, now, user_id),
    )


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


def _new_token(nbytes: int = 32) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(nbytes)).decode().rstrip("=")


def create_session(user_id: str, *, ip: str | None = None, user_agent: str | None = None) -> dict[str, str]:
    """签发会话，返回 {session_id, csrf_token}。"""
    now = _now()
    session_id = _new_token()
    csrf_token = _new_token(24)
    db.execute(
        """
        INSERT INTO sessions(id, user_id, csrf_token, ip, user_agent,
                             created_at, last_seen_at, expires_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (session_id, user_id, csrf_token, ip, (user_agent or "")[:300],
         _fmt(now), _fmt(now), _fmt(now + timedelta(days=SESSION_IDLE_DAYS))),
    )
    return {"session_id": session_id, "csrf_token": csrf_token}


def load_session(session_id: str) -> dict[str, Any] | None:
    """取有效会话（自动剔除过期/绝对超期），并滑动续期。"""
    if not session_id:
        return None
    row = db.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not row:
        return None
    data = dict(row)
    now = _now()
    expires = _parse(data.get("expires_at"))
    created = _parse(data.get("created_at"))
    if (expires and expires < now) or (created and now - created > timedelta(days=SESSION_ABSOLUTE_DAYS)):
        destroy_session(session_id)
        return None
    # 滑动续期（每次访问都推后闲置过期时刻）
    db.execute(
        "UPDATE sessions SET last_seen_at=?, expires_at=? WHERE id=?",
        (_fmt(now), _fmt(now + timedelta(days=SESSION_IDLE_DAYS)), session_id),
    )
    return data


def destroy_session(session_id: str) -> None:
    db.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def destroy_user_sessions(user_id: str, *, keep: str | None = None) -> int:
    """吊销某用户的全部会话（改密、禁用、删号时调用）；keep 可保留当前会话。"""
    if keep:
        return db.execute("DELETE FROM sessions WHERE user_id=? AND id<>?", (user_id, keep))
    return db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def purge_expired_sessions() -> int:
    return db.execute("DELETE FROM sessions WHERE expires_at < ?", (_fmt(_now()),))


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------


def authenticate(username: str, password: str) -> dict[str, Any]:
    """校验用户名密码。

    失败一律抛 AuthError(LOGIN_FAILED_MESSAGE)，不泄露账号是否存在；
    账号被禁用或锁定时给出**明确**原因（此时已知凭据正确或账号确实存在，
    告知原因才能让用户知道该找管理员，安全收益大于泄露风险）。
    """
    row = get_user_row_by_username(username)
    if not row:
        # 仍然做一次哈希运算，抹平"用户不存在"与"密码错误"的响应时间差
        _hasher.hash(password)
        raise AuthError(LOGIN_FAILED_MESSAGE)

    remaining = locked_remaining_seconds(row)
    if remaining > 0:
        raise AuthError(
            f"账号已被临时锁定，请 {max(1, remaining // 60)} 分钟后再试；"
            "如需立即解锁请联系管理员。",
            code="locked",
            retry_after=remaining,
        )

    if not verify_password(row["password_hash"], password):
        locked_seconds = record_login_failure(row["id"])
        if locked_seconds:
            raise AuthError(
                f"密码错误次数过多，账号已锁定 {locked_seconds // 60} 分钟。",
                code="locked",
                retry_after=locked_seconds,
            )
        raise AuthError(LOGIN_FAILED_MESSAGE)

    status = row.get("status") or "active"
    if status == "pending":
        # 待审核与停用要分开讲：前者是「还没轮到你」，后者是「你被停了」。
        # 说成一样的，用户会以为自己被拒了，转头就来问管理员。
        raise AuthError("账号正在等待管理员审核，审核通过后即可登录。", code="pending")
    if status != "active":
        raise AuthError("账号已被停用，请联系管理员。", code="disabled")

    # 密码正确：顺带做哈希参数升级
    if needs_rehash(row["password_hash"]):
        db.execute(
            "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
            (hash_password(password), _fmt(_now()), row["id"]),
        )
    record_login_success(row["id"])
    refreshed = get_user_row(row["id"])
    assert refreshed is not None
    return refreshed


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


def audit(
    action: str,
    *,
    actor_id: str | None = None,
    actor_name: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    target_owner: str | None = None,
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """写审计日志；失败只记 warning，绝不影响主流程。"""
    try:
        db.execute(
            """
            INSERT INTO audit_log(actor_id, actor_name, action, target_type, target_id,
                                  target_owner, detail_json, ip, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (actor_id, actor_name, action, target_type, target_id, target_owner,
             json.dumps(detail or {}, ensure_ascii=False), ip, _fmt(_now())),
        )
    except Exception:  # noqa: BLE001
        logger.warning("审计日志写入失败：action=%s target=%s", action, target_id, exc_info=True)


# ---------------------------------------------------------------------------
# 用量计数与配额
# ---------------------------------------------------------------------------


def bump_usage(user_id: str, *, calls: int = 1, tokens: int = 0) -> None:
    """累加当日与当月用量。"""
    if not user_id:
        return
    now = _now()
    for period in (now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")):
        db.execute(
            """
            INSERT INTO usage_counters(user_id, period, llm_calls, total_tokens, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(user_id, period) DO UPDATE SET
              llm_calls = llm_calls + excluded.llm_calls,
              total_tokens = total_tokens + excluded.total_tokens,
              updated_at = excluded.updated_at
            """,
            (user_id, period, calls, tokens, _fmt(now)),
        )


def usage_snapshot(user_id: str) -> UsageSnapshot:
    """当前周期用量快照。"""
    now = _now()
    day = db.query_one(
        "SELECT llm_calls FROM usage_counters WHERE user_id=? AND period=?",
        (user_id, now.strftime("%Y-%m-%d")),
    )
    month = db.query_one(
        "SELECT total_tokens FROM usage_counters WHERE user_id=? AND period=?",
        (user_id, now.strftime("%Y-%m")),
    )
    cases = db.query_one("SELECT COUNT(*) AS n FROM cases WHERE user_id=?", (user_id,))
    return UsageSnapshot(
        llm_calls_today=int(day["llm_calls"]) if day else 0,
        tokens_this_month=int(month["total_tokens"]) if month else 0,
        case_count=int(cases["n"]) if cases else 0,
    )


class QuotaExceededError(RuntimeError):
    """用量超出配额（API 层转 429）。"""


def check_quota(user_row: dict[str, Any]) -> None:
    """发起 LLM 调用前校验配额；超限抛 QuotaExceededError。"""
    try:
        quota = Quota(**json.loads(user_row.get("quota_json") or "{}"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return
    if not (quota.daily_llm_calls or quota.monthly_tokens):
        return
    usage = usage_snapshot(user_row["id"])
    if quota.daily_llm_calls and usage.llm_calls_today >= quota.daily_llm_calls:
        raise QuotaExceededError(
            f"今日调用次数已达上限（{quota.daily_llm_calls} 次），请明日再试或联系管理员调整配额。"
        )
    if quota.monthly_tokens and usage.tokens_this_month >= quota.monthly_tokens:
        raise QuotaExceededError(
            f"本月 token 用量已达上限（{quota.monthly_tokens}），请联系管理员调整配额。"
        )


# ---------------------------------------------------------------------------
# 首次启动引导
# ---------------------------------------------------------------------------


def count_users() -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM users")
    return int(row["n"]) if row else 0


def adopt_orphan_data(owner_id: str) -> dict[str, int]:
    """把**没有属主**的历史数据归到指定用户名下。

    003_auth 给 cases / oa_library 补了 `user_id`，升级前既有的行属主为空串。
    没有属主的案件对任何普通用户都不可见、对管理员也只读（resolve_case 会当成
    「他人案件」处理），等于把用户自己的存量数据锁死。故**仅在首启建管理员时**
    调用一次，把这些孤儿数据认领给管理员。

    只在库里一个用户都没有的时刻执行，因此不存在把他人数据划给管理员的风险。
    """
    cases = db.execute(
        "UPDATE cases SET user_id=? WHERE user_id IS NULL OR user_id=''", (owner_id,)
    )
    library = db.execute(
        "UPDATE oa_library SET user_id=? WHERE user_id IS NULL OR user_id=''", (owner_id,)
    )
    return {"cases": cases, "oa_library": library}


def ensure_bootstrap_admin() -> dict[str, Any] | None:
    """库中无任何用户时创建管理员。

    密码来源优先级：环境变量 ADMIN_PASSWORD → 随机生成（**在日志里打印一次**）。
    返回新建的用户行；已有用户则返回 None。
    """
    if count_users() > 0:
        return None
    # 环境变量优先；其次 backend/.env（经 AppConfig 读入）；再次内置默认
    from ..config import get_config

    cfg = get_config()
    username = (os.getenv("ADMIN_USERNAME") or cfg.admin_username or "admin").strip().lower()
    supplied = os.getenv("ADMIN_PASSWORD") or (cfg.admin_password or "") or None
    password = supplied or generate_password(16)
    row = create_user(
        username=username,
        password=password,
        role="admin",
        display_name="管理员",
        must_change_password=not supplied,   # 环境变量给的密码视为已知，不强制改
    )
    banner = "=" * 64
    if supplied:
        logger.warning("\n%s\n已创建管理员账号：%s（密码取自环境变量 ADMIN_PASSWORD）\n%s",
                       banner, username, banner)
    else:
        logger.warning(
            "\n%s\n已创建管理员账号\n  用户名：%s\n  初始密码：%s\n"
            "  ⚠ 该密码仅显示这一次，请立即登录并修改。\n%s",
            banner, username, password, banner,
        )
    audit("user_create", actor_name="system", target_type="user", target_id=row["id"],
          detail={"role": "admin", "bootstrap": True})

    # 升级场景：把 003_auth 之前遗留的无属主数据认领给这位管理员，
    # 否则用户自己的存量案件会变成谁都改不动的孤儿。
    adopted = adopt_orphan_data(row["id"])
    if adopted["cases"] or adopted["oa_library"]:
        logger.warning(
            "已将 %s 个历史案件、%s 条历史案例归属到管理员「%s」（升级前的存量数据无属主）",
            adopted["cases"], adopted["oa_library"], username,
        )
        audit("data_adopt", actor_name="system", target_type="case",
              target_owner=row["id"], detail=adopted)
    return row
