# -*- coding: utf-8 -*-
"""自助注册：注册 → 待审 → 管理员放行 → 可登录。

审核制的要点是「注册这一步不带来任何权限」，所以这组用例重点盯两件事：
待审账号登录必须被拦下，且拦下的理由要与「已停用」区分开。
"""
import pytest
from fastapi.testclient import TestClient

API = "/api/v1"


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    """每个用例前清空注册限流。

    限流是按来源 IP 计数的，而测试全部来自同一个客户端——
    不清的话前一个用例的请求会把后一个顶成 429。
    """
    from app.services.rate_limit import registration_limiter

    registration_limiter.reset()
    yield
    registration_limiter.reset()


@pytest.fixture
def open_registration(admin_client: TestClient):
    """把注册打开。

    默认是**关闭**的——那是安全边界，不是产品口味：注册是本平台唯一不需要身份
    就能写库的入口，裸库默认开着意味着一次部署就对整个互联网开放了注册。
    所以这些用例必须显式打开，不能依赖默认值。
    """
    admin_client.put(f"{API}/admin/registration", json={"allow_registration": True})
    yield
    admin_client.put(f"{API}/admin/registration", json={"allow_registration": False})


@pytest.fixture
def anon(raw_client: TestClient) -> TestClient:
    """匿名客户端。

    raw_client 是整个会话共用的，可能带着别处登录留下的 cookie；
    而注册这条路必须在「完全没有身份」的前提下验证，所以每次先清干净。
    """
    raw_client.cookies.clear()
    return raw_client


def _register(client: TestClient, username: str, password: str = "GoodPass#2026"):
    return client.post(
        f"{API}/auth/register",
        json={"username": username, "password": password, "display_name": "测试用户"},
    )


def test_register_creates_pending_account(anon: TestClient, open_registration) -> None:
    r = _register(anon, "newcomer")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert "审核" in body["message"]
    # 注册不该签发会话
    assert "pa_session" not in r.cookies


def test_pending_cannot_login_and_reason_is_specific(anon: TestClient, open_registration) -> None:
    """待审与停用要分开讲，否则用户以为自己被拒了。"""
    _register(anon, "waiting")
    r = anon.post(f"{API}/auth/login", json={"username": "waiting", "password": "GoodPass#2026"})
    assert r.status_code == 401
    body = r.json()
    assert body["code"] == "pending"
    assert "审核" in body["detail"]
    assert "停用" not in body["detail"]


def test_duplicate_username_is_reported(anon: TestClient, open_registration) -> None:
    """注册接口必须如实说「名字被占了」——那是用户改名的唯一依据。"""
    _register(anon, "taken")
    r = _register(anon, "taken")
    assert r.status_code == 400
    assert r.json()["code"] == "username_taken"


def test_weak_password_rejected(anon: TestClient, open_registration) -> None:
    r = _register(anon, "weakpw", password="123")
    assert r.status_code == 422


def test_bad_username_rejected(anon: TestClient, open_registration) -> None:
    for bad in ("1startswithdigit", "ab", "has space", "has@symbol"):
        r = _register(anon, bad)
        assert r.status_code == 422, f"{bad} 应被拒"


def test_admin_approves_then_login_works(admin_client: TestClient, anon: TestClient, open_registration) -> None:
    _register(anon, "approveme")

    listed = admin_client.get(f"{API}/admin/users", params={"status": "pending"})
    assert listed.status_code == 200
    pending = [u for u in listed.json()["items"] if u["username"] == "approveme"]
    assert len(pending) == 1, "待审账号应能按 status=pending 筛出来"

    user_id = pending[0]["id"]
    r = admin_client.patch(f"{API}/admin/users/{user_id}", json={"status": "active"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"

    r = anon.post(f"{API}/auth/login", json={"username": "approveme", "password": "GoodPass#2026"})
    assert r.status_code == 200, r.text


def test_registration_can_be_closed(admin_client: TestClient, anon: TestClient) -> None:
    r = admin_client.put(f"{API}/admin/registration", json={"allow_registration": False})
    assert r.status_code == 200
    assert r.json()["allow_registration"] is False

    assert anon.get(f"{API}/auth/registration-open").json()["open"] is False
    r = _register(anon, "toolate")
    assert r.status_code == 400
    assert r.json()["code"] == "registration_closed"

    # 开回来之后又能注册
    admin_client.put(f"{API}/admin/registration", json={"allow_registration": True})
    assert _register(anon, "toolate").status_code == 201


def test_closing_registration_keeps_existing_pending(
    admin_client: TestClient, anon: TestClient
) -> None:
    """关闭注册是「不再收新的」，不该把已经排队的人赶走。"""
    _register(anon, "alreadyqueued")
    admin_client.put(f"{API}/admin/registration", json={"allow_registration": False})

    policy = admin_client.get(f"{API}/admin/registration").json()
    assert policy["pending_count"] >= 1

    listed = admin_client.get(f"{API}/admin/users", params={"status": "pending"})
    assert any(u["username"] == "alreadyqueued" for u in listed.json()["items"])


def test_registration_endpoints_are_public(anon: TestClient) -> None:
    """未登录也必须能访问，否则新用户根本注册不了。"""
    assert anon.get(f"{API}/auth/registration-open").status_code == 200
    # 走到 400/422 说明已经进了业务逻辑，而不是被 401 挡在门外
    assert _register(anon, "ab").status_code != 401


def test_registration_closed_by_default(anon: TestClient) -> None:
    """裸库默认必须是关闭的。

    这条断言的方向就是修复本身：早先默认 True，于是一次部署就让
    yintuai.com 对整个互联网开放了注册，而没有任何管理员做过这个动作。
    """
    from app.db import database as db

    db.execute("DELETE FROM settings WHERE key='auth'")
    assert anon.get(f"{API}/auth/registration-open").json()["open"] is False
    r = _register(anon, "shouldfail")
    assert r.status_code == 400
    assert r.json()["code"] == "registration_closed"


def test_registration_is_rate_limited(anon: TestClient, open_registration) -> None:
    """注册必须限流。

    密码哈希是 argon2id（64 MiB / 约 50ms），而 db.arun 的线程槽位是全应用共用的——
    匿名请求每次换个新用户名打进来，几百条并发就能把槽位占满，
    连带把真实客户的每一个接口（案件列表、上传、流水线状态）一起拖垮。
    """
    from app.services.rate_limit import registration_limiter

    codes = [_register(anon, f"flood{i}").status_code for i in range(registration_limiter.limit + 3)]
    assert 429 in codes, "超出窗口配额后必须返回 429"
    first_429 = codes.index(429)
    assert first_429 <= registration_limiter.limit, "限流应在配额用尽后立即生效"


def test_rate_limit_response_tells_when_to_retry(anon: TestClient, open_registration) -> None:
    """429 要带 Retry-After，否则客户端只能盲目重试。"""
    from app.services.rate_limit import registration_limiter

    for i in range(registration_limiter.limit):
        _register(anon, f"quota{i}")
    r = _register(anon, "overflow")
    assert r.status_code == 429
    assert r.json()["code"] == "rate_limited"
    assert int(r.headers.get("Retry-After", "0")) > 0
