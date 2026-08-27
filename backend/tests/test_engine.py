"""M3 流水线引擎测试：顺序执行 / 门控挂起 / 失败重试 / 取消 / 恢复 / 并发保护。

不经 HTTP：直接用测试内定义的 StepDef 列表调 engine.start()（以及一处直接
await engine.run_pipeline()），断言 DB（cases / pipeline_runs / messages）与
SSE hub 广播出的事件序列同时满足 backend-architecture.md §3.4 契约。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from ulid import ULID

from app.db import database as db
from app.pipelines import engine
from app.pipelines.engine import (
    Ctx,
    InteractionRequest,
    PipelineConflictError,
    StepDef,
    StepResult,
)
from app.services.sse import hub

# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _database():
    """建库跑迁移（本模块不起 FastAPI，直接用 db 层）。"""
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def _engine_state_guard():
    """兜底清理：任何测试都不得把运行中的任务/挂起态泄漏给下一个测试。"""
    yield
    for case_id, task in list(engine._tasks.items()):
        if not task.done():  # pragma: no cover —— 正常路径下测试自己已收尾
            task.cancel()
        engine._tasks.pop(case_id, None)
    engine._pending.clear()


def _new_case(module: str = "disclosure", title: str = "引擎测试案件") -> str:
    """直接插一行 cases（module 有 CHECK 约束，只能用四个真实模块名）。"""
    case_id = str(ULID())
    now = db.now_str()
    db.execute(
        "INSERT INTO cases(id, module, title, status, state_json, created_at, updated_at) "
        "VALUES (?,?,?,'draft','{}',?,?)",
        (case_id, module, title, now, now),
    )
    return case_id


def _case(case_id: str) -> dict[str, Any]:
    return dict(db.query_one("SELECT * FROM cases WHERE id=?", (case_id,)))


def _state(case_id: str) -> dict[str, Any]:
    return json.loads(_case(case_id)["state_json"] or "{}")


def _runs(case_id: str, run_group: str = "initial") -> list[dict[str, Any]]:
    """该案件某 run_group 的全部 run 行，按插入顺序。"""
    return [
        dict(r)
        for r in db.query_all(
            "SELECT * FROM pipeline_runs WHERE case_id=? AND run_group=? ORDER BY rowid ASC",
            (case_id, run_group),
        )
    ]


def _runs_of(case_id: str, step_key: str, run_group: str = "initial") -> list[dict[str, Any]]:
    return [r for r in _runs(case_id, run_group) if r["step_key"] == step_key]


def _messages(case_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in db.query_all("SELECT * FROM messages WHERE case_id=? ORDER BY seq ASC", (case_id,))
    ]


def _parse(payload: dict[str, Any]) -> dict[str, Any]:
    """hub 广播的 SSE payload → {event, data(已解析)}。"""
    data = payload["data"]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            pass
    return {"event": payload["event"], "data": data}


def _drain(queue: asyncio.Queue, sink: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把队列里已到达的事件全部取出追加到 sink。"""
    while True:
        try:
            sink.append(_parse(queue.get_nowait()))
        except asyncio.QueueEmpty:
            return sink


async def _wait_event(
    queue: asyncio.Queue, sink: list[dict[str, Any]], name: str, timeout: float = 10.0
) -> dict[str, Any]:
    """阻塞等待某个事件到达（沿途事件一并收进 sink），返回该事件。"""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"等待事件 {name} 超时；已收到 {_names(sink)}")
        payload = await asyncio.wait_for(queue.get(), timeout=remaining)
        event = _parse(payload)
        sink.append(event)
        if event["event"] == name:
            return event


def _names(events: list[dict[str, Any]]) -> list[str]:
    return [e["event"] for e in events]


def _of(events: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e["data"] for e in events if e["event"] == name]


def _step_status_seq(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(d["step_key"], d["status"]) for d in _of(events, "step_status")]


async def _wait_until(pred, timeout: float = 10.0) -> bool:
    """轮询等待条件成立。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# 1. 顺序执行
# ---------------------------------------------------------------------------

async def test_sequential_run_merges_state_and_emits_events() -> None:
    """三步流水线全 done：state_json 合并各步 output，事件含 step_status + pipeline_done。"""
    case_id = _new_case(title="顺序执行")
    seen: dict[str, Any] = {}

    async def s1(ctx: Ctx) -> dict[str, Any]:
        # Ctx 契约字段
        seen["case_id"] = ctx.case_id
        seen["case_title"] = ctx.case["title"]
        seen["run_group"] = ctx.run_group
        seen["step_key"] = ctx.step_key
        seen["attempt"] = ctx.attempt
        seen["start_payload"] = ctx.start_payload
        seen["services"] = (ctx.llm is not None, ctx.assets is not None, ctx.db is not None)
        await ctx.chat_delta("正在")   # persist=False，只广播不落库
        await ctx.chat_delta("准备…")
        await ctx.chat_done()
        return {"one": 1, "shared": "s1"}

    async def s2(ctx: Ctx) -> StepResult:
        assert ctx.state["one"] == 1                      # 上一步 output 已进工作内存
        row = await ctx.reload_case()                      # reload_case 可用
        assert row["id"] == case_id
        await ctx.doc_delta("spec", "# 标题")
        await ctx.doc_done("spec")
        return StepResult(output={"two": 2, "shared": "s2"})

    async def s3(ctx: Ctx) -> None:
        assert ctx.state["two"] == 2
        await ctx.emit("log", {"text": "第三步无输出"})
        # 不返回任何东西（handler → None）：引擎应视为空 output

    steps = [
        StepDef("s1", "第一步", s1),
        StepDef("s2", "第二步", s2),
        StepDef("s3", "第三步", s3),
    ]

    events: list[dict[str, Any]] = []
    async with hub.subscribe(case_id) as queue:
        task = engine.start(case_id, steps, start_payload={"seed": "abc"})
        assert engine.is_running(case_id) is True
        await asyncio.wait_for(task, timeout=20)
        await _wait_until(lambda: queue.qsize() == 0, timeout=1.0)
        _drain(queue, events)

    # Ctx 契约
    assert seen["case_id"] == case_id
    assert seen["case_title"] == "顺序执行"
    assert seen["run_group"] == "initial"
    assert seen["step_key"] == "s1"
    assert seen["attempt"] == 1
    assert seen["start_payload"] == {"seed": "abc"}
    assert seen["services"] == (True, True, True)

    # 案件与工作内存
    case = _case(case_id)
    assert case["status"] == "completed"
    assert case["current_step"] is None
    state = _state(case_id)
    assert state["one"] == 1 and state["two"] == 2
    assert state["shared"] == "s2"                        # 后步骤覆盖同名键
    assert state["_run_group"] == "initial"
    assert state["_start_payload"] == {"seed": "abc"}
    assert [d["key"] for d in state["_steps"]] == ["s1", "s2", "s3"]

    # 每步一行 pipeline_runs
    runs = _runs(case_id)
    assert [(r["step_key"], r["status"], r["attempt"]) for r in runs] == [
        ("s1", "done", 1), ("s2", "done", 1), ("s3", "done", 1)
    ]
    assert json.loads(runs[0]["output_json"]) == {"one": 1, "shared": "s1"}
    assert json.loads(runs[2]["output_json"]) == {}       # 返回 None → {}
    assert all(r["started_at"] and r["finished_at"] for r in runs)

    # 事件序列
    assert _step_status_seq(events) == [
        ("s1", "running"), ("s1", "done"),
        ("s2", "running"), ("s2", "done"),
        ("s3", "running"), ("s3", "done"),
    ]
    assert _names(events)[-1] == "pipeline_done"
    assert _of(events, "pipeline_done") == [{"run_group": "initial", "status": "done"}]

    # 流式通道：llm_delta 广播但不落库；llm_done 落库
    deltas = _of(events, "llm_delta")
    assert {d["channel"] for d in deltas} == {"chat", "doc"}
    assert [d["text"] for d in deltas if d["channel"] == "chat"] == ["正在", "准备…"]
    assert [d["step_key"] for d in deltas if d["channel"] == "chat"] == ["s1", "s1"]
    assert next(d for d in deltas if d["channel"] == "doc")["doc_id"] == "spec"

    msg_events = [json.loads(m["meta_json"])["event"] for m in _messages(case_id)]
    assert "llm_delta" not in msg_events                  # 高频增量不落库
    assert msg_events.count("llm_done") == 2
    assert msg_events.count("step_status") == 6
    assert msg_events[-1] == "pipeline_done"
    # 落库的事件带 step_key（重放归属）
    assert {m["step_key"] for m in _messages(case_id) if m["step_key"]} == {"s1", "s2", "s3"}
    assert engine.is_running(case_id) is False
    assert case_id not in engine._tasks


# ---------------------------------------------------------------------------
# 2. 门控（await_user 协程挂起）
# ---------------------------------------------------------------------------

async def test_gate_await_user_suspends_and_resumes() -> None:
    """含 await_user 的步骤挂起 → get_pending 可见 → submit_input 后续跑至完成。"""
    case_id = _new_case(title="门控挂起")
    after_ran = asyncio.Event()

    async def gate(ctx: Ctx) -> dict[str, Any]:
        answer = await ctx.await_user(
            InteractionRequest(
                kind="form",
                schema={"type": "object", "properties": {"n": {"type": "integer"}}},
                prompt="请填写数字",
                default={"n": 0},
            )
        )
        return {"answer": answer}

    async def after(ctx: Ctx) -> dict[str, Any]:
        after_ran.set()
        return {"doubled": ctx.state["answer"]["n"] * 2}

    steps = [
        StepDef("gate", "门控步", gate, gate="form"),
        StepDef("after", "后续步", after),
    ]

    events: list[dict[str, Any]] = []
    async with hub.subscribe(case_id) as queue:
        task = engine.start(case_id, steps)
        interaction = await _wait_event(queue, events, "interaction_required")

        # SSE interaction_required 契约字段
        assert interaction["data"] == {
            "step_key": "gate",
            "kind": "form",
            "schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
            "prompt": "请填写数字",
            "default": {"n": 0},
        }
        # get_pending（→ REST state.pending_interaction）
        pending = engine.get_pending(case_id)
        assert pending is not None
        assert pending["step_key"] == "gate"
        assert pending["kind"] == "form"
        assert pending["prompt"] == "请填写数字"
        assert pending["default"] == {"n": 0}
        assert pending["schema"]["type"] == "object"

        # 挂起期间：case + run 行均为 waiting_user
        assert _case(case_id)["status"] == "waiting_user"
        assert _runs_of(case_id, "gate")[0]["status"] == "waiting_user"
        assert ("gate", "waiting_user") in _step_status_seq(events)
        assert not after_ran.is_set()

        # 错误 step_key 不误唤醒
        with pytest.raises(PipelineConflictError, match="gate"):
            engine.submit_input(case_id, "after", {"n": 9})
        await asyncio.sleep(0.1)
        assert engine.get_pending(case_id) is not None
        assert not after_ran.is_set()
        assert _case(case_id)["status"] == "waiting_user"

        # 正确 step_key 唤醒
        engine.submit_input(case_id, "gate", {"n": 21})
        # 同一挂起重复提交 → 冲突
        with pytest.raises(PipelineConflictError, match="已提交"):
            engine.submit_input(case_id, "gate", {"n": 1})

        await asyncio.wait_for(task, timeout=20)
        await _wait_until(lambda: queue.qsize() == 0, timeout=1.0)
        _drain(queue, events)

    assert after_ran.is_set()
    assert engine.get_pending(case_id) is None
    assert _case(case_id)["status"] == "completed"
    state = _state(case_id)
    assert state["answer"] == {"n": 21}
    assert state["doubled"] == 42

    gate_run = _runs_of(case_id, "gate")[0]
    assert gate_run["status"] == "done"
    assert json.loads(gate_run["user_input_json"]) == {"n": 21}

    # running → waiting_user → running → done
    assert [s for s in _step_status_seq(events) if s[0] == "gate"] == [
        ("gate", "running"), ("gate", "waiting_user"), ("gate", "running"), ("gate", "done")
    ]
    assert _of(events, "pipeline_done") == [{"run_group": "initial", "status": "done"}]


async def test_submit_input_without_pending_conflicts() -> None:
    """没有挂起时提交输入 → PipelineConflictError（API 层映射 409）。"""
    case_id = _new_case(title="无挂起提交")
    assert engine.get_pending(case_id) is None
    with pytest.raises(PipelineConflictError, match="没有等待用户输入"):
        engine.submit_input(case_id, "gate", {"n": 1})


async def test_await_user_outside_engine_raises() -> None:
    """引擎外直接调 handler 时 await_user 应明确报错，而非静默挂死。"""
    ctx = Ctx("no-such-case", "initial", {}, None, {"id": "no-such-case"})
    with pytest.raises(RuntimeError, match="只能在流水线引擎调度的步骤内使用"):
        await ctx.await_user(kind="confirm", prompt="确认？")


# ---------------------------------------------------------------------------
# 3. 失败与重试
# ---------------------------------------------------------------------------

async def test_failure_stops_pipeline_then_retry_resumes() -> None:
    """中间步抛异常 → failed + error{retryable} + 流水线终止；retry 后 attempt=2 续跑完成。"""
    case_id = _new_case(title="失败重试")
    calls = {"s1": 0, "s2": 0, "s3": 0}

    async def s1(ctx: Ctx) -> dict[str, Any]:
        calls["s1"] += 1
        return {"s1_done": True}

    async def s2(ctx: Ctx) -> dict[str, Any]:
        calls["s2"] += 1
        if ctx.attempt == 1:
            raise ValueError("第二步炸了")
        return {"s2_attempt": ctx.attempt}

    async def s3(ctx: Ctx) -> dict[str, Any]:
        calls["s3"] += 1
        return {"s3_done": True}

    steps = [
        StepDef("s1", "第一步", s1),
        StepDef("s2", "第二步", s2),
        StepDef("s3", "第三步", s3),
    ]

    # —— 首跑：s2 失败，s3 不应执行 ——
    first: list[dict[str, Any]] = []
    async with hub.subscribe(case_id) as queue:
        task = engine.start(case_id, steps)
        await asyncio.wait_for(task, timeout=20)          # 步骤失败不炸引擎任务
        await _wait_until(lambda: queue.qsize() == 0, timeout=1.0)
        _drain(queue, first)

    assert calls == {"s1": 1, "s2": 1, "s3": 0}
    assert _case(case_id)["status"] == "failed"
    s2_run = _runs_of(case_id, "s2")[0]
    assert s2_run["status"] == "failed"
    assert s2_run["error"] == "ValueError: 第二步炸了"
    assert _runs_of(case_id, "s3") == []                  # 后续步骤未起 run 行

    errors = _of(first, "error")
    assert len(errors) == 1
    assert errors[0]["step_key"] == "s2"
    assert errors[0]["msg"] == "ValueError: 第二步炸了"
    assert errors[0]["retryable"] is True
    assert _step_status_seq(first) == [
        ("s1", "running"), ("s1", "done"), ("s2", "running"), ("s2", "failed")
    ]
    assert _of(first, "pipeline_done") == [{"run_group": "initial", "status": "failed"}]

    # —— 重试：同 run_group 再 start（= REST /pipeline/retry 的语义）——
    second: list[dict[str, Any]] = []
    async with hub.subscribe(case_id) as queue:
        task = engine.start(case_id, steps, run_group="initial")
        await asyncio.wait_for(task, timeout=20)
        await _wait_until(lambda: queue.qsize() == 0, timeout=1.0)
        _drain(queue, second)

    assert calls == {"s1": 1, "s2": 2, "s3": 1}           # s1 已 done → 跳过，不重复执行
    assert _case(case_id)["status"] == "completed"
    state = _state(case_id)
    assert state["s1_done"] is True and state["s2_attempt"] == 2 and state["s3_done"] is True

    s2_runs = _runs_of(case_id, "s2")
    assert [(r["attempt"], r["status"]) for r in s2_runs] == [(1, "failed"), (2, "done")]
    assert len(_runs_of(case_id, "s1")) == 1              # 跳过的步骤不新增 run 行
    assert _step_status_seq(second) == [
        ("s2", "running"), ("s2", "done"), ("s3", "running"), ("s3", "done")
    ]
    assert _of(second, "pipeline_done") == [{"run_group": "initial", "status": "done"}]


async def test_failure_marks_retryable_false() -> None:
    """retryable=False 的步骤失败时 error 事件如实回传（前端据此隐藏重试按钮）。"""
    case_id = _new_case(title="不可重试")

    async def boom(ctx: Ctx) -> dict[str, Any]:
        raise RuntimeError("不可重试的失败")

    steps = [StepDef("boom", "致命步", boom, retryable=False)]

    events: list[dict[str, Any]] = []
    async with hub.subscribe(case_id) as queue:
        # 直接 await run_pipeline（不经任务表）也应工作
        await asyncio.wait_for(engine.run_pipeline(case_id, steps, "initial"), timeout=20)
        await _wait_until(lambda: queue.qsize() == 0, timeout=1.0)
        _drain(queue, events)

    assert _of(events, "error")[0]["retryable"] is False
    assert _case(case_id)["status"] == "failed"
    assert _runs_of(case_id, "boom")[0]["status"] == "failed"


async def test_engine_level_exception_still_terminates() -> None:
    """handler 返回不支持的类型（TypeError 逃出 _run_step 的保护范围）时的引擎级兜底。

    回归用例：这类异常曾经导致 run 行永远停在 running、且既无 error 也无
    pipeline_done —— 前端会一直转圈。现在必须收尾并补发终止事件。
    """
    case_id = _new_case(title="引擎级异常")
    after_ran = asyncio.Event()

    async def bad(ctx: Ctx) -> Any:
        return "字符串不是合法的 StepResult/dict/None"

    async def after(ctx: Ctx) -> dict[str, Any]:              # pragma: no cover
        after_ran.set()
        return {"unreachable": True}

    steps = [StepDef("bad", "非法返回步", bad), StepDef("after", "后续步", after)]

    events: list[dict[str, Any]] = []
    async with hub.subscribe(case_id) as queue:
        task = engine.start(case_id, steps)
        await asyncio.wait_for(task, timeout=20)
        await _wait_until(lambda: queue.qsize() == 0, timeout=1.0)
        _drain(queue, events)

    assert not after_ran.is_set()
    assert _case(case_id)["status"] == "failed"

    run = _runs_of(case_id, "bad")[0]
    assert run["status"] == "failed"                          # 不得残留 running
    assert run["finished_at"]
    assert "TypeError" in run["error"]

    errors = _of(events, "error")
    assert len(errors) == 1
    assert errors[0]["step_key"] == "bad"
    assert "TypeError" in errors[0]["msg"]
    assert errors[0]["retryable"] is False
    assert _of(events, "pipeline_done") == [{"run_group": "initial", "status": "failed"}]
    assert engine.is_running(case_id) is False


# ---------------------------------------------------------------------------
# 4. 取消
# ---------------------------------------------------------------------------

async def test_cancel_running_pipeline() -> None:
    """运行中取消：run 行 cancelled、案件回 draft、任务表清理、pipeline_done{cancelled}。"""
    case_id = _new_case(title="取消运行")
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def slow(ctx: Ctx) -> dict[str, Any]:
        entered.set()
        await asyncio.sleep(30)
        finished.set()                                     # pragma: no cover
        return {"never": True}

    async def never(ctx: Ctx) -> dict[str, Any]:           # pragma: no cover
        return {"unreachable": True}

    steps = [StepDef("slow", "长耗时步", slow), StepDef("never", "不该执行", never)]

    events: list[dict[str, Any]] = []
    async with hub.subscribe(case_id) as queue:
        task = engine.start(case_id, steps)
        await asyncio.wait_for(entered.wait(), timeout=10)
        assert engine.is_running(case_id) is True

        engine.cancel(case_id)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)

        await _wait_event(queue, events, "pipeline_done")

    assert not finished.is_set()
    assert _of(events, "pipeline_done") == [{"run_group": "initial", "status": "cancelled"}]
    assert _runs_of(case_id, "slow")[0]["status"] == "cancelled"
    assert _runs_of(case_id, "slow")[0]["finished_at"]
    assert _runs_of(case_id, "never") == []
    assert _case(case_id)["status"] == "draft"             # 回 draft：可重新 start/resume
    # 任务表与挂起表清理
    assert engine.is_running(case_id) is False
    assert case_id not in engine._tasks
    assert engine.get_pending(case_id) is None


async def test_cancel_while_waiting_user() -> None:
    """挂起在门控上时取消：waiting_user 的 run 行也回写 cancelled，挂起表清空。"""
    case_id = _new_case(title="门控中取消")

    async def gate(ctx: Ctx) -> dict[str, Any]:
        return {"answer": await ctx.await_user(kind="confirm", prompt="确认继续？")}

    steps = [StepDef("gate", "门控步", gate, gate="confirm")]

    events: list[dict[str, Any]] = []
    async with hub.subscribe(case_id) as queue:
        task = engine.start(case_id, steps)
        await _wait_event(queue, events, "interaction_required")
        assert engine.get_pending(case_id) is not None

        engine.cancel(case_id)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        await _wait_event(queue, events, "pipeline_done")

    assert _of(events, "pipeline_done")[-1]["status"] == "cancelled"
    assert _runs_of(case_id, "gate")[0]["status"] == "cancelled"
    assert engine.get_pending(case_id) is None
    assert _case(case_id)["status"] == "draft"


async def test_cancel_when_not_running_conflicts() -> None:
    """未在运行时取消 → PipelineConflictError（API 层 409）。"""
    case_id = _new_case(title="空取消")
    with pytest.raises(PipelineConflictError, match="未在运行"):
        engine.cancel(case_id)


# ---------------------------------------------------------------------------
# 5. 恢复（startup 钩子 + waiting_user 重发）
# ---------------------------------------------------------------------------

def _seed_run(
    case_id: str, step_key: str, status: str, attempt: int = 1, run_group: str = "initial"
) -> str:
    run_id = str(ULID())
    db.execute(
        "INSERT INTO pipeline_runs(id, case_id, run_group, step_key, attempt, status, started_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (run_id, case_id, run_group, step_key, attempt, status, db.now_str()),
    )
    return run_id


async def test_recover_interrupted_marks_running_as_failed() -> None:
    """进程猝死遗留的 running：run → failed('interrupted')，case → failed。"""
    case_id = _new_case(title="中断恢复")
    run_id = _seed_run(case_id, "half_done", "running")
    db.execute("UPDATE cases SET status='running', current_step='half_done' WHERE id=?", (case_id,))

    result = engine.recover_interrupted()

    assert result["runs_failed"] >= 1 and result["cases_failed"] >= 1
    row = dict(db.query_one("SELECT * FROM pipeline_runs WHERE id=?", (run_id,)))
    assert row["status"] == "failed"
    assert row["error"] == "interrupted"
    assert row["finished_at"]
    assert _case(case_id)["status"] == "failed"

    # 幂等：再调一次不应再改动任何行
    again = engine.recover_interrupted()
    assert again == {"runs_failed": 0, "cases_failed": 0}


async def test_recover_keeps_waiting_user_and_resume_reemits_interaction() -> None:
    """waiting_user 不被恢复钩子改动；resume（同 run_group 再 start）重发 interaction_required。"""
    case_id = _new_case(title="挂起恢复")
    done_run = _seed_run(case_id, "prepare", "done")
    waiting_run = _seed_run(case_id, "gate", "waiting_user")
    db.execute(
        "UPDATE cases SET status='waiting_user', current_step='gate', state_json=? WHERE id=?",
        (json.dumps({"_run_group": "initial", "prepared": True}, ensure_ascii=False), case_id),
    )

    # 恢复钩子不动 waiting_user
    engine.recover_interrupted()
    assert dict(db.query_one("SELECT * FROM pipeline_runs WHERE id=?", (done_run,)))["status"] == "done"
    assert (
        dict(db.query_one("SELECT * FROM pipeline_runs WHERE id=?", (waiting_run,)))["status"]
        == "waiting_user"
    )
    assert _case(case_id)["status"] == "waiting_user"

    calls = {"prepare": 0, "gate": 0}

    async def prepare(ctx: Ctx) -> dict[str, Any]:        # pragma: no cover —— 应被跳过
        calls["prepare"] += 1
        return {"prepared": True}

    async def gate(ctx: Ctx) -> dict[str, Any]:
        calls["gate"] += 1
        return {"answer": await ctx.await_user(kind="form", prompt="重发的表单", default={"v": 1})}

    steps = [
        StepDef("prepare", "准备", prepare),
        StepDef("gate", "门控步", gate, gate="form"),
    ]

    # resolve_run_group 从 state_json 读回上次的 run_group
    assert engine.resolve_run_group(_case(case_id)) == "initial"

    events: list[dict[str, Any]] = []
    async with hub.subscribe(case_id) as queue:
        task = engine.start(case_id, steps, run_group="initial")
        interaction = await _wait_event(queue, events, "interaction_required")
        assert interaction["data"]["step_key"] == "gate"
        assert interaction["data"]["prompt"] == "重发的表单"
        assert interaction["data"]["default"] == {"v": 1}

        # 重跑的门控步是新 attempt（旧 waiting_user 行仍在）
        gate_runs = _runs_of(case_id, "gate")
        assert [(r["attempt"], r["status"]) for r in gate_runs] == [(1, "waiting_user"), (2, "waiting_user")]

        engine.submit_input(case_id, "gate", {"v": 7})
        await asyncio.wait_for(task, timeout=20)
        await _wait_until(lambda: queue.qsize() == 0, timeout=1.0)
        _drain(queue, events)

    assert calls == {"prepare": 0, "gate": 1}              # prepare 已 done → 跳过
    assert _case(case_id)["status"] == "completed"
    assert _state(case_id)["answer"] == {"v": 7}
    assert _state(case_id)["prepared"] is True             # 跳过步骤的产物由 state_json 带回
    assert _of(events, "pipeline_done") == [{"run_group": "initial", "status": "done"}]


# ---------------------------------------------------------------------------
# 6. 并发保护
# ---------------------------------------------------------------------------

async def test_duplicate_start_rejected() -> None:
    """同一案件重复 start → PipelineConflictError（API 层 409）。"""
    case_id = _new_case(title="并发保护")
    entered = asyncio.Event()
    second_ran = {"count": 0}

    async def slow(ctx: Ctx) -> dict[str, Any]:
        entered.set()
        second_ran["count"] += 1
        await asyncio.sleep(30)
        return {"never": True}                             # pragma: no cover

    steps = [StepDef("slow", "长耗时步", slow)]

    task = engine.start(case_id, steps)
    await asyncio.wait_for(entered.wait(), timeout=10)

    with pytest.raises(PipelineConflictError, match="正在运行中"):
        engine.start(case_id, steps)
    assert second_ran["count"] == 1

    # 别的案件不受影响
    other_id = _new_case(title="另一个案件")
    other_done = asyncio.Event()

    async def quick(ctx: Ctx) -> dict[str, Any]:
        other_done.set()
        return {"ok": True}

    other_task = engine.start(other_id, [StepDef("quick", "快步", quick)])
    await asyncio.wait_for(other_task, timeout=20)
    assert other_done.is_set()
    assert _case(other_id)["status"] == "completed"

    engine.cancel(case_id)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    assert engine.is_running(case_id) is False

    # 取消后可以重新 start
    reran = asyncio.Event()

    async def again(ctx: Ctx) -> dict[str, Any]:
        reran.set()
        return {"again": True}

    task2 = engine.start(case_id, [StepDef("again", "重来", again)])
    await asyncio.wait_for(task2, timeout=20)
    assert reran.is_set()
    assert _case(case_id)["status"] == "completed"
