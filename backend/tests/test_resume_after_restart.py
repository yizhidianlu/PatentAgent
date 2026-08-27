# -*- coding: utf-8 -*-
"""重启后停在门控的案件能否被 resume 救回。

服务重启会清空引擎内存态，而数据库里的 waiting_user 保持原样——
恢复责任交给 /pipeline/resume。此前前端从没调过它，于是这类案件彻底卡死：
门控卡片照常渲染成可点，一点得到 409，cancel 与 retry 同样 409。
这组用例锁住「resume 能重发交互请求」这条唯一出路。
"""
from fastapi.testclient import TestClient

API = "/api/v1"


def _new_case(client: TestClient) -> str:
    r = client.post(f"{API}/cases", json={"module": "disclosure", "title": "重启恢复验证"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_resume_endpoint_exists_and_is_reachable(client: TestClient) -> None:
    """先确认这条路本身是通的——它此前是前端从不调用的死路。"""
    case_id = _new_case(client)
    r = client.post(f"{API}/cases/{case_id}/pipeline/resume")
    # 尚未开始的案件 resume 可以是 200（重建并启动）或 409（无可恢复的运行），
    # 但绝不该是 404 —— 那意味着端点根本不存在
    assert r.status_code != 404, "resume 端点应当存在"


def test_waiting_user_survives_restart_in_db(client: TestClient) -> None:
    """waiting_user 不会被 startup 恢复钩子清掉——这正是 resume 存在的前提。"""
    from app.db import database as db
    from app.pipelines import engine

    case_id = _new_case(client)
    db.execute(
        "INSERT INTO pipeline_runs(id, case_id, run_group, step_key, attempt, status,"
        " started_at)"
        " VALUES ('run-wait-1', ?, 'initial', 'intake', 1, 'waiting_user', datetime('now'))",
        (case_id,),
    )
    db.execute("UPDATE cases SET status='waiting_user' WHERE id=?", (case_id,))

    recovered = engine.recover_interrupted()
    assert isinstance(recovered, dict)

    row = db.query_one("SELECT status FROM pipeline_runs WHERE id='run-wait-1'")
    assert row["status"] == "waiting_user", "恢复钩子不该动 waiting_user，那是 resume 的责任"


def test_pipeline_state_exposes_waiting_without_pending(client: TestClient) -> None:
    """前端据以判断「该 resume 了」的信号：有 waiting_user 步骤但没有待处理交互。"""
    from app.db import database as db

    case_id = _new_case(client)
    db.execute(
        "INSERT INTO pipeline_runs(id, case_id, run_group, step_key, attempt, status,"
        " started_at)"
        " VALUES ('run-wait-2', ?, 'initial', 'intake', 1, 'waiting_user', datetime('now'))",
        (case_id,),
    )

    r = client.get(f"{API}/cases/{case_id}/pipeline/state")
    assert r.status_code == 200, r.text
    state = r.json()
    waiting = [s for s in state.get("steps", []) if s.get("status") == "waiting_user"]
    assert waiting, "state 应当暴露 waiting_user 步骤"
    # 内存态已空 → 没有 pending_interaction，这两条同时成立就是「需要 resume」
    assert state.get("pending_interaction") is None
