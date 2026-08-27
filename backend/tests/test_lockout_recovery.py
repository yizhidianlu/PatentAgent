# -*- coding: utf-8 -*-
"""账号锁定必须能自行恢复，且管理员必须能解锁。

原先 failed_logins 是**累计值**且锁定到期后从不复位：累到 10 次锁 60 分钟，
到期后再发一个错误密码就变成 11、仍然 ≥10、于是再锁 60 分钟——
每小时一个请求即可把任意账号永久锁死，而攻击者只需要知道用户名（admin 是免猜的）。
生产库里两个账号都是管理员，两个都锁上就等于把所有人永久关在门外，
恢复只能回到服务器控制台改 SQLite。
"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import database as db
from app.services import auth as auth_service

API = "/api/v1"


@pytest.fixture(autouse=True)
def _db(raw_client: TestClient):
    """建表由 lifespan 完成——直接调 service 的用例也得先把库准备好。"""
    return raw_client


def _mk_user(username: str = "locktest", password: str = "LockTest#2026") -> dict:
    row = auth_service.get_user_row_by_username(username)
    if row:
        db.execute("DELETE FROM users WHERE id=?", (row["id"],))
    return auth_service.create_user(
        username=username, password=password, role="user",
        display_name="锁定测试", must_change_password=False,
    )


def _fail_n(user_id: str, n: int) -> int:
    last = 0
    for _ in range(n):
        last = auth_service.record_login_failure(user_id)
    return last


def test_lock_triggers_after_threshold() -> None:
    """基本行为不能坏：连续失败够次数仍然要锁。"""
    user = _mk_user("lock_basic")
    assert _fail_n(user["id"], 4) == 0, "未达阈值不该锁"
    assert _fail_n(user["id"], 1) > 0, "第 5 次应触发锁定"


def test_expired_lock_resets_counter() -> None:
    """锁定到期后计数归零 —— 这是防永久续锁的关键。"""
    user = _mk_user("lock_expire")
    _fail_n(user["id"], 10)

    row = auth_service.get_user_row(user["id"])
    assert row["failed_logins"] == 10
    assert row["locked_until"]

    # 把锁定时间推到过去，模拟「已到期」
    past = auth_service._fmt(auth_service._now() - timedelta(minutes=1))
    db.execute("UPDATE users SET locked_until=? WHERE id=?", (past, user["id"]))

    seconds = auth_service.record_login_failure(user["id"])
    assert seconds == 0, "到期后的第一次失败不该立刻再锁 60 分钟"

    row = auth_service.get_user_row(user["id"])
    assert row["failed_logins"] == 1, "计数应重新开始，而不是累加到 11"


def test_cannot_be_locked_forever_by_one_request_per_hour() -> None:
    """攻击者每小时一个请求无法永久续锁。"""
    user = _mk_user("lock_forever")
    _fail_n(user["id"], 10)

    # 反复「等到期 → 发一个错误密码」，看能否一直维持 60 分钟锁定
    long_locks = 0
    for _ in range(4):
        past = auth_service._fmt(auth_service._now() - timedelta(minutes=1))
        db.execute("UPDATE users SET locked_until=? WHERE id=?", (past, user["id"]))
        seconds = auth_service.record_login_failure(user["id"])
        if seconds >= 3600:
            long_locks += 1
    assert long_locks == 0, "单个请求不该能反复续上 60 分钟锁定"


def test_admin_can_unlock(admin_client: TestClient) -> None:
    """管理员必须有一条不依赖「受害者能登录」的解锁路径。"""
    user = _mk_user("lock_admin")
    _fail_n(user["id"], 10)
    assert auth_service.locked_remaining_seconds(auth_service.get_user_row(user["id"])) > 0

    r = admin_client.post(f"{API}/admin/users/{user['id']}/unlock")
    assert r.status_code == 200, r.text
    assert r.json()["locked_until"] is None

    row = auth_service.get_user_row(user["id"])
    assert row["failed_logins"] == 0
    assert auth_service.locked_remaining_seconds(row) == 0


def test_unlock_requires_admin(client: TestClient) -> None:
    """普通用户不能解锁别人（也不能给自己解锁）。"""
    user = _mk_user("lock_perm")
    r = client.post(f"{API}/admin/users/{user['id']}/unlock")
    assert r.status_code in (401, 403)


def test_unlock_unknown_user_is_404(admin_client: TestClient) -> None:
    r = admin_client.post(f"{API}/admin/users/01NOTAREALUSERID0000000000/unlock")
    assert r.status_code == 404


def test_successful_login_still_clears_counter() -> None:
    """原有行为保留：登录成功也清零。"""
    user = _mk_user("lock_success")
    _fail_n(user["id"], 3)
    auth_service.record_login_success(user["id"])
    assert auth_service.get_user_row(user["id"])["failed_logins"] == 0
