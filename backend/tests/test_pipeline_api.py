"""M3 流水线 REST 契约测试（backend-architecture.md §3.4）。

走真实 HTTP（TestClient + lifespan）：start / state / input / retry / cancel / resume
的状态码与响应体形状，以及 demo 流水线（registry.build_demo_steps）的推进。

cases.module 有 CHECK 约束（只允许四个真实模块名），故把 demo 工厂临时注册到
'reader' 名下，测试结束还原注册表。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.pipelines import registry

API = "/api/v1"
DEMO_MODULE = "reader"



@pytest.fixture(scope="module", autouse=True)
def demo_pipeline(client: TestClient):
    """把 demo 三步流水线临时挂到真实模块名下，用完还原。"""
    previous = registry.get_builder(DEMO_MODULE)
    registry.register(DEMO_MODULE, registry.build_demo_steps)
    yield
    if previous is None:
        registry._builders.pop(DEMO_MODULE, None)
    else:
        registry.register(DEMO_MODULE, previous)


def _new_case(client: TestClient, title: str, module: str = DEMO_MODULE) -> str:
    resp = client.post(f"{API}/cases", json={"module": module, "title": title})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _state(client: TestClient, case_id: str) -> dict[str, Any]:
    resp = client.get(f"{API}/cases/{case_id}/pipeline/state")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _steps(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {s["key"]: s for s in state["steps"]}


def _wait_state(
    client: TestClient,
    case_id: str,
    pred: Callable[[dict[str, Any]], bool],
    timeout: float = 20.0,
) -> dict[str, Any]:
    """轮询 GET /pipeline/state 直到条件成立（流水线在后台事件循环推进）。"""
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _state(client, case_id)
        if pred(last):
            return last
        time.sleep(0.05)
    raise AssertionError(f"等待流水线状态超时；最后一次 state={last}")


def _case_status(client: TestClient, case_id: str) -> str:
    resp = client.get(f"{API}/cases/{case_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()["status"]


def _wait_case_status(client: TestClient, case_id: str, status: str, timeout: float = 20.0) -> None:
    """轮询案件状态（引擎回写 cases.status 与 run 行不在同一次写入里）。"""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        last = _case_status(client, case_id)
        if last == status:
            return
        time.sleep(0.05)
    raise AssertionError(f"等待案件状态 {status} 超时；最后一次 status={last}")


# ---------------------------------------------------------------------------
# start 之前：state 就能列出全 pending 的步骤表
# ---------------------------------------------------------------------------

def test_state_before_start_lists_pending_steps(client: TestClient) -> None:
    case_id = _new_case(client, "未启动的案件")
    state = _state(client, case_id)

    assert state["run_group"] == "initial"
    assert state["pending_interaction"] is None
    assert [s["key"] for s in state["steps"]] == ["prepare", "gate_form", "flaky"]
    assert [s["name_zh"] for s in state["steps"]] == ["准备", "表单门控", "可失败步"]
    assert all(s["status"] == "pending" and s["attempt"] == 0 for s in state["steps"])
    assert all(s["error"] is None for s in state["steps"])


def test_unknown_case_returns_404(client: TestClient) -> None:
    missing = "no-such-case-id"
    assert client.post(f"{API}/cases/{missing}/pipeline/start", json={}).status_code == 404
    assert client.get(f"{API}/cases/{missing}/pipeline/state").status_code == 404
    assert client.post(
        f"{API}/cases/{missing}/pipeline/input", json={"step_key": "x", "payload": {}}
    ).status_code == 404
    assert client.post(f"{API}/cases/{missing}/pipeline/cancel").status_code == 404
    assert client.post(f"{API}/cases/{missing}/pipeline/resume").status_code == 404
    assert client.post(f"{API}/cases/{missing}/pipeline/retry").status_code == 404


def test_unregistered_module_returns_400(client: TestClient) -> None:
    """模块流水线尚未注册 → start 400（而非 500）。"""
    for module in ("oa", "paper2patent", "disclosure"):
        if registry.get_builder(module) is None:
            break
    else:  # pragma: no cover —— 后续模块全注册后本用例自动跳过
        pytest.skip("四个模块的流水线均已注册，无法构造未注册场景")

    case_id = _new_case(client, "未注册模块", module=module)
    resp = client.post(f"{API}/cases/{case_id}/pipeline/start", json={})
    assert resp.status_code == 400
    assert "尚未注册" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 主流程：start → 门控 → input → 失败 → retry → 完成
# ---------------------------------------------------------------------------

def test_full_pipeline_rest_contract(client: TestClient) -> None:
    case_id = _new_case(client, "REST 全流程")

    # —— start：202 + {run_group} ——
    started = client.post(f"{API}/cases/{case_id}/pipeline/start", json={"fail_attempts": 1})
    assert started.status_code == 202, started.text
    assert started.json() == {"run_group": "initial"}

    # —— 推进到门控：pending_interaction 出现 ——
    state = _wait_state(client, case_id, lambda s: s["pending_interaction"] is not None)
    pending = state["pending_interaction"]
    assert pending["step_key"] == "gate_form"
    assert pending["kind"] == "form"
    assert pending["prompt"] == "请填写演示表单"
    assert pending["default"] == {"answer": 0}
    # 契约字段名是 schema（模型内部字段名 schema_ 不得外泄）
    assert "schema" in pending and "schema_" not in pending
    assert pending["schema"]["properties"]["answer"]["type"] == "integer"

    steps = _steps(state)
    assert steps["prepare"]["status"] == "done" and steps["prepare"]["attempt"] == 1
    assert steps["gate_form"]["status"] == "waiting_user"
    assert steps["flaky"]["status"] == "pending"
    _wait_case_status(client, case_id, "waiting_user")

    # —— 运行中重复 start → 409 ——
    dup = client.post(f"{API}/cases/{case_id}/pipeline/start", json={})
    assert dup.status_code == 409
    assert "正在运行" in dup.json()["detail"]
    # resume 同样被并发保护挡住
    assert client.post(f"{API}/cases/{case_id}/pipeline/resume").status_code == 409

    # —— step_key 不匹配的 input → 409 ——
    wrong = client.post(
        f"{API}/cases/{case_id}/pipeline/input", json={"step_key": "flaky", "payload": {"answer": 1}}
    )
    assert wrong.status_code == 409
    assert "gate_form" in wrong.json()["detail"]
    assert _state(client, case_id)["pending_interaction"] is not None  # 未被误唤醒

    # —— 正确的 input → 202，状态推进 ——
    ok = client.post(
        f"{API}/cases/{case_id}/pipeline/input",
        json={"step_key": "gate_form", "payload": {"answer": 42}},
    )
    assert ok.status_code == 202, ok.text

    # flaky 首跑失败（fail_attempts=1）→ 流水线终止
    state = _wait_state(client, case_id, lambda s: _steps(s)["flaky"]["status"] == "failed")
    steps = _steps(state)
    assert state["pending_interaction"] is None
    assert steps["gate_form"]["status"] == "done"
    assert steps["flaky"]["attempt"] == 1
    assert "演示失败" in steps["flaky"]["error"]
    _wait_case_status(client, case_id, "failed")

    # 门控答案已进工作内存
    detail = client.get(f"{API}/cases/{case_id}").json()
    assert detail["state"]["form"] == {"answer": 42}

    # —— 非 waiting_user 时 input → 409 ——
    idle = client.post(
        f"{API}/cases/{case_id}/pipeline/input",
        json={"step_key": "gate_form", "payload": {"answer": 1}},
    )
    assert idle.status_code == 409
    assert "没有等待用户输入" in idle.json()["detail"]

    # —— 未运行时 cancel → 409 ——
    assert client.post(f"{API}/cases/{case_id}/pipeline/cancel").status_code == 409

    # —— retry 指定非 failed 步骤 → 409 ——
    bad_retry = client.post(f"{API}/cases/{case_id}/pipeline/retry", json={"step_key": "prepare"})
    assert bad_retry.status_code == 409
    assert "prepare" in bad_retry.json()["detail"]

    # —— retry（不带 body = 重试当前失败步）→ 202 并续跑至完成 ——
    retried = client.post(f"{API}/cases/{case_id}/pipeline/retry")
    assert retried.status_code == 202, retried.text
    assert retried.json() == {"run_group": "initial"}

    state = _wait_state(client, case_id, lambda s: _steps(s)["flaky"]["status"] == "done")
    steps = _steps(state)
    assert steps["flaky"]["attempt"] == 2                    # attempt+1 新 run 行
    assert steps["flaky"]["error"] is None
    assert steps["prepare"]["attempt"] == 1                  # 已 done 的步骤未重跑
    assert steps["gate_form"]["attempt"] == 1
    _wait_case_status(client, case_id, "completed")

    final_state = client.get(f"{API}/cases/{case_id}").json()["state"]
    assert final_state["flaky_ok"] is True and final_state["flaky_attempt"] == 2

    # —— 无失败步时 retry → 409 ——
    no_failed = client.post(f"{API}/cases/{case_id}/pipeline/retry")
    assert no_failed.status_code == 409
    assert "没有失败的步骤" in no_failed.json()["detail"]

    # —— 事件流已落库（SSE 重放源）——
    events = [m["meta"]["event"] for m in client.get(f"{API}/cases/{case_id}/messages").json()]
    assert "interaction_required" in events
    assert "error" in events
    assert events.count("pipeline_done") == 2                # 首跑 failed + 重试 done
    assert events[-1] == "pipeline_done"


# ---------------------------------------------------------------------------
# cancel：门控挂起中取消 → 202，案件回 draft，可重新 start
# ---------------------------------------------------------------------------

def test_cancel_and_restart(client: TestClient) -> None:
    case_id = _new_case(client, "取消再启动")

    assert client.post(f"{API}/cases/{case_id}/pipeline/start", json={}).status_code == 202
    _wait_state(client, case_id, lambda s: s["pending_interaction"] is not None)

    cancelled = client.post(f"{API}/cases/{case_id}/pipeline/cancel")
    assert cancelled.status_code == 202, cancelled.text

    state = _wait_state(client, case_id, lambda s: _steps(s)["gate_form"]["status"] == "cancelled")
    assert state["pending_interaction"] is None
    _wait_case_status(client, case_id, "draft")             # 回 draft：可重新 start/resume

    # 取消后可 resume：已 done 的 prepare 跳过，门控重发
    resumed = client.post(f"{API}/cases/{case_id}/pipeline/resume")
    assert resumed.status_code == 202, resumed.text
    assert resumed.json() == {"run_group": "initial"}

    state = _wait_state(client, case_id, lambda s: s["pending_interaction"] is not None)
    assert state["pending_interaction"]["step_key"] == "gate_form"
    assert _steps(state)["prepare"]["attempt"] == 1          # 未重跑
    assert _steps(state)["gate_form"]["attempt"] == 2        # 门控重跑 = 新 attempt

    # 收尾：答完门控让流水线跑完（fail_attempts 缺省 0 → flaky 一次通过）
    assert client.post(
        f"{API}/cases/{case_id}/pipeline/input",
        json={"step_key": "gate_form", "payload": {"answer": 7}},
    ).status_code == 202
    _wait_state(client, case_id, lambda s: _steps(s)["flaky"]["status"] == "done")
    _wait_case_status(client, case_id, "completed")
