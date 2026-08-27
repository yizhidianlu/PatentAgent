"""管理后台契约测试（M8 / auth-system.md §5.1 的 /admin/*）。

覆盖：用户 CRUD、一次性密码、重置密码、禁用后会话立即失效、自删/自降护栏、
最后一个启用管理员护栏、审计日志、平台统计、普通用户越权访问 /admin/*。

纪律：所有写操作只针对本文件新建的一次性账号；
`admin` 本人与 conftest 的 `tester` / `tester2` 一律不动。
"""

from __future__ import annotations

from typing import Any

from app.db import database as db
from conftest import AuthedClient, create_user, login, login_fresh_user

API = "/api/v1"


def _audit(admin: AuthedClient, action: str, limit: int = 200) -> list[dict[str, Any]]:
    resp = admin.get(f"{API}/admin/audit", params={"action": action, "limit": limit})
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


# ---------------------------------------------------------------------------
# 用户 CRUD
# ---------------------------------------------------------------------------


def test_create_user_returns_one_time_password_and_forces_change(
    admin_client: AuthedClient,
) -> None:
    resp = admin_client.post(
        f"{API}/admin/users",
        json={"username": "AdmNew", "display_name": "新同事", "role": "user"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["user"]["username"] == "admnew"            # 用户名统一存小写
    assert body["user"]["must_change_password"] is True
    assert len(body["password"]) >= 12                     # 未给密码 → 随机强密码
    assert "仅显示这一次" in body["note"]
    assert "password_hash" not in resp.text

    # 详情接口取不到密码（明文只在建号那一次返回）
    detail = admin_client.get(f"{API}/admin/users/{body['user']['id']}")
    assert detail.status_code == 200
    assert body["password"] not in detail.text


def test_create_duplicate_username_is_409(admin_client: AuthedClient) -> None:
    create_user(admin_client, "admdup", password="AdmDup#1")
    again = admin_client.post(f"{API}/admin/users", json={"username": "admdup"})
    assert again.status_code == 409
    assert "已存在" in again.json()["detail"]


def test_create_user_validates_username(admin_client: AuthedClient) -> None:
    for bad in ("ab", "1startswithdigit", "has space", "x" * 40):
        resp = admin_client.post(f"{API}/admin/users", json={"username": bad})
        assert resp.status_code == 422, f"{bad} → {resp.status_code}"


def test_list_and_filter_users(admin_client: AuthedClient) -> None:
    create_user(admin_client, "admlista", password="AdmList#1")
    page = admin_client.get(f"{API}/admin/users", params={"q": "admlista"}).json()
    assert page["total"] == 1 and page["items"][0]["username"] == "admlista"

    admins = admin_client.get(f"{API}/admin/users", params={"role": "admin"}).json()
    assert admins["total"] >= 1
    assert all(u["role"] == "admin" for u in admins["items"])

    active = admin_client.get(f"{API}/admin/users", params={"status": "active"}).json()
    assert all(u["status"] == "active" for u in active["items"])

    assert admin_client.get(f"{API}/admin/users", params={"role": "nope"}).status_code == 422


def test_update_user_display_name_and_quota(admin_client: AuthedClient) -> None:
    issued = create_user(admin_client, "admquota", password="AdmQuota#1")
    user_id = issued["user"]["id"]
    resp = admin_client.patch(
        f"{API}/admin/users/{user_id}",
        json={"display_name": "配额同事", "quota": {"daily_llm_calls": 5, "monthly_tokens": 100}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["display_name"] == "配额同事"
    assert body["quota"]["daily_llm_calls"] == 5
    assert body["quota"]["monthly_tokens"] == 100

    # 空补丁 → 422（避免静默无操作）
    assert admin_client.patch(f"{API}/admin/users/{user_id}", json={}).status_code == 422
    assert admin_client.patch(f"{API}/admin/users/no-such-id", json={
        "display_name": "x"}).status_code == 404


def test_delete_user_removes_cases(raw_client, admin_client: AuthedClient) -> None:
    victim = login_fresh_user(raw_client, admin_client, "admdel", "AdmDel#2026")
    case_id = victim.post(
        f"{API}/cases", json={"module": "disclosure", "title": "待随号删除的案件"}
    ).json()["id"]

    resp = admin_client.delete(f"{API}/admin/users/{victim.user['id']}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert "admdel" in resp.json()["message"]      # 交代清楚删了谁、连带删了几个案件
    assert db.query_one("SELECT id FROM users WHERE id=?", (victim.user["id"],)) is None
    assert db.query_one("SELECT id FROM cases WHERE id=?", (case_id,)) is None
    # 会话随之失效
    assert victim.get(f"{API}/auth/me").status_code == 401
    assert admin_client.delete(f"{API}/admin/users/{victim.user['id']}").status_code == 404


# ---------------------------------------------------------------------------
# 重置密码 / 禁用
# ---------------------------------------------------------------------------


def test_reset_password_issues_new_one_and_revokes_sessions(
    raw_client, admin_client: AuthedClient
) -> None:
    user = login_fresh_user(raw_client, admin_client, "admreset", "AdmReset#2026")
    assert user.get(f"{API}/auth/me").status_code == 200

    resp = admin_client.post(f"{API}/admin/users/{user.user['id']}/reset-password")
    assert resp.status_code == 200, resp.text
    new_password = resp.json()["password"]
    assert resp.json()["user"]["must_change_password"] is True

    # 旧会话立即失效
    assert user.get(f"{API}/auth/me").status_code == 401
    # 新密码可登录，但要先改密
    fresh = login(raw_client, "admreset", new_password)
    assert fresh.user["must_change_password"] is True
    assert fresh.get(f"{API}/cases").status_code == 403

    assert admin_client.post(f"{API}/admin/users/nope/reset-password").status_code == 404


def test_disable_user_invalidates_session_immediately(
    raw_client, admin_client: AuthedClient
) -> None:
    """禁用是「立即」生效的 —— 这正是选服务端 session 而不是 JWT 的理由。"""
    user = login_fresh_user(raw_client, admin_client, "admdisable", "AdmDis#2026")
    assert user.get(f"{API}/cases").status_code == 200

    assert admin_client.patch(
        f"{API}/admin/users/{user.user['id']}", json={"status": "disabled"}
    ).status_code == 200

    resp = user.get(f"{API}/cases")
    # 停用时会话已被直接删除，故这里落在「登录已过期」分支；两者都是 401，前端一律跳登录页
    assert resp.status_code == 401, resp.text
    assert db.query_one("SELECT id FROM sessions WHERE user_id=?", (user.user["id"],)) is None
    # 若会话侥幸残留（例如并发签发），账号状态守卫仍会拦下并给出明确原因
    from app.services import auth as auth_service

    revived = auth_service.create_session(user.user["id"])
    denied = raw_client.request(
        "GET", f"{API}/cases", headers={"Cookie": f"pa_session={revived['session_id']}"}
    )
    assert denied.status_code == 401 and "停用" in denied.json()["detail"]

    # 重新启用后可再次登录
    assert admin_client.patch(
        f"{API}/admin/users/{user.user['id']}", json={"status": "active"}
    ).status_code == 200
    again = login(raw_client, "admdisable", "AdmDis#2026")
    assert again.get(f"{API}/cases").status_code == 200


# ---------------------------------------------------------------------------
# 管理员护栏
# ---------------------------------------------------------------------------


def test_admin_cannot_delete_or_demote_self(admin_client: AuthedClient) -> None:
    me = admin_client.get(f"{API}/auth/me").json()
    assert me["role"] == "admin"

    demote = admin_client.patch(f"{API}/admin/users/{me['id']}", json={"role": "user"})
    assert demote.status_code == 400 and "自己" in demote.json()["detail"]

    disable = admin_client.patch(f"{API}/admin/users/{me['id']}", json={"status": "disabled"})
    assert disable.status_code == 400 and "自己" in disable.json()["detail"]

    removed = admin_client.delete(f"{API}/admin/users/{me['id']}")
    assert removed.status_code == 400 and "自己" in removed.json()["detail"]

    # 护栏生效后自己仍然是启用状态的管理员
    still = admin_client.get(f"{API}/auth/me").json()
    assert still["role"] == "admin" and still["status"] == "active"


def test_last_active_admin_is_protected(admin_client: AuthedClient) -> None:
    """系统至少保留一个**启用状态**的管理员。

    造法：建第二个管理员 → 先停用他（此刻还有两个管理员行，护栏放行）
    → 再删/降他，此时启用管理员只剩 admin 一人，护栏必须拦住。
    """
    issued = create_user(admin_client, "admsecond", password="AdmSecond#1", role="admin")
    second = issued["user"]["id"]
    assert issued["user"]["role"] == "admin"

    assert admin_client.patch(
        f"{API}/admin/users/{second}", json={"status": "disabled"}
    ).status_code == 200

    demote = admin_client.patch(f"{API}/admin/users/{second}", json={"role": "user"})
    assert demote.status_code == 400 and "至少" in demote.json()["detail"]

    removed = admin_client.delete(f"{API}/admin/users/{second}")
    assert removed.status_code == 400 and "至少" in removed.json()["detail"]

    # 恢复启用后（两个启用管理员）就可以正常降级与删除
    assert admin_client.patch(
        f"{API}/admin/users/{second}", json={"status": "active"}
    ).status_code == 200
    assert admin_client.patch(
        f"{API}/admin/users/{second}", json={"role": "user"}
    ).status_code == 200
    assert admin_client.delete(f"{API}/admin/users/{second}").status_code == 200


# ---------------------------------------------------------------------------
# 审计与统计
# ---------------------------------------------------------------------------


def test_audit_log_records_admin_actions(admin_client: AuthedClient) -> None:
    issued = create_user(admin_client, "admaudit", password="AdmAudit#1")
    user_id = issued["user"]["id"]
    admin_client.post(f"{API}/admin/users/{user_id}/reset-password")
    admin_client.patch(f"{API}/admin/users/{user_id}", json={"display_name": "审计对象"})

    me = admin_client.get(f"{API}/auth/me").json()

    created = [e for e in _audit(admin_client, "user_create") if e["target_id"] == user_id]
    assert created, "建号未写审计"
    assert created[0]["actor_id"] == me["id"]
    assert created[0]["actor_name"] == me["username"]
    assert created[0]["target_type"] == "user"
    assert created[0]["detail"]["username"] == "admaudit"
    assert created[0]["created_at"]

    assert [e for e in _audit(admin_client, "password_reset") if e["target_id"] == user_id]
    assert [e for e in _audit(admin_client, "user_update") if e["target_id"] == user_id]
    # 登录也留痕
    assert _audit(admin_client, "login")

    by_actor = admin_client.get(
        f"{API}/admin/audit", params={"actor_id": me["id"], "limit": 5}
    ).json()
    assert by_actor["limit"] == 5 and all(e["actor_id"] == me["id"] for e in by_actor["items"])


def test_failed_login_is_audited_without_leaking_password(
    raw_client, admin_client: AuthedClient
) -> None:
    create_user(admin_client, "admfail", password="AdmFail#1")
    raw_client.cookies.clear()
    raw_client.post(
        f"{API}/auth/login", json={"username": "admfail", "password": "s3cret-attempt"}
    )
    raw_client.cookies.clear()
    entries = [e for e in _audit(admin_client, "login_failed") if e["actor_name"] == "admfail"]
    assert entries, "登录失败未写审计"
    assert "s3cret-attempt" not in str(entries)          # 绝不记录尝试过的密码


def test_platform_stats(admin_client: AuthedClient, client: AuthedClient) -> None:
    client.post(f"{API}/cases", json={"module": "reader", "title": "统计用案件"})
    stats = admin_client.get(f"{API}/admin/stats").json()
    assert stats["user_count"] >= 2
    assert stats["active_user_count"] >= 1
    assert stats["case_count"] >= 1
    assert "reader" in stats["case_count_by_module"]
    assert stats["storage_mb"] >= 0
    for key in ("llm_calls_today", "tokens_this_month", "artifact_count"):
        assert key in stats


def test_admin_can_list_another_users_cases(
    admin_client: AuthedClient, client: AuthedClient
) -> None:
    case_id = client.post(
        f"{API}/cases", json={"module": "disclosure", "title": "管理员可见的他人案件"}
    ).json()["id"]
    page = admin_client.get(f"{API}/admin/users/{client.user['id']}/cases").json()
    assert page["total"] >= 1
    assert case_id in {c["id"] for c in page["items"]}
    assert admin_client.get(f"{API}/admin/users/no-such-user/cases").status_code == 404


# ---------------------------------------------------------------------------
# 配额与用量
# ---------------------------------------------------------------------------


def test_llm_usage_is_charged_to_the_case_owner(client: AuthedClient) -> None:
    """流水线在后台任务里跑，拿不到 request —— 用量按案件属主归账。"""
    from app.services import llm as llm_service

    case_id = client.post(
        f"{API}/cases", json={"module": "reader", "title": "用量归账测试"}
    ).json()["id"]
    before = client.get(f"{API}/auth/me").json()["usage"]

    llm_service._record_call(
        case_id=case_id, step_key="test.usage", model="fake-model",
        prompt_tokens=100, completion_tokens=50, duration_ms=1, status="ok",
    )

    after = client.get(f"{API}/auth/me").json()["usage"]
    assert after["llm_calls_today"] == before["llm_calls_today"] + 1
    assert after["tokens_this_month"] == before["tokens_this_month"] + 150
    # llm_calls 明细表也照记
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM llm_calls WHERE case_id=? AND step_key='test.usage'",
        (case_id,),
    )
    assert int(row["n"]) == 1


def test_orphan_data_is_adopted_by_the_bootstrap_admin(
    admin_client: AuthedClient, other_client: AuthedClient
) -> None:
    """升级场景：003_auth 之前遗留的无属主案件要认领给首启管理员。

    否则用户自己的存量案件会变成「谁都看不见、管理员也只读」的孤儿。
    """
    from ulid import ULID

    from app.services import auth as auth_service

    case_id = str(ULID())
    now = db.now_str()
    db.execute(
        "INSERT INTO cases(id, module, title, status, state_json, user_id, created_at, updated_at)"
        " VALUES (?,'disclosure','升级前的存量案件','draft','{}','',?,?)",
        (case_id, now, now),
    )
    # 孤儿案件对普通用户不可见
    assert other_client.get(f"{API}/cases/{case_id}").status_code == 404

    admin_id = admin_client.get(f"{API}/auth/me").json()["id"]
    adopted = auth_service.adopt_orphan_data(admin_id)
    assert adopted["cases"] >= 1

    row = db.query_one("SELECT user_id FROM cases WHERE id=?", (case_id,))
    assert row["user_id"] == admin_id
    # 认领之后管理员是真属主：可读也可写
    assert admin_client.get(f"{API}/cases/{case_id}").status_code == 200
    assert admin_client.patch(
        f"{API}/cases/{case_id}", json={"title": "认领后可编辑"}
    ).status_code == 200
    assert other_client.get(f"{API}/cases/{case_id}").status_code == 404

    admin_client.delete(f"{API}/cases/{case_id}")


def test_quota_exceeded_blocks_pipeline_start_with_429(
    raw_client, admin_client: AuthedClient
) -> None:
    from app.services import auth as auth_service

    user = login_fresh_user(raw_client, admin_client, "admquotahit", "AdmQH#2026")
    assert admin_client.patch(
        f"{API}/admin/users/{user.user['id']}", json={"quota": {"daily_llm_calls": 1}}
    ).status_code == 200

    case_id = user.post(
        f"{API}/cases", json={"module": "reader", "title": "配额拦截测试"}
    ).json()["id"]

    # 配额未用完时不拦（这里只验证 429 不会误伤，不真的跑流水线）
    auth_service.bump_usage(user.user["id"], calls=1, tokens=0)

    resp = user.post(f"{API}/cases/{case_id}/pipeline/start", json={})
    assert resp.status_code == 429, resp.text
    assert "上限" in resp.json()["detail"]
    # resume / retry / 交底书迭代同样受拦
    assert user.post(f"{API}/cases/{case_id}/pipeline/resume").status_code == 429

    # 管理员放宽配额后恢复正常（此处只断言不再是 429）
    assert admin_client.patch(
        f"{API}/admin/users/{user.user['id']}", json={"quota": {"daily_llm_calls": 0}}
    ).status_code == 200
    assert user.get(f"{API}/auth/me").json()["quota"]["daily_llm_calls"] == 0


# ---------------------------------------------------------------------------
# 越权
# ---------------------------------------------------------------------------


def test_normal_user_cannot_reach_admin_endpoints(client: AuthedClient) -> None:
    me = client.get(f"{API}/auth/me").json()
    for method, path, kwargs in [
        ("GET", f"{API}/admin/users", {}),
        ("POST", f"{API}/admin/users", {"json": {"username": "sneaky"}}),
        ("GET", f"{API}/admin/users/{me['id']}", {}),
        ("PATCH", f"{API}/admin/users/{me['id']}", {"json": {"role": "admin"}}),
        ("POST", f"{API}/admin/users/{me['id']}/reset-password", {}),
        ("DELETE", f"{API}/admin/users/{me['id']}", {}),
        ("GET", f"{API}/admin/users/{me['id']}/cases", {}),
        ("GET", f"{API}/admin/audit", {}),
        ("GET", f"{API}/admin/stats", {}),
    ]:
        resp = client.request(method, path, **kwargs)
        assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"
        assert "管理员" in resp.json()["detail"]
    # 越权尝试没有把自己变成管理员
    assert client.get(f"{API}/auth/me").json()["role"] == "user"


def test_delete_user_keeps_disk_files_by_default(admin_client, raw_client) -> None:
    """删账号默认不删盘。

    删账号是常见操作（离职、误建、改名重开），磁盘上的原始材料与交付物却是
    不可再生的：备份的媒体侧对已删文件只保留有限的历史窗口，过窗就真没了。
    要连盘一起清必须显式传 purge_files=true。
    """
    from app.config import get_config
    from conftest import login_fresh_user

    username = "keepdisk"
    victim = login_fresh_user(raw_client, admin_client, username, "KeepDisk#2026")

    r = victim.post(f"{API}/cases", json={"module": "disclosure", "title": "留档材料"})
    case_id = r.json()["id"]
    up = victim.post(
        f"{API}/cases/{case_id}/files",
        files=[("files", ("重要材料.md", b"# keep me", "text/markdown"))],
    )
    assert up.status_code == 201, up.text

    case_dir = get_config().uploads_dir / case_id
    assert case_dir.is_dir()

    d = admin_client.delete(f"{API}/admin/users/{victim.user['id']}")
    assert d.status_code == 200, d.text
    assert case_dir.is_dir(), "默认删账号不该把磁盘上的材料一并删掉"


def test_delete_user_purges_disk_when_asked(admin_client, raw_client) -> None:
    """显式要求时仍然照删——默认关不等于做不到。"""
    from app.config import get_config
    from conftest import login_fresh_user

    username = "purgedisk"
    victim = login_fresh_user(raw_client, admin_client, username, "PurgeDisk#2026")

    r = victim.post(f"{API}/cases", json={"module": "disclosure", "title": "该删的"})
    case_id = r.json()["id"]
    victim.post(
        f"{API}/cases/{case_id}/files",
        files=[("files", ("x.md", b"# bye", "text/markdown"))],
    )
    case_dir = get_config().uploads_dir / case_id
    assert case_dir.is_dir()

    d = admin_client.delete(
        f"{API}/admin/users/{victim.user['id']}", params={"purge_files": "true"}
    )
    assert d.status_code == 200, d.text
    assert not case_dir.exists()
