# -*- coding: utf-8 -*-
"""自助注册：注册 → 待审 → 管理员放行 → 可登录。

审核制的要点是「注册这一步不带来任何权限」，所以这组用例重点盯两件事：
待审账号登录必须被拦下，且拦下的理由要与「已停用」区分开。
"""
import pytest
from fastapi.testclient import TestClient

API = "/api/v1"


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


def test_register_creates_pending_account(anon: TestClient) -> None:
    r = _register(anon, "newcomer")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert "审核" in body["message"]
    # 注册不该签发会话
    assert "pa_session" not in r.cookies


def test_pending_cannot_login_and_reason_is_specific(anon: TestClient) -> None:
    """待审与停用要分开讲，否则用户以为自己被拒了。"""
    _register(anon, "waiting")
    r = anon.post(f"{API}/auth/login", json={"username": "waiting", "password": "GoodPass#2026"})
    assert r.status_code == 401
    body = r.json()
    assert body["code"] == "pending"
    assert "审核" in body["detail"]
    assert "停用" not in body["detail"]


def test_duplicate_username_is_reported(anon: TestClient) -> None:
    """注册接口必须如实说「名字被占了」——那是用户改名的唯一依据。"""
    _register(anon, "taken")
    r = _register(anon, "taken")
    assert r.status_code == 400
    assert r.json()["code"] == "username_taken"


def test_weak_password_rejected(anon: TestClient) -> None:
    r = _register(anon, "weakpw", password="123")
    assert r.status_code == 422


def test_bad_username_rejected(anon: TestClient) -> None:
    for bad in ("1startswithdigit", "ab", "has space", "has@symbol"):
        r = _register(anon, bad)
        assert r.status_code == 422, f"{bad} 应被拒"


def test_admin_approves_then_login_works(admin_client: TestClient, anon: TestClient) -> None:
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
