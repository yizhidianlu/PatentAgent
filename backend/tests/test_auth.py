"""认证契约测试（M8 / auth-system.md §3 §4.1）。

覆盖：登录成功与失败、失败不泄露账号是否存在、大小写不敏感、暴力破解锁定与解锁、
登出、/auth/me、改密（旧密码错 / 新旧相同 / 改密吊销其它会话）、会话过期、
CSRF 缺失与错误、首登强制改密拦截、未登录一律 401 JSON、安全响应头。

纪律：本文件里的每个场景都用**专属的一次性账号**，绝不去动 conftest 里
`client` / `admin_client` 所用的账号 —— 那两个会话是整场测试共用的。
"""

from __future__ import annotations

import pytest

from app.db import database as db
from app.services import auth as auth_service
from conftest import ADMIN_PASSWORD, ADMIN_USERNAME, AuthedClient, create_user, login

API = "/api/v1"


def _raw_post(raw, url: str, *, session_id: str | None = None, csrf: str | None = None, **kw):
    """裸请求：可精确控制带不带 Cookie / CSRF 头（用于探测中间件行为）。"""
    headers = dict(kw.pop("headers", None) or {})
    if session_id is not None:
        headers["Cookie"] = f"pa_session={session_id}"
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    return raw.request("POST", url, headers=headers, **kw)


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------


def test_login_sets_httponly_session_cookie_and_returns_csrf(raw_client, admin_client) -> None:
    """登录下发 httpOnly 的 pa_session 与可被 JS 读取的 pa_csrf。"""
    issued = create_user(admin_client, "authlogin", password="AuthLogin#1")
    raw_client.cookies.clear()
    resp = raw_client.post(
        f"{API}/auth/login", json={"username": "authlogin", "password": "AuthLogin#1"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["username"] == "authlogin"
    assert body["user"]["role"] == "user"
    assert body["csrf_token"]
    assert "password_hash" not in resp.text          # 敏感字段永不出现在响应里

    raw_cookies = "; ".join(resp.headers.get_list("set-cookie"))
    assert "pa_session=" in raw_cookies and "HttpOnly" in raw_cookies
    # CSRF token 要能被前端 JS 读到，故不能 httpOnly
    csrf_cookie = [c for c in resp.headers.get_list("set-cookie") if c.startswith("pa_csrf=")]
    assert csrf_cookie and "HttpOnly" not in csrf_cookie[0]
    assert issued["password"] == "AuthLogin#1"
    raw_client.cookies.clear()


def test_login_is_case_insensitive_on_username(raw_client, admin_client) -> None:
    create_user(admin_client, "authcase", password="AuthCase#1")
    raw_client.cookies.clear()
    resp = raw_client.post(
        f"{API}/auth/login", json={"username": "AuthCASE", "password": "AuthCase#1"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["username"] == "authcase"
    raw_client.cookies.clear()


def test_wrong_password_and_unknown_user_are_indistinguishable(raw_client, admin_client) -> None:
    """失败文案与状态码必须完全一致，否则可用来枚举账号是否存在。"""
    create_user(admin_client, "authprobe", password="AuthProbe#1")
    raw_client.cookies.clear()
    wrong = raw_client.post(
        f"{API}/auth/login", json={"username": "authprobe", "password": "not-the-password"}
    )
    missing = raw_client.post(
        f"{API}/auth/login", json={"username": "definitely-no-such-user", "password": "whatever"}
    )
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()["detail"] == missing.json()["detail"] == auth_service.LOGIN_FAILED_MESSAGE
    raw_client.cookies.clear()


def test_lockout_after_five_failures_then_admin_reset_unlocks(raw_client, admin_client) -> None:
    """连续失败 5 次锁 5 分钟；锁定期内即使密码正确也拒绝；管理员重置密码即解锁。"""
    issued = create_user(admin_client, "authlock", password="AuthLock#1")
    user_id = issued["user"]["id"]
    raw_client.cookies.clear()

    for i in range(4):
        resp = raw_client.post(
            f"{API}/auth/login", json={"username": "authlock", "password": f"bad{i}"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == auth_service.LOGIN_FAILED_MESSAGE

    fifth = raw_client.post(f"{API}/auth/login", json={"username": "authlock", "password": "bad4"})
    assert fifth.status_code == 401
    assert "锁定" in fifth.json()["detail"]

    # 锁定期内密码正确也不放行
    correct = raw_client.post(
        f"{API}/auth/login", json={"username": "authlock", "password": "AuthLock#1"}
    )
    assert correct.status_code == 401 and "锁定" in correct.json()["detail"]

    row = db.query_one("SELECT failed_logins, locked_until FROM users WHERE id=?", (user_id,))
    assert int(row["failed_logins"]) == 5 and row["locked_until"]

    # 管理员重置密码 → failed_logins 清零、锁定解除
    reset = admin_client.post(f"{API}/admin/users/{user_id}/reset-password")
    assert reset.status_code == 200, reset.text
    new_password = reset.json()["password"]
    after = raw_client.post(
        f"{API}/auth/login", json={"username": "authlock", "password": new_password}
    )
    assert after.status_code == 200, after.text
    raw_client.cookies.clear()


def test_disabled_account_login_says_why(raw_client, admin_client) -> None:
    issued = create_user(admin_client, "authdisabled", password="AuthDis#1")
    user_id = issued["user"]["id"]
    assert admin_client.patch(
        f"{API}/admin/users/{user_id}", json={"status": "disabled"}
    ).status_code == 200
    raw_client.cookies.clear()
    resp = raw_client.post(
        f"{API}/auth/login", json={"username": "authdisabled", "password": "AuthDis#1"}
    )
    assert resp.status_code == 401
    assert "停用" in resp.json()["detail"]
    raw_client.cookies.clear()


# ---------------------------------------------------------------------------
# /auth/me、登出
# ---------------------------------------------------------------------------


def test_me_returns_identity_quota_and_usage(client: AuthedClient) -> None:
    body = client.get(f"{API}/auth/me").json()
    assert body["username"] == client.user["username"]
    assert body["role"] == "user"
    assert body["must_change_password"] is False
    assert body["quota"] == {
        "daily_llm_calls": 0, "monthly_tokens": 0, "storage_mb": 0, "max_cases": 0
    }
    assert body["usage"] is not None and "llm_calls_today" in body["usage"]
    assert "password_hash" not in body


def test_logout_destroys_session_and_next_call_is_401(raw_client, admin_client) -> None:
    from conftest import login_fresh_user

    user = login_fresh_user(raw_client, admin_client, "authlogout", "AuthOut#2026")
    assert user.get(f"{API}/auth/me").status_code == 200

    assert user.post(f"{API}/auth/logout").status_code == 200
    assert db.query_one("SELECT id FROM sessions WHERE id=?", (user.session_id,)) is None
    assert user.get(f"{API}/auth/me").status_code == 401
    # 会话已销毁，再登出一次由中间件统一拦成 401（前端同样跳登录页）
    assert user.post(f"{API}/auth/logout").status_code == 401


# ---------------------------------------------------------------------------
# 改密
# ---------------------------------------------------------------------------


def test_change_password_rejects_wrong_old_and_same_new(raw_client, admin_client) -> None:
    from conftest import login_fresh_user

    user = login_fresh_user(raw_client, admin_client, "authchg", "AuthChg#2026")

    wrong = user.post(
        f"{API}/auth/change-password",
        json={"old_password": "definitely-wrong", "new_password": "Another#2026"},
    )
    assert wrong.status_code == 400 and "当前密码不正确" in wrong.json()["detail"]

    same = user.post(
        f"{API}/auth/change-password",
        json={"old_password": "AuthChg#2026", "new_password": "AuthChg#2026"},
    )
    assert same.status_code == 400 and "不能与当前密码相同" in same.json()["detail"]

    weak = user.post(
        f"{API}/auth/change-password",
        json={"old_password": "AuthChg#2026", "new_password": "12345678"},
    )
    assert weak.status_code == 422        # 纯数字不满足最低强度


def test_change_password_revokes_other_sessions(raw_client, admin_client) -> None:
    """改密后本会话保留、其它设备上的会话立即失效。"""
    from conftest import login_fresh_user

    first = login_fresh_user(raw_client, admin_client, "authmulti", "AuthMulti#2026")
    second = login(raw_client, "authmulti", "AuthMulti#2026")
    assert second.get(f"{API}/auth/me").status_code == 200

    changed = second.post(
        f"{API}/auth/change-password",
        json={"old_password": "AuthMulti#2026", "new_password": "AuthMulti#2027"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["ok"] is True
    assert "退出" in changed.json()["message"]                   # 明确告知其它设备已被登出

    assert second.get(f"{API}/auth/me").status_code == 200      # 当前会话保留
    assert first.get(f"{API}/auth/me").status_code == 401       # 其它会话被吊销
    # 新密码可登录、旧密码不行
    raw_client.cookies.clear()
    assert raw_client.post(
        f"{API}/auth/login", json={"username": "authmulti", "password": "AuthMulti#2026"}
    ).status_code == 401
    assert raw_client.post(
        f"{API}/auth/login", json={"username": "authmulti", "password": "AuthMulti#2027"}
    ).status_code == 200
    raw_client.cookies.clear()


def test_first_login_must_change_password_blocks_everything_else(
    raw_client, admin_client
) -> None:
    """管理员建的号首登必须改密：除 me / change-password / logout 外一律 403。"""
    create_user(admin_client, "authfirst", password="AuthFirst#1")
    fresh = login(raw_client, "authfirst", "AuthFirst#1")
    assert fresh.user["must_change_password"] is True

    blocked = fresh.get(f"{API}/cases")
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "must_change_password"
    assert fresh.post(f"{API}/cases", json={"module": "disclosure"}).status_code == 403

    assert fresh.get(f"{API}/auth/me").status_code == 200        # 白名单：查身份
    ok = fresh.post(
        f"{API}/auth/change-password",
        json={"old_password": "AuthFirst#1", "new_password": "AuthFirst#2026"},
    )
    assert ok.status_code == 200, ok.text
    assert fresh.get(f"{API}/cases").status_code == 200          # 改密后放行
    assert fresh.get(f"{API}/auth/me").json()["must_change_password"] is False


# ---------------------------------------------------------------------------
# 会话与 CSRF
# ---------------------------------------------------------------------------


def test_expired_session_is_401_and_row_is_purged(raw_client, admin_client) -> None:
    from conftest import login_fresh_user

    user = login_fresh_user(raw_client, admin_client, "authexpire", "AuthExp#2026")
    db.execute(
        "UPDATE sessions SET expires_at='2000-01-01 00:00:00' WHERE id=?", (user.session_id,)
    )
    resp = user.get(f"{API}/auth/me")
    assert resp.status_code == 401
    assert "过期" in resp.json()["detail"]
    assert db.query_one("SELECT id FROM sessions WHERE id=?", (user.session_id,)) is None


def test_csrf_token_missing_or_wrong_is_403(raw_client, client: AuthedClient) -> None:
    """非幂等方法必须带正确的 X-CSRF-Token（双提交模式）。"""
    payload = {"module": "disclosure", "title": "CSRF 探测"}

    missing = _raw_post(
        raw_client, f"{API}/cases", session_id=client.session_id, json=payload
    )
    assert missing.status_code == 403 and "CSRF" in missing.json()["detail"]

    wrong = _raw_post(
        raw_client, f"{API}/cases", session_id=client.session_id,
        csrf="not-the-real-token", json=payload,
    )
    assert wrong.status_code == 403 and "CSRF" in wrong.json()["detail"]

    good = _raw_post(
        raw_client, f"{API}/cases", session_id=client.session_id,
        csrf=client.csrf_token, json=payload,
    )
    assert good.status_code == 201, good.text
    # GET 不需要 CSRF
    assert raw_client.request(
        "GET", f"{API}/cases", headers={"Cookie": f"pa_session={client.session_id}"}
    ).status_code == 200
    raw_client.cookies.clear()


def test_login_itself_is_csrf_exempt(raw_client) -> None:
    """登录时还没有会话，自然不能要求 CSRF token。"""
    raw_client.cookies.clear()
    resp = raw_client.post(
        f"{API}/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    raw_client.cookies.clear()


# ---------------------------------------------------------------------------
# 中间件全局守卫
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", f"{API}/cases"),
        ("GET", f"{API}/auth/me"),
        ("GET", f"{API}/system/env"),
        ("GET", f"{API}/oa/library"),
        ("GET", f"{API}/admin/users"),
        ("POST", f"{API}/cases"),
    ],
)
def test_unauthenticated_api_is_401_json(raw_client, method: str, path: str) -> None:
    raw_client.cookies.clear()
    resp = raw_client.request(method, path)
    assert resp.status_code == 401, f"{method} {path} → {resp.status_code}"
    assert resp.headers["content-type"].startswith("application/json")
    assert "<!doctype html" not in resp.text.lower()


def test_public_paths_need_no_session(raw_client) -> None:
    raw_client.cookies.clear()
    assert raw_client.get(f"{API}/system/health").status_code == 200
    # 登录端点公开（凭据错误是 401，但不是「未登录」的 401）
    bad = raw_client.post(f"{API}/auth/login", json={"username": "x", "password": "y"})
    assert bad.json()["detail"] == auth_service.LOGIN_FAILED_MESSAGE


def test_security_headers_are_present(raw_client) -> None:
    resp = raw_client.get(f"{API}/system/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
    # 401 也要带安全头（安全头中间件在认证之外）
    raw_client.cookies.clear()
    denied = raw_client.get(f"{API}/cases")
    assert denied.status_code == 401
    assert denied.headers["X-Content-Type-Options"] == "nosniff"
