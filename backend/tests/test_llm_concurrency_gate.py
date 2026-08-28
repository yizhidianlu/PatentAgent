# -*- coding: utf-8 -*-
"""按上游 host 的 LLM 并发闸：多人同时跑流水线时，上游看到的并发不超上限。

两档模型共用一个订阅时，N 个用户 = N 路并发打同一家。订阅并发上限是硬的
（智谱 1302「并发数过高」），各调用方各自指数退避的结果是雪崩式的
「触发限流（第 N/8 次）」。在自己门口 FIFO 排队，429 就从常态变成异常。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.db import database as db_mod
from app.services import llm

MODEL = "gate-test-model"
OVERRIDE = {"base_url": "https://gate.example/v1", "api_key": "sk-gate", "model": MODEL}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    db_mod.init_db()
    llm._load_quirks_once()
    llm._host_gates.clear()
    monkeypatch.setattr(llm, "_concurrency_limit", lambda: 2)
    yield
    llm._host_gates.clear()
    llm._MODEL_QUIRKS.pop(MODEL, None)
    llm._persist_quirks()


def _chunk(content=None, finish=None, usage=None):
    delta = SimpleNamespace(content=content, model_extra=None)
    choice = SimpleNamespace(delta=delta, finish_reason=finish)
    return SimpleNamespace(choices=[choice] if (content or finish) else [], usage=usage)


class SlowStream:
    """每片之间让出事件循环并小睡，制造真实的「生成占用时间」。"""

    def __init__(self, tracker: "Tracker"):
        self._tracker = tracker
        self._chunks = [
            _chunk(content="片段一"), _chunk(content="片段二"), _chunk(finish="stop"),
            _chunk(usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2)),
        ]

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        await asyncio.sleep(0.02)
        try:
            return next(self._it)
        except StopIteration:
            self._tracker.leave()
            raise StopAsyncIteration from None


class Tracker:
    def __init__(self):
        self.inflight = 0
        self.max_inflight = 0

    def enter(self):
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)

    def leave(self):
        self.inflight -= 1


class FakeClient:
    def __init__(self, tracker: Tracker):
        self._tracker = tracker

    @property
    def chat(self):
        tracker = self._tracker

        class _C:
            async def create(self, **kw):
                assert kw.get("stream"), "本用例只走流式路径"
                tracker.enter()          # 上游视角：create 即开始占一路并发
                return SlowStream(tracker)

        return SimpleNamespace(completions=_C())


@pytest.mark.anyio
async def test_upstream_never_sees_more_than_the_limit(monkeypatch) -> None:
    """8 路并发进来，上游最多同时看到 2 路（闸设 2）；全部正常完成。"""
    llm._MODEL_QUIRKS[MODEL] = {"stream_chat": True}   # 强制流式路径
    tracker = Tracker()
    monkeypatch.setattr(llm, "_client", lambda *a, **k: FakeClient(tracker))

    async def one() -> str:
        return await llm.chat([{"role": "user", "content": "hi"}], override=OVERRIDE)

    results = await asyncio.gather(*[one() for _ in range(8)])

    assert all(r == "片段一片段二" for r in results), "排队不能弄丢任何一路调用"
    assert tracker.max_inflight <= 2, (
        f"上游看到了 {tracker.max_inflight} 路并发——闸没有生效，"
        "订阅的并发上限会把所有人拖进 429 雪崩"
    )
    assert tracker.max_inflight == 2, "闸也不该把并发压成串行（那是白白拖慢所有人）"


@pytest.mark.anyio
async def test_slot_released_on_failure(monkeypatch) -> None:
    """失败路径必须归还通道，否则几次错误后整个平台的 LLM 调用永久卡死。"""
    llm._MODEL_QUIRKS[MODEL] = {"stream_chat": True}

    class Boom:
        @property
        def chat(self):
            class _C:
                async def create(self, **kw):
                    raise ValueError("bad request")   # 不可重试，直接上抛

            return SimpleNamespace(completions=_C())

    monkeypatch.setattr(llm, "_client", lambda *a, **k: Boom())

    for _ in range(5):    # 超过闸容量（2）的失败次数：若不释放，第 3 次就会挂起
        with pytest.raises(ValueError):
            await asyncio.wait_for(
                llm.chat([{"role": "user", "content": "hi"}], override=OVERRIDE),
                timeout=5,
            )

    gate = llm._gate_for(OVERRIDE["base_url"])
    assert gate is not None and not gate.locked(), "失败后通道没有全部归还"


@pytest.mark.anyio
async def test_different_hosts_have_independent_gates(monkeypatch) -> None:
    """快速档与深度档指向不同供应商时，各排各的队，互不拖累。"""
    a = llm._gate_for("https://vendor-a.example/v1")
    b = llm._gate_for("https://vendor-b.example/v1")
    assert a is not None and b is not None and a is not b
    assert llm._gate_for("https://vendor-a.example/v2") is a, "同 host 不同路径应共用一道闸"


@pytest.mark.anyio
async def test_zero_limit_disables_the_gate(monkeypatch) -> None:
    monkeypatch.setattr(llm, "_concurrency_limit", lambda: 0)
    assert llm._gate_for("https://any.example/v1") is None


@pytest.mark.anyio
async def test_queueing_is_announced_as_progress(monkeypatch) -> None:
    """排队要告诉用户在等什么——否则前端只有一个转圈，与卡死无从区分。"""
    notified: list[str] = []

    async def fake_notify(case_id, message, **kw):
        notified.append(message)

    monkeypatch.setattr(llm, "_notify", fake_notify)

    gate = llm._gate_for("https://queue.example/v1")
    assert gate is not None
    await gate.acquire(); await gate.acquire()          # 占满两个槽位
    try:
        task = asyncio.create_task(
            llm._acquire_slot("https://queue.example/v1",
                              case_id="c1", step_key="s1", model="m")
        )
        await asyncio.sleep(0.05)
        assert any("排队" in m for m in notified), "排队时必须发进度通知"
        gate.release()                                   # 让出一个槽位
        slot = await asyncio.wait_for(task, timeout=5)
        assert slot is gate
    finally:
        gate.release()
        gate.release()


# ---------------------------------------------------------------------------
# 在途可见性：记录有滞后，状态没有
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_inflight_calls_are_visible_while_running(monkeypatch) -> None:
    """llm_calls 只记已完成的调用；「现在能不能重启」取决于在途的那些。

    部署端的真实教训：一个跑了 15 分钟还没回来的思维链调用，在任何库表里
    都不可见——「最近一次调用在 15 分钟前」既可能是空闲，也可能正是
    最不该打断的时刻。runtime_stats 暴露的是此刻。
    """
    llm._MODEL_QUIRKS[MODEL] = {"stream_chat": True}
    tracker = Tracker()
    monkeypatch.setattr(llm, "_client", lambda *a, **k: FakeClient(tracker))

    seen: dict = {}

    async def one() -> str:
        return await llm.chat([{"role": "user", "content": "hi"}], override=OVERRIDE)

    async def watch() -> None:
        await asyncio.sleep(0.05)          # 等调用真正在途
        seen.update(llm.runtime_stats())

    results, _ = await asyncio.gather(asyncio.gather(*[one() for _ in range(4)]), watch())

    assert seen["inflight"] >= 1, "在途期间 runtime_stats 必须能看见调用"
    assert seen["inflight"] <= 2, "在途数不该超过并发闸上限"
    assert seen["queued"] >= 1, "闸满时排队数也要可见——泄漏判定靠 queued 与 inflight 的组合"
    assert MODEL in seen["inflight_models"]

    after = llm.runtime_stats()
    assert after["inflight"] == 0 and after["queued"] == 0, "跑完必须清零，否则指标本身在说谎"
    assert all(r == "片段一片段二" for r in results)


def test_health_endpoint_exposes_llm_runtime(client) -> None:
    """更新前置检查只需一次 GET /system/health，不必再做 netstat 取证。"""
    r = client.get("/api/v1/system/health")
    assert r.status_code == 200
    stats = r.json().get("llm")
    assert stats is not None, "health 必须带 llm 运行态"
    for key in ("inflight", "queued", "oldest_inflight_sec", "concurrency_limit"):
        assert key in stats, f"缺字段 {key}"
